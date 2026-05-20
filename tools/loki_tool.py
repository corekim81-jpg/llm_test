"""
tools/loki_tool.py — Loki 로그 조회 도구  [수정본]
──────────────────────────────────────────────────────
BankSystem_16 실제 Loki 스트림 레이블:
  Web : service_name="bank-web-httpd-logs"  / server_role="web"
  WAS : service_name="bank-was-tomcat-logs" / server_role="was"
  DB  : 미수집 (현재 없음)

변경 요약:
  - loki_host(host 레이블) → service_name + server_role 레이블로 교체
  - LogQLBuilder: {host=} → {service_name=, server_role=}
  - LokiInput: loki_host → service_name / server_role 분리
  - 테스트 케이스: 실제 service_name 기준으로 교체

독립 실행:
    MOCK_MODE=true python -m monitoring_llm.tools.loki_tool
    LOKI_URL=http://192.168.0.41:3100 python -m monitoring_llm.tools.loki_tool
"""

import re
import json
import hashlib
from datetime import datetime
from typing import Type, Optional
from pydantic import BaseModel, Field
import asyncio

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

try:
    from langchain_core.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool

from monitoring_llm.tools.base import (
    LOKI_URL, MOCK_MODE, safe_get, ok, err, ts_to_str
)


# ── 노드 → Loki 스트림 레이블 매핑 ──────────────────────────────
LOKI_NODE_CONFIG = {
    "dev-masternode": {
        "service_name": "bank-web-httpd-logs",
        "server_role":  "web",
    },
    "ONTUNETEST2": {
        "service_name": "bank-was-tomcat-logs",
        "server_role":  "was",
    },
    # DB 로그 수집 미설정 — 추후 추가 시 여기에 등록
    # "DESKTOP-H0M89JB": {
    #     "service_name": "bank-db-mysql-logs",
    #     "server_role":  "db",
    # },
}


# ── LogQL 빌더 ────────────────────────────────────────────────────
class LogQLBuilder:
    """
    조건에 따라 LogQL 자동 생성.
    스트림 셀렉터: service_name + server_role (BankSystem_16 기준)
    """
    def __init__(
        self,
        service_name: str,
        server_role: str = "",
        extra_labels: dict = None,
    ):
        self.service_name = service_name
        self.server_role  = server_role
        self.extra_labels = extra_labels or {}

    def build(
        self,
        level_filter: str = "",   # "ERROR|WARN" — Apache access log엔 없음, 비워도 됨
        keyword: str = "",        # "500|OOM|timeout"
        status_code: str = "",    # HTTP 상태코드 "500" / "4xx" / "5xx"
        trace_id: str = "",       # Jaeger TraceID
    ) -> str:
        # ── Stream selector ──────────────────────────────────────
        parts = [f'service_name="{self.service_name}"']
        if self.server_role:
            parts.append(f'server_role="{self.server_role}"')
        for k, v in self.extra_labels.items():
            parts.append(f'{k}="{v}"')
        stream = "{" + ", ".join(parts) + "}"

        # ── Pipeline filters ─────────────────────────────────────
        pipeline = ""

        # level 필터 — Tomcat/앱 로그에만 유효 (Apache access 로그엔 없음)
        if level_filter:
            pipeline += f' |~ `(?i)({level_filter})`'

        # 상태코드 필터 — Apache Combined Log Format 지원
        # 형식: "METHOD /path HTTP/1.x" STATUS SIZE
        # 패턴: " 500 " / " 5xx " / " [45]xx "
        if status_code:
            if status_code in ("4xx", "5xx", "[45]xx"):
                prefix = status_code[0] if status_code != "[45]xx" else "[45]"
                pipeline += f' |~ `" {prefix}\\d\\d "`'
            else:
                # 구체적 상태코드: 500, 404 등
                pipeline += f' |~ `" {re.escape(status_code)} "`'

        if keyword:
            # | 는 regex OR 연산자 — re.escape 하면 \| 로 변환되어 깨짐
            # 각 항목만 개별 escape 후 | 로 재결합
            escaped_kw = "|".join(re.escape(k) for k in keyword.split("|"))
            pipeline += f' |~ `(?i)({escaped_kw})`'
        if trace_id:
            pipeline += f' |= `{trace_id}`'

        return stream + pipeline

    def build_error_logql(self) -> str:
        return self.build(level_filter="ERROR|FATAL")

    def build_http500_logql(self) -> str:
        return self.build(status_code="500", level_filter="ERROR")

    def build_oom_logql(self) -> str:
        return self.build(
            keyword="OutOfMemoryError|OOM|GC overhead|java.lang.OutOfMemory"
        )


# ── 로그 전처리 ───────────────────────────────────────────────────
def preprocess_logs(raw_logs: list[dict]) -> dict:
    """
    Loki raw 로그를 LLM에 넘기기 전에 전처리.
    - 중복 제거 / 스택트레이스 추출 / 레벨별 집계
    """
    level_counts: dict[str, int] = {}
    key_logs: list[dict] = []
    seen_hashes: set[str] = set()
    stack_traces: list[list[str]] = []
    current_trace: list[str] = []
    trace_id_set: set[str] = set()

    STACK_LINE_RE = re.compile(r'^\s+at\s+[\w.$<>]+\(')
    LEVEL_RE      = re.compile(r'\b(ERROR|WARN|INFO|DEBUG|FATAL|TRACE)\b')
    TRACE_ID_RE   = re.compile(r'traceId[=:\s]+([0-9a-f]{16,32})', re.IGNORECASE)

    for entry in raw_logs:
        line = entry.get("log", "")
        t    = entry.get("time", "")

        m     = LEVEL_RE.search(line)
        level = m.group(1) if m else "OTHER"

        tid = TRACE_ID_RE.search(line)
        if tid:
            trace_id_set.add(tid.group(1))

        if STACK_LINE_RE.match(line):
            current_trace.append(line.strip())
            continue

        if len(current_trace) >= 2:
            stack_traces.append(current_trace[:8])
        current_trace = []

        level_counts[level] = level_counts.get(level, 0) + 1

        content_hash = hashlib.md5(
            re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '', line).encode()
        ).hexdigest()[:12]
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        if level in ("ERROR", "FATAL", "WARN"):
            key_logs.append({"time": t, "level": level, "log": line[:250]})

    if len(current_trace) >= 2:
        stack_traces.append(current_trace[:8])

    return {
        "total":        len(raw_logs),
        "unique":       len(seen_hashes),
        "level_counts": level_counts,
        "key_logs":     key_logs[:30],
        "stack_traces": ["\n".join(st) for st in stack_traces[:5]],
        "trace_ids":    list(trace_id_set)[:10],
    }


# ── Mock 데이터 ────────────────────────────────────────────────────
def _mock_logs(service_name: str, keyword: str = "") -> dict:
    sample_logs = [
        {"time": "14:32:01", "level": "ERROR",
         "log": f"[ERROR] [{service_name}] java.lang.OutOfMemoryError: Java heap space"},
        {"time": "14:32:01", "level": "ERROR",
         "log": "  at java.util.Arrays.copyOf(Arrays.java:3210)"},
        {"time": "14:32:01", "level": "ERROR",
         "log": "  at com.bank.service.TransactionService.process(TransactionService.java:87)"},
        {"time": "14:32:03", "level": "ERROR",
         "log": f"[ERROR] [{service_name}] HTTP 500 /api/transfer - Internal Server Error"},
        {"time": "14:32:05", "level": "WARN",
         "log": f"[WARN]  [{service_name}] DB connection timeout after 30000ms - retrying (2/3)"},
        {"time": "14:32:10", "level": "ERROR",
         "log": f"[ERROR] [{service_name}] AJP connection to 192.168.16.20:8009 refused"},
        {"time": "14:32:15", "level": "ERROR",
         "log": f"[ERROR] [{service_name}] HTTP 500 /api/balance - java.lang.NullPointerException"},
        {"time": "14:32:20", "level": "WARN",
         "log": f"[WARN]  [{service_name}] Slow query detected: 3241ms for SELECT * FROM transactions"},
        {"time": "14:33:01", "level": "ERROR",
         "log": f"[ERROR] [{service_name}] GC overhead limit exceeded"},
    ]
    if keyword:
        sample_logs = [
            e for e in sample_logs
            if re.search(keyword, e["log"], re.IGNORECASE)
        ]
    return preprocess_logs(sample_logs)


# ── Tool 입력 스키마 ───────────────────────────────────────────────
class LokiInput(BaseModel):
    service_name: str = Field(
        description=(
            "Loki service_name 레이블 "
            "(예: bank-web-httpd-logs | bank-was-tomcat-logs)"
        )
    )
    server_role: str = Field(
        default="",
        description="Loki server_role 레이블 (예: web | was)"
    )
    start_ts: int    = Field(description="시작 Unix timestamp")
    end_ts: int      = Field(description="종료 Unix timestamp")
    level_filter: str = Field(
        default="ERROR|WARN",
        description="로그 레벨 필터 — 대소문자 자동 무시 (예: 'ERROR|WARN|FATAL')"
    )
    keyword: str     = Field(default="", description="추가 키워드 필터 (예: 'OOM|timeout')")
    status_code: str = Field(
        default="",
        description=(
            "HTTP 상태코드 필터. "
            "Apache access 로그 기준: '500'(특정), '5xx'(5xx 전체), '[45]xx'(4xx+5xx). "
            "Tomcat 로그는 level_filter 사용 권장."
        )
    )
    trace_id: str    = Field(default="", description="Jaeger TraceID로 특정 요청 로그 조회")
    limit: int       = Field(default=200, description="최대 로그 수 (기본 200, 최대 500)")


# ── BaseTool 구현 ──────────────────────────────────────────────────
class LokiQueryTool(BaseTool):
    name: str = "loki_query"
    description: str = (
        "서버의 로그를 Loki에서 조회하고 전처리한다. "
        "오류/경고 로그, HTTP 상태코드, 특정 키워드(OOM, timeout 등) 검색에 사용. "
        "스택트레이스 자동 추출, Jaeger TraceID 연동 지원. "
        "service_name: bank-web-httpd-logs(Web) | bank-was-tomcat-logs(WAS)"
    )
    args_schema: Type[BaseModel] = LokiInput

    def _run(
        self,
        service_name: str,
        server_role: str = "",
        start_ts: int = 0,
        end_ts: int = 0,
        level_filter: str = "ERROR|WARN",
        keyword: str = "",
        status_code: str = "",
        trace_id: str = "",
        limit: int = 200,
    ) -> str:
        limit   = min(limit, 500)
        builder = LogQLBuilder(
            service_name=service_name,
            server_role=server_role,
        )
        logql = builder.build(
            level_filter=level_filter,
            keyword=keyword,
            status_code=status_code,
            trace_id=trace_id,
        )

        if MOCK_MODE:
            preprocessed = _mock_logs(service_name, keyword or status_code)
            return ok({
                "service_name": service_name,
                "server_role":  server_role,
                "logql":        logql,
                "mock":         True,
                "period":       f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                **preprocessed,
            })

        # Loki API 호출 (nanosecond timestamp)
        result = safe_get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query":     logql,
                "start":     start_ts * 10**9,
                "end":       end_ts   * 10**9,
                "limit":     limit,
                "direction": "backward",
            },
            timeout=20,
        )

        if not result["ok"]:
            return err(result["error"], service_name=service_name, logql=logql)

        data = result["data"]
        if data.get("status") != "success":
            return err(data.get("error", "Loki 쿼리 실패"), logql=logql)

        raw_logs = []
        for stream in data.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                ts = int(ts_ns) // 10**9
                dt = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                raw_logs.append({"time": dt, "log": line, "labels": labels})

        preprocessed = preprocess_logs(raw_logs)

        return ok({
            "service_name": service_name,
            "server_role":  server_role,
            "logql":        logql,
            "period":       f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            **preprocessed,
        })

    async def _arun(self, **kwargs) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._run(**kwargs))


# ── 독립 실행 테스트 ──────────────────────────────────────────────
if __name__ == "__main__":
    from monitoring_llm.nlp.time_parser import default_range

    print(f"{'='*55}")
    print(f"Loki Tool 테스트 — MOCK_MODE={MOCK_MODE}")
    print(f"LOKI_URL={LOKI_URL}")
    print(f"{'='*55}")

    tr   = default_range(60)
    tool = LokiQueryTool()

    # ── 실제 BankSystem_16 로그 구조 ────────────────────────────
    # bank-web-httpd-logs  : Apache Combined Log Format (access 로그)
    # bank-was-tomcat-logs : Tomcat Combined Log Format (access 로그)
    # → 둘 다 ERROR|WARN 키워드 없음 → 상태코드 필터만 유효
    # → catalina.out(Tomcat 에러 로그) 수집 필요 시 별도 OTel filelog 설정
    tr = default_range(60 * 24)  # 24시간
    test_cases = [
        {
            "service_name": "bank-web-httpd-logs",
            "server_role":  "web",
            "level_filter": "",       # access 로그 — 레벨 없음
            "keyword":      "",
            "status_code":  "5xx",    # 5xx 에러만
        },
        {
            "service_name": "bank-was-tomcat-logs",
            "server_role":  "was",
            "level_filter": "",       # access 로그 — 레벨 없음
            "keyword":      "",
            "status_code":  "5xx",    # 5xx 에러만
        },
        # 정상 동작 확인용 — 필터 없이 전체 로그
        {
            "service_name": "bank-web-httpd-logs",
            "server_role":  "web",
            "level_filter": "",
            "keyword":      "",
            "status_code":  "",       # 필터 없음 → 전체 access 로그
        },
    ]

    for tc in test_cases:
        sn = tc["service_name"]
        print(f"\n[{sn}] role={tc['server_role']}")

        # LogQL 미리보기
        builder = LogQLBuilder(
            service_name=sn,
            server_role=tc["server_role"],
        )
        print(f"  LogQL: {builder.build(level_filter=tc['level_filter'], keyword=tc['keyword'], status_code=tc['status_code'])}")

        result = tool._run(
            service_name=sn,
            server_role=tc["server_role"],
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
            level_filter=tc["level_filter"],
            keyword=tc["keyword"],
            status_code=tc["status_code"],
        )
        data = json.loads(result)
        if data.get("ok"):
            print(f"  총 로그: {data['total']}건 (중복제거 후 {data['unique']}건)")
            print(f"  레벨별: {data['level_counts']}")
            if data.get("stack_traces"):
                print(f"  스택트레이스 {len(data['stack_traces'])}개 추출됨")
            if data.get("trace_ids"):
                print(f"  TraceID 발견: {data['trace_ids'][:3]}")
            for entry in data.get("key_logs", [])[:3]:
                print(f"    [{entry['time']}] {entry['log'][:80]}")
        else:
            print(f"  ERROR: {data.get('error')}")
