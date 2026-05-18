"""
analysis/error_classifier.py — 에러 분류기
────────────────────────────────────────────
역할: 로그·트레이스에서 에러를 HTTP / DB / JVM / 시스템 으로 분류하고
      원인·영향·Jaeger 연동 정보를 구조화하여 반환.

흐름:
    (Loki 결과, Jaeger 결과)
     → classify_from_logs()     에러코드별 분류 + 원인 추론
     → correlate_with_traces()  TraceID로 Loki↔Jaeger 연결
     → ErrorReport 반환

독립 실행:
    python -m monitoring_llm.analysis.error_classifier
"""

from __future__ import annotations

import os 
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import re
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from monitoring_llm.analysis.log_analyzer import LogAnalysisResult

from typing import Optional

log = logging.getLogger("monitoring_llm.analysis")


# ── 에러 분류 사전 ────────────────────────────────────────────────────
HTTP_TAXONOMY: dict[str, dict] = {
    "400": {"name": "Bad Request",          "layer": "Web",  "cause": "클라이언트 요청 형식 오류"},
    "401": {"name": "Unauthorized",         "layer": "Web",  "cause": "인증 실패 — 토큰/세션 만료 확인"},
    "403": {"name": "Forbidden",            "layer": "Web",  "cause": "권한 부족 — ACL/방화벽 확인"},
    "404": {"name": "Not Found",            "layer": "Web",  "cause": "경로 오류 또는 리소스 삭제"},
    "500": {"name": "Internal Server Error","layer": "WAS",  "cause": "WAS 코드 예외 (NPE·OOM·DB 연결 실패)"},
    "502": {"name": "Bad Gateway",          "layer": "Web",  "cause": "Web→WAS AJP 연결 실패 — WAS 다운 확인"},
    "503": {"name": "Service Unavailable",  "layer": "WAS",  "cause": "WAS 스레드풀 고갈 또는 디스크 풀"},
    "504": {"name": "Gateway Timeout",      "layer": "WAS",  "cause": "WAS 응답 지연 — DB slow query 의심"},
}

DB_TAXONOMY: dict[str, dict] = {
    "1040": {"name": "Too many connections", "cause": "max_connections 초과 — 연결 풀 설정 확인"},
    "1045": {"name": "Access denied",        "cause": "DB 계정 또는 비밀번호 오류"},
    "1205": {"name": "Lock wait timeout",    "cause": "트랜잭션 잠금 대기 초과 — deadlock 가능성"},
    "1213": {"name": "Deadlock found",       "cause": "트랜잭션 교착 — 쿼리 실행 순서 재조정 필요"},
    "2003": {"name": "Can't connect",        "cause": "DB 서버 다운 또는 방화벽 차단"},
    "2013": {"name": "Lost connection",      "cause": "네트워크 단절 또는 wait_timeout 초과"},
}

JVM_TAXONOMY: dict[str, dict] = {
    "OutOfMemoryError": {
        "name": "Heap 부족",
        "cause": "JVM 힙 설정 부족 또는 메모리 누수 — jmap으로 힙 덤프 분석",
    },
    "GC overhead":  {
        "name": "GC 과부하",
        "cause": "GC에 CPU 98% 이상 소모 — 힙 증설 또는 메모리 누수 수정",
    },
    "StackOverflow":{
        "name": "스택 오버플로우",
        "cause": "무한 재귀 호출 — 코드 로직 점검",
    },
    "NullPointerException": {
        "name": "NPE",
        "cause": "null 객체 참조 — 스택트레이스에서 코드 위치 확인",
    },
    "ClassNotFoundException": {
        "name": "클래스 미발견",
        "cause": "JAR 누락 또는 클래스패스 오류 — 배포 검토",
    },
}


SYS_TAXONOMY: dict[str, dict] = {
    "No space left":     {"name": "디스크 풀",     "cause": "logrotate 강제 실행 후 용량 확보"},
    "Connection refused":{"name": "연결 거부",     "cause": "대상 서비스 다운 또는 방화벽 차단"},
    "Read timed out":    {"name": "읽기 타임아웃", "cause": "네트워크 지연 또는 대상 서비스 느림"},
    "ENOSPC":            {"name": "디스크 풀(OS)", "cause": "파일시스템 마운트 포인트 용량 0"},
    "Thread pool full":  {"name": "스레드풀 고갈", "cause": "maxThreads 초과 — 스레드 설정 증설 또는 slow 요청 제거"},
}


# ── 위험도 아이콘 (공통 상수) ─────────────────────────────────────────
RISK_ICON: dict[str, str] = {"low": "🟢", "medium": "🟡", "high": "🔴"}

# ── 패턴명 → (category, code) 매핑 ───────────────────────────────────
PATTERN_TO_CAT: dict[str, tuple[str, str]] = {
    "OOM":               ("JVM", "OutOfMemoryError"),
    "GC_OVERHEAD":       ("JVM", "GC overhead"),        # ✅ 추가
    "NPE":               ("JVM", "NullPointerException"),
    "AJP_Error":         ("SYS", "Connection refused"),
    "DB_ConnFail":       ("DB",  "2003"),
    "DB_Deadlock":       ("DB",  "1213"),
    "DB_SlowQuery":      ("DB",  "slow_query"),
    "HTTP_500":          ("HTTP","500"),
    "HTTP_502":          ("HTTP","502"),                 # ✅ 추가
    "HTTP_503":          ("HTTP","503"),
    "Disk_Full":         ("SYS", "No space left"),
    "ConnRefused":       ("SYS", "Connection refused"),
    "Timeout":           ("SYS", "Read timed out"),
    "ThreadPoolExhaust": ("SYS", "Thread pool full"),   # ✅ SYS_TAXONOMY와 연결
}

# Apache/Nginx 공통 로그 형식: "METHOD /path HTTP/x.x" STATUS SIZE
_HTTP_CODE_RE = {
    code: re.compile(rf'" {code} \d+')
    for code in HTTP_TAXONOMY
}


# ── 분류 결과 구조체 ──────────────────────────────────────────────────
@dataclass
class ErrorItem:
    code: str          # "500" | "OOM" | "1213" 등
    category: str      # "HTTP" | "DB" | "JVM" | "SYS"
    name: str
    layer: str         # "Web" | "WAS" | "DB" | "OS"
    cause: str         # 추정 원인
    count: int         # 발생 횟수
    trace_ids: list    = field(default_factory=list)
    first_seen: str    = ""
    last_seen: str     = ""


@dataclass
class ErrorReport:
    errors: list[ErrorItem]          = field(default_factory=list)
    http_error_freq: dict            = field(default_factory=dict)  # {code: count}
    error_timeline:  dict            = field(default_factory=dict)  # {HH:MM: count}
    correlated_traces: list[dict]    = field(default_factory=list)
    root_layer: str                  = ""    # 에러가 시작된 계층 추정

    @property
    def top_errors(self) -> list[ErrorItem]:
        return sorted(self.errors, key=lambda e: -e.count)[:5]

    def to_prompt_text(self) -> str:
        lines = []
        if self.root_layer:
            lines.append(f"추정 발생 계층: {self.root_layer}")
        lines.append(f"에러 유형 {len(self.errors)}종:")
        for e in self.top_errors:
            lines.append(
                f"  [{e.category}] {e.code} {e.name} — "
                f"{e.count}건 | 원인: {e.cause}"
            )
            if e.trace_ids:
                lines.append(f"    TraceID: {e.trace_ids[:2]}")
        if self.correlated_traces:
            lines.append(f"연관 트레이스 {len(self.correlated_traces)}건:")
            for t in self.correlated_traces[:3]:
                lines.append(
                    f"  {t.get('traceID','')} — {t.get('total_ms',0)}ms "
                    f"에러스팬 {t.get('error_count',0)}개"
                )
        return "\n".join(lines)


# ── 분류 함수 ─────────────────────────────────────────────────────────
def classify_from_logs(log_result: LogAnalysisResult | dict) -> ErrorReport:
    """
    log_analyzer.LogAnalysisResult 또는 raw dict에서 에러를 분류.
    """
    # 모듈 상단 TYPE_CHECKING으로 타입 힌트 처리,
    # 런타임 import는 여기서 한 번만
    from monitoring_llm.analysis.log_analyzer import LogAnalysisResult, preprocess

    if isinstance(log_result, dict):
        # from monitoring_llm.analysis.log_analyzer import preprocess
        log_result = preprocess(log_result)

    report = ErrorReport()

    # ── ① 패턴 기반 분류 ──────────────────────────────────────────────
    # pattern_to_cat = {
    #     "OOM":               ("JVM",  "OutOfMemoryError"),
    #     "NPE":               ("JVM",  "NullPointerException"),
    #     "AJP_Error":         ("SYS",  "Connection refused"),
    #     "DB_ConnFail":       ("DB",   "2003"),
    #     "DB_Deadlock":       ("DB",   "1213"),
    #     "DB_SlowQuery":      ("DB",   "slow_query"),
    #     "HTTP_500":          ("HTTP", "500"),
    #     "HTTP_503":          ("HTTP", "503"),
    #     "Disk_Full":         ("SYS",  "No space left"),
    #     "ConnRefused":       ("SYS",  "Connection refused"),
    #     "Timeout":           ("SYS",  "Read timed out"),
    #     "ThreadPoolExhaust": ("WAS",  "Thread pool full"),
    # }

    for pat_name, cnt in log_result.patterns.items():
        if pat_name not in PATTERN_TO_CAT:
            continue
        cat, code = PATTERN_TO_CAT[pat_name]
        item = _build_error_item(code, cat, cnt)
        item.trace_ids = log_result.trace_ids[:3]
        report.errors.append(item)

    # ── ② HTTP 에러코드 빈도 집계 (위치 한정 정규식) ─
    for entry in log_result.key_logs:
        line = entry.get("log", "")
        # for code in HTTP_TAXONOMY:
            # if re.search(rf'\b{code}\b', line):
        for code, pattern in _HTTP_CODE_RE.items():
            if pattern.search(line):
                report.http_error_freq[code] = report.http_error_freq.get(code, 0) + 1

    # HTTP 빈도 기반 ErrorItem 추가 (미분류분)
    # ── ③ 미분류 HTTP 코드 추가 ───────────────────────────────────────
    existing_http = {e.code for e in report.errors if e.category == "HTTP"}
    for code, cnt in report.http_error_freq.items():
        if code not in existing_http:
            report.errors.append(_build_error_item(code, "HTTP", cnt))

    report.error_timeline = log_result.error_timeline

    # ── 발생 계층 추정 ────────────────────────────────────────────────
    report.root_layer = _infer_root_layer(report.errors)

    return report


def correlate_with_traces(report: ErrorReport, jaeger_json: str | dict) -> ErrorReport:
    """
    Jaeger 트레이스 결과와 로그 TraceID를 연결하여
    report.correlated_traces 채움.
    """
    if isinstance(jaeger_json, str):
        try:
            data = json.loads(jaeger_json)
        except Exception:
            return report
    else:
        data = jaeger_json

    traces = data.get("traces", [])
    seen: set[str] = set()
    
    # for t in traces:
    #     trace_id = t.get("traceID", "")
    #     # 로그 TraceID와 교차 확인
    #     for err in report.errors:
    #         if any(tid in trace_id for tid in err.trace_ids):
    #             if t not in report.correlated_traces:
    #                 report.correlated_traces.append(t)
    #     # 에러 트레이스는 무조건 포함
    #     if t.get("error_count", 0) > 0:
    #         if t not in report.correlated_traces:
    #             report.correlated_traces.append(t)
    
    for t in traces:
        trace_id = t.get("traceID", "")

        # ✅ 완전 일치 비교
        for err in report.errors:
            if trace_id in err.trace_ids and trace_id not in seen:
                report.correlated_traces.append(t)
                seen.add(trace_id)

        # 에러 스팬이 있는 트레이스는 무조건 포함
        if t.get("error_count", 0) > 0 and trace_id not in seen:
            report.correlated_traces.append(t)
            seen.add(trace_id)

    return report


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────
def _build_error_item(code: str, category: str, count: int) -> ErrorItem:
    if category == "HTTP" and code in HTTP_TAXONOMY:
        info = HTTP_TAXONOMY[code]
        return ErrorItem(code=code, category="HTTP", name=info["name"],
                         layer=info.get("layer","WAS"), cause=info["cause"], count=count)
    if category == "DB" and code in DB_TAXONOMY:
        info = DB_TAXONOMY[code]
        return ErrorItem(code=code, category="DB", name=info["name"],
                         layer="DB", cause=info["cause"], count=count)
    if category == "JVM":
        info = JVM_TAXONOMY.get(code, {"name": code, "cause": "JVM 오류"})
        return ErrorItem(code=code, category="JVM", name=info["name"],
                         layer="WAS", cause=info["cause"], count=count)
    if category in ("SYS", "WAS"):
        info = SYS_TAXONOMY.get(code, {"name": code, "cause": "시스템 오류"})
        return ErrorItem(code=code, category="SYS", name=info["name"],
                         layer="OS", cause=info["cause"], count=count)
    return ErrorItem(code=code, category=category, name=code,
                     layer="Unknown", cause="추가 분석 필요", count=count)


def _infer_root_layer(errors: list[ErrorItem]) -> str:
    """에러 항목들에서 최초 발생 계층 추정 (DB > WAS > Web > OS 우선순위)"""
    layers = {e.layer for e in errors}
    for priority in ("DB", "WAS", "Web", "OS"):
        if priority in layers:
            return priority
    return "Unknown"


# ── 독립 실행 테스트 ──────────────────────────────────────────────────
if __name__ == "__main__":
    # import sys; sys.path.insert(0, "../..")
    # from monitoring_llm.analysis.log_analyzer import preprocess, SAMPLE_LOKI  # type: ignore

    # SAMPLE = {
    #     "total": 9, "unique": 7,
    #     "level_counts": {"ERROR": 7, "WARN": 2},
    #     "patterns": {"OOM": 2, "HTTP_500": 3, "AJP_Error": 1, "Timeout": 2},
    #     "key_logs": [
    #         {"time":"14:32:01","level":"ERROR","log":'[ERROR] "POST /api/transfer HTTP/1.1" 500 1234 traceId=abc123'},
    #         {"time":"14:32:03","level":"ERROR","log":"[ERROR] java.lang.OutOfMemoryError: Java heap space"},
    #         {"time":"14:32:10","level":"ERROR","log":"[ERROR] AJP error: connection refused"},
    #     ],
    #     "error_timeline": {"14:32": 5, "14:33": 3, "14:34": 1},
    #     "trace_ids": ["abc123def456"],
    # }

    # from monitoring_llm.analysis.log_analyzer import preprocess
    # log_res = preprocess(SAMPLE)
    # report  = classify_from_logs(log_res)

    # print("에러 분류 결과\n" + "="*50)
    # print(f"발생 계층 추정: {report.root_layer}")
    # print(f"에러 종류 {len(report.errors)}개:")
    # for e in report.top_errors:
    #     print(f"  [{e.category}] {e.code} {e.name} {e.count}건")
    #     print(f"    원인: {e.cause}")
    # print(f"\n프롬프트 텍스트:\n{report.to_prompt_text()}")
    
    import sys; sys.path.insert(0, "../..")
    from monitoring_llm.analysis.log_analyzer import preprocess

    SAMPLE = {
        "total": 9, "unique": 7,
        "level_counts": {"ERROR": 7, "WARN": 2},
        "patterns": {
            "OOM": 2, "GC_OVERHEAD": 1, "HTTP_500": 3,
            "HTTP_502": 1, "AJP_Error": 1, "Timeout": 2,
            "ThreadPoolExhaust": 1,
        },
        "key_logs": [
            {"time": "14:32:01", "level": "ERROR",
             "log": '[ERROR] "POST /api/transfer HTTP/1.1" 500 1234 traceId=abc123'},
            {"time": "14:32:02", "level": "ERROR",
             "log": '[ERROR] "GET /api/account HTTP/1.1" 502 0'},
            {"time": "14:32:03", "level": "ERROR",
             "log": "[ERROR] java.lang.OutOfMemoryError: Java heap space"},
        ],
        "error_timeline": {"14:32": 5, "14:33": 3},
        "trace_ids": ["abc123def456"],
    }

    log_res = preprocess(SAMPLE)
    report  = classify_from_logs(log_res)

    print("에러 분류 결과\n" + "=" * 50)
    print(f"발생 계층 추정: {report.root_layer}")
    print(f"에러 종류 {len(report.errors)}개:")
    for e in report.top_errors:
        print(f"  [{e.category}] {e.code} {e.name} {e.count}건")
        print(f"    원인: {e.cause}")
    print(f"\n프롬프트 텍스트:\n{report.to_prompt_text()}")
