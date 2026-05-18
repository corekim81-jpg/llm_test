"""
tools/loki_tool.py — Loki 로그 조회 도구
──────────────────────────────────────────────
역할: Loki HTTP API를 통해 로그 조회 및 전처리.
      LLM에 넘기기 전에 중복 제거, 스택트레이스 추출, 레벨별 분류.

LogQL 자동 생성 규칙:
  기본:        {host="<loki_host>"}
  레벨 필터:   |~ "(?i)(error|warn)"
  키워드 추가: |~ "(?i)<keyword>"
  레이블 추가: status_code 등 structured metadata 활용

독립 실행:
    MOCK_MODE=true python -m monitoring_llm.tools.loki_tool
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


# ── LogQL 빌더 ────────────────────────────────────────────────────
class LogQLBuilder:
    """
    조건에 따라 LogQL 자동 생성.
    Loki v3+ structured metadata 지원.
    """
    def __init__(self, host: str, job: Optional[str] = None):
        self.host = host
        self.job = job

    def build(
        self,
        level_filter: str = "",      # "error|warn"
        keyword: str = "",           # "500|OOM|timeout"
        status_code: str = "",       # HTTP 상태코드 "500"
        trace_id: str = "",          # Jaeger TraceID 연동
        label_filters: dict = None,  # 추가 stream 셀렉터
    ) -> str:
        # Stream selector
        # selectors = {'"host"': f'"{self.host}"'} # # 결과: {"host"="web01"}  ← 잘못된 LogQL
        selectors = {"host": f'"{self.host}"'}
        if self.job:
            selectors['"job"'] = f'"{self.job}"'
        if label_filters:
            for k, v in label_filters.items():
                selectors[f'"{k}"'] = f'"{v}"'
        stream = "{" + ",".join(f"{k}={v}" for k, v in selectors.items()) + "}"
        

        # Pipeline filters
        pipeline = ""
        if level_filter:            
            pipeline += f' |~ `(?i)({level_filter})`' # (?i) 대소문자 무시 플래그
        if status_code:
            pipeline += f' |~ `(?:status|HTTP).*{re.escape(status_code)}|{re.escape(status_code)}.*(?:status|HTTP)`'
        if keyword:
            pipeline += f' |~ `(?i)({re.escape(keyword)})`'
        if trace_id:
            pipeline += f' |= `{trace_id}`'

        return stream + pipeline

    def build_error_logql(self) -> str:
        return self.build(level_filter="ERROR|FATAL")

    def build_http500_logql(self) -> str:
        return self.build(status_code="500", level_filter="ERROR")

    def build_oom_logql(self) -> str:
        return self.build(keyword="OutOfMemoryError|OOM|GC overhead|java.lang.OutOfMemory")        


# ── 로그 전처리 ───────────────────────────────────────────────────
def preprocess_logs(raw_logs: list[dict]) -> dict:
    """
    Loki raw 로그를 LLM에 넘기기 전에 전처리.
    - 중복 제거 (동일 내용 반복 로그)
    - 스택트레이스 추출 및 그룹핑
    - 레벨별 집계
    - 핵심 에러 라인 추출
    """
    level_counts: dict[str, int] = {}
    key_logs: list[dict] = []
    seen_hashes: set[str] = set()
    stack_traces: list[list[str]] = []
    current_trace: list[str] = []
    trace_id_set: set[str] = set()

    STACK_LINE_RE = re.compile(r'^\s+at\s+[\w.$<>]+\(')
    LEVEL_RE = re.compile(r'\b(ERROR|WARN|INFO|DEBUG|FATAL|TRACE)\b')
    TRACE_ID_RE = re.compile(r'traceId[=:\s]+([0-9a-f]{16,32})', re.IGNORECASE)

    for entry in raw_logs:
        line = entry.get("log", "")
        t = entry.get("time", "")

        # 레벨 추출
        m = LEVEL_RE.search(line)
        level = m.group(1) if m else "OTHER"
        level_counts[level] = level_counts.get(level, 0) + 1

        # TraceID 추출 (Jaeger 연동용)
        tid = TRACE_ID_RE.search(line)
        if tid:
            trace_id_set.add(tid.group(1))

        # 스택트레이스 감지
        # 스택트레이스 라인이면 누적만 하고 나머지 처리 스킵
        if STACK_LINE_RE.match(line):
            current_trace.append(line.strip())
            continue                              # ← level_counts 집계에서도 제외
        
        
        # 일반 라인 진입 시 직전 스택트레이스 flush
        if len(current_trace) >= 2:
            stack_traces.append(current_trace[:8]) # 최대 8줄
        current_trace = []

        # 레벨 집계 (스택트레이스 라인 제외됨)
        level_counts[level] = level_counts.get(level, 0) + 1

        # 중복 제거
        content_hash = hashlib.md5(
            re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '', line).encode()
        ).hexdigest()[:12]
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        # 핵심 에러 로그
        if level in ("ERROR", "FATAL", "WARN"):
            key_logs.append({"time": t, "level": level, "log": line[:250]})

    # ↓ 루프 종료 후 마지막 스택트레이스 flush
    if len(current_trace) >= 2:
        stack_traces.append(current_trace[:8]) # 최대 8줄                      

    return {
        "total": len(raw_logs),
        "unique": len(seen_hashes),
        "level_counts": level_counts,
        "key_logs": key_logs[:30],
        "stack_traces": ["\n".join(st) for st in stack_traces[:5]],
        "trace_ids": list(trace_id_set)[:10],
    }


# ── Mock 데이터 ────────────────────────────────────────────────────
def _mock_logs(host: str, keyword: str = "") -> dict:
    sample_logs = [
        {"time": "14:32:01", "level": "ERROR",
         "log": f"[ERROR] [{host}] java.lang.OutOfMemoryError: Java heap space"},
        {"time": "14:32:01", "level": "ERROR",
         "log": "  at java.util.Arrays.copyOf(Arrays.java:3210)"},
        {"time": "14:32:01", "level": "ERROR",
         "log": "  at com.bank.service.TransactionService.process(TransactionService.java:87)"},
        {"time": "14:32:03", "level": "ERROR",
         "log": f"[ERROR] [{host}] HTTP 500 /api/transfer - Internal Server Error"},
        {"time": "14:32:05", "level": "WARN",
         "log": f"[WARN]  [{host}] DB connection timeout after 30000ms - retrying (2/3)"},
        {"time": "14:32:10", "level": "ERROR",
         "log": f"[ERROR] [{host}] AJP connection to 192.168.16.20:8009 refused"},
        {"time": "14:32:15", "level": "ERROR",
         "log": f"[ERROR] [{host}] HTTP 500 /api/balance - java.lang.NullPointerException"},
        {"time": "14:32:20", "level": "WARN",
         "log": f"[WARN]  [{host}] Slow query detected: 3241ms for SELECT * FROM transactions"},
        {"time": "14:33:01", "level": "ERROR",
         "log": f"[ERROR] [{host}] GC overhead limit exceeded"},
    ]
    if keyword:
        sample_logs = [
            e for e in sample_logs
            if re.search(keyword, e["log"], re.IGNORECASE)
        ]
    preprocessed = preprocess_logs(sample_logs)
    return preprocessed


# ── Tool 입력 스키마 ───────────────────────────────────────────────
class LokiInput(BaseModel):
    loki_host: str   = Field(description="Loki {host} 레이블 (예: web01-bank16)")
    start_ts: int    = Field(description="시작 Unix timestamp")
    end_ts: int      = Field(description="종료 Unix timestamp")
    level_filter: str = Field(
        # default="ERROR|WARN|error|warn", # 정규식에서 대소문자 구분 제거 하면 됨 
        default="ERROR|WARN",
        description="로그 레벨 필터 — 대소문자 자동 무시 (예: 'ERROR|WARN|FATAL')"
    )
    keyword: str      = Field(default="", description="추가 키워드 필터 (예: '500|OOM|timeout')")
    status_code: str  = Field(default="", description="HTTP 상태코드 필터 (예: '500')")
    trace_id: str     = Field(default="", description="Jaeger TraceID로 특정 요청 로그 조회")
    limit: int        = Field(default=200, description="최대 로그 수 (기본 200, 최대 500)")
    loki_job: str     = Field(default="", description="Loki {job} 레이블 (선택)")


# ── BaseTool 구현 ──────────────────────────────────────────────────
class LokiQueryTool(BaseTool):
    name: str = "loki_query"
    description: str = (
        "서버의 로그를 Loki에서 조회하고 전처리한다. "
        "오류/경고 로그, HTTP 상태코드, 특정 키워드(OOM, timeout 등) 검색에 사용. "
        "스택트레이스 자동 추출, Jaeger TraceID 연동 지원."
    )
    args_schema: Type[BaseModel] = LokiInput

    def _run(
        self,
        loki_host: str,
        start_ts: int,
        end_ts: int,
        level_filter: str = "ERROR|WARN",
        keyword: str = "",
        status_code: str = "",
        trace_id: str = "",
        limit: int = 200,
        loki_job: str = "",
    ) -> str:
        limit = min(limit, 500)
        builder = LogQLBuilder(host=loki_host, job=loki_job or None)
        logql = builder.build(
            level_filter=level_filter,
            keyword=keyword,
            status_code=status_code,
            trace_id=trace_id,
        )

        if MOCK_MODE:
            preprocessed = _mock_logs(loki_host, keyword or status_code)
            return ok({
                "host": loki_host,
                "logql": logql,
                "mock": True,
                "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
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
            return err(result["error"], host=loki_host, logql=logql)

        data = result["data"]
        if data.get("status") != "success":
            return err(data.get("error", "Loki 쿼리 실패"), logql=logql)

        # raw 로그 파싱
        raw_logs = []
        result_data = data.get("data", {})
        # for stream in data["data"]["result"]:  # "data" 키 없으면 KeyError
        for stream in result_data.get("result", []):
            labels = stream.get("stream", {})
            # for ts_ns, line in stream["values"]:  # "values" 없으면 KeyError
            for ts_ns, line in stream.get("values", []):    
                ts = int(ts_ns) // 10**9
                dt = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                raw_logs.append({
                    "time": dt,
                    "log": line,
                    "labels": labels,
                })

        # 전처리
        preprocessed = preprocess_logs(raw_logs)

        return ok({
            "host": loki_host,
            "logql": logql,
            "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            **preprocessed,
        })

    # async def _arun(self, **kwargs) -> str:
    #     return self._run(**kwargs)
    async def _arun(self, **kwargs) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._run(**kwargs)
    )


# ── 독립 실행 테스트 ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.insert(0, "../..")
    from monitoring_llm.nlp.time_parser import default_range

    print(f"{'='*55}")
    print(f"Loki Tool 테스트 — MOCK_MODE={MOCK_MODE}")
    print(f"{'='*55}")

    tr = default_range(60)
    tool = LokiQueryTool()

    tests = [
        ("web-bank16",  "", "", "500"),
        ("was-bank16",  "", "OOM|OutOfMemory", ""),
        ("db-bank16",   "", "slow|timeout", ""),
    ]

    for host, level, kw, sc in tests:
        print(f"\n[{host}] level='{level}' keyword='{kw}' status='{sc}'")

        builder = LogQLBuilder(host=host)
        print(f"  LogQL: {builder.build(level_filter=level, keyword=kw, status_code=sc)}")

        result = tool._run(
            loki_host=host,
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
            level_filter=level or "ERROR|WARN|error|warn",
            keyword=kw,
            status_code=sc,
        )
        data = json.loads(result)
        if data.get("ok"):
            print(f"  총 로그: {data['total']}건 (중복제거 후 {data['unique']}건)")
            print(f"  레벨별: {data['level_counts']}")
            if data.get("stack_traces"):
                print(f"  스택트레이스 {len(data['stack_traces'])}개 추출됨")
            if data.get("trace_ids"):
                print(f"  TraceID 발견: {data['trace_ids'][:3]}")
            for entry in data.get("key_logs", [])[:2]:
                print(f"    [{entry['time']}] {entry['log'][:80]}")
        else:
            print(f"  ERROR: {data.get('error')}")
