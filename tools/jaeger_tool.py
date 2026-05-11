"""
tools/jaeger_tool.py — Jaeger 트레이스 조회 도구
──────────────────────────────────────────────────
역할: Jaeger HTTP API로 분산 트레이스 조회.
      500 에러 역추적, 지연 병목 구간 특정, Loki TraceID 연동.

Jaeger API 엔드포인트:
    /api/services           — 서비스 목록
    /api/traces?service=..  — 트레이스 목록
    /api/traces/<traceID>   — 단일 트레이스 상세

BankSystem_16 서비스명 규칙:
    web-service    (Apache → AJP)
    was-service    (Tomcat/Spring Boot)
    db-service     (MySQL JDBC)

독립 실행:
    MOCK_MODE=true python -m monitoring_llm.tools.jaeger_tool
"""

import json
from datetime import datetime
from typing import Type, Optional
from pydantic import BaseModel, Field

try:
    from langchain_core.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool

from monitoring_llm.tools.base import (
    JAEGER_URL, MOCK_MODE, safe_get, ok, err, ts_to_str
)


# ── 트레이스 분석 유틸 ────────────────────────────────────────────
def analyze_trace(trace: dict) -> dict:
    """
    단일 트레이스를 분석하여 병목 구간과 에러 스팬 추출.
    반환: {traceID, total_ms, spans, critical_path, errors}
    """
    spans = trace.get("spans", [])
    processes = trace.get("processes", {})

    if not spans:
        return {"traceID": trace.get("traceID", ""), "spans": 0}

    # 스팬별 분석
    span_summaries = []
    error_spans = []

    for span in spans:
        duration_ms = round(span.get("duration", 0) / 1000, 2)  # µs → ms
        service_name = processes.get(
            span.get("processID", ""), {}
        ).get("serviceName", "unknown")

        tags = {t["key"]: t["value"] for t in span.get("tags", [])}
        is_error = tags.get("error", False) or tags.get("http.status_code", 200) >= 500

        summary = {
            "operation":    span.get("operationName", ""),
            "service":      service_name,
            "duration_ms":  duration_ms,
            "is_error":     is_error,
            "status_code":  tags.get("http.status_code"),
            "db_statement": tags.get("db.statement", "")[:100] if "db.statement" in tags else None
        }
        span_summaries.append(summary)
        if is_error:
            error_spans.append(summary)

    # 전체 지속시간 (루트 스팬)
    root = min(spans, key=lambda s: s.get("startTime", 0))
    total_ms = round(root.get("duration", 0) / 1000, 2)

    # Critical path (가장 느린 상위 3 스팬)
    critical = sorted(span_summaries, key=lambda s: s["duration_ms"], reverse=True)[:3]

    return {
        "traceID":      trace.get("traceID", "")[:16],
        "total_ms":     total_ms,
        "span_count":   len(spans),
        "error_count":  len(error_spans),
        "critical_path": critical,
        "error_spans":  error_spans[:5],
        "start_time":   datetime.fromtimestamp(
            root.get("startTime", 0) / 10**6
        ).strftime("%H:%M:%S.%f")[:12],
    }


# ── Mock 데이터 ────────────────────────────────────────────────────
def _mock_traces(service: str, error_only: bool) -> list[dict]:
    base = [
        {
            "traceID": "abc123def456789a",
            "total_ms": 342.5,
            "span_count": 8,
            "error_count": 2,
            "critical_path": [
                {"operation": "SELECT transactions", "service": "db-service",
                 "duration_ms": 280.0, "is_error": False, "status_code": None,
                 "db_statement": "SELECT * FROM transactions WHERE account_id=? LIMIT 1000"},
                {"operation": "POST /api/transfer", "service": "was-service",
                 "duration_ms": 340.0, "is_error": True, "status_code": 500,
                 "db_statement": None},
                {"operation": "AJP /api/transfer", "service": "web-service",
                 "duration_ms": 342.5, "is_error": True, "status_code": 500,
                 "db_statement": None},
            ],
            "error_spans": [
                {"operation": "POST /api/transfer", "service": "was-service",
                 "duration_ms": 340.0, "is_error": True, "status_code": 500,
                 "db_statement": None},
            ],
            "start_time": "14:32:03.421",
        },
        {
            "traceID": "def789abc123456b",
            "total_ms": 89.3,
            "span_count": 5,
            "error_count": 0,
            "critical_path": [
                {"operation": "GET /api/balance", "service": "web-service",
                 "duration_ms": 89.3, "is_error": False, "status_code": 200,
                 "db_statement": None},
                {"operation": "SELECT balance", "service": "db-service",
                 "duration_ms": 72.1, "is_error": False, "status_code": None,
                 "db_statement": "SELECT balance FROM accounts WHERE id=?"},
            ],
            "error_spans": [],
            "start_time": "14:32:08.103",
        },
    ]
    return [t for t in base if not error_only or t["error_count"] > 0]


# ── Tool 입력 스키마 ───────────────────────────────────────────────
class JaegerTraceListInput(BaseModel):
    service_name: str   = Field(description="Jaeger service 이름 (예: was-service, BankSystem16-WAS)")
    start_ts: int       = Field(description="시작 Unix timestamp")
    end_ts: int         = Field(description="종료 Unix timestamp")
    min_duration_ms: int = Field(default=100, description="최소 트레이스 지속시간 (ms, 기본 100ms)")
    limit: int          = Field(default=20, description="최대 반환 트레이스 수")
    error_only: bool    = Field(default=False, description="에러 트레이스만 조회")


class JaegerTraceDetailInput(BaseModel):
    trace_id: str = Field(description="조회할 Jaeger TraceID (16~32자 hex)")


# ── BaseTool 구현 — 트레이스 목록 ─────────────────────────────────
class JaegerTraceListTool(BaseTool):
    name: str = "jaeger_trace_list"
    description: str = (
        "서비스의 분산 트레이스 목록을 조회하여 지연·에러 패턴을 분석한다. "
        "500 에러 역추적, 느린 API 구간 특정에 사용. "
        "service_name, 시간 범위, error_only 플래그가 필요하다."
    )
    args_schema: Type[BaseModel] = JaegerTraceListInput

    def _run(
        self,
        service_name: str,
        start_ts: int,
        end_ts: int,
        min_duration_ms: int = 100,
        limit: int = 20,
        error_only: bool = False,
    ) -> str:
        limit = min(limit, 50)

        if MOCK_MODE:
            traces = _mock_traces(service_name, error_only)
            return ok({
                "service": service_name,
                "mock": True,
                "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                "total_traces": len(traces),
                "error_rate_pct": round(
                    100 * sum(1 for t in traces if t["error_count"] > 0) / max(len(traces), 1), 1
                ),
                "avg_duration_ms": round(
                    sum(t["total_ms"] for t in traces) / max(len(traces), 1), 1
                ),
                "traces": traces,
            })

        # Jaeger /api/traces 호출
        params = {
            "service":     service_name,
            "start":       start_ts * 10**6,  # Jaeger: microsecond
            "end":         end_ts   * 10**6,
            "minDuration": f"{min_duration_ms}ms",
            "limit":       limit,
        }
        if error_only:
            params["tags"] = json.dumps({"error": "true"})

        result = safe_get(f"{JAEGER_URL}/api/traces", params=params, timeout=15)

        if not result["ok"]:
            return err(result["error"], service=service_name)

        raw_traces = result["data"].get("data", [])
        if not raw_traces:
            return ok({
                "service": service_name,
                "message": "조건에 맞는 트레이스 없음",
                "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                "total_traces": 0,
            })

        analyzed = [analyze_trace(t) for t in raw_traces[:limit]]
        error_count = sum(1 for t in analyzed if t.get("error_count", 0) > 0)
        durations = [t["total_ms"] for t in analyzed if "total_ms" in t]

        return ok({
            "service": service_name,
            "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            "total_traces": len(analyzed),
            "error_rate_pct": round(100 * error_count / max(len(analyzed), 1), 1),
            "avg_duration_ms": round(sum(durations) / max(len(durations), 1), 1),
            "max_duration_ms": round(max(durations, default=0), 1),
            "traces": analyzed,
        })

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


# ── BaseTool 구현 — 단일 트레이스 상세 ────────────────────────────
class JaegerTraceDetailTool(BaseTool):
    name: str = "jaeger_trace_detail"
    description: str = (
        "특정 TraceID의 상세 스팬 정보를 조회한다. "
        "Loki 로그에서 traceId를 발견했을 때 전체 요청 흐름을 추적하는 데 사용."
    )
    args_schema: Type[BaseModel] = JaegerTraceDetailInput

    def _run(self, trace_id: str) -> str:
        if MOCK_MODE:
            mock = _mock_traces("was-service", False)
            t = mock[0] if mock else {}
            t["traceID"] = trace_id[:16]
            return ok({"trace": t, "mock": True})

        result = safe_get(f"{JAEGER_URL}/api/traces/{trace_id}", timeout=10)
        if not result["ok"]:
            return err(result["error"], trace_id=trace_id)

        data = result["data"].get("data", [])
        if not data:
            return ok({"message": f"TraceID {trace_id} 를 찾을 수 없습니다."})

        return ok({"trace": analyze_trace(data[0])})

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


# ── 서비스 목록 조회 (에이전트 초기화 시 사용) ────────────────────
def list_jaeger_services() -> list[str]:
    """Jaeger에 등록된 서비스 목록 조회"""
    if MOCK_MODE:
        return ["web-service", "was-service", "db-service", "BankSystem16"]

    result = safe_get(f"{JAEGER_URL}/api/services", timeout=5)
    if not result["ok"]:
        return []
    return result["data"].get("data", [])


# ── 독립 실행 테스트 ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.insert(0, "../..")
    from monitoring_llm.nlp.time_parser import default_range

    print(f"{'='*55}")
    print(f"Jaeger Tool 테스트 — MOCK_MODE={MOCK_MODE}")
    print(f"{'='*55}")

    tr = default_range(60)
    list_tool   = JaegerTraceListTool()
    detail_tool = JaegerTraceDetailTool()

    for service, error_only in [("was-service", False), ("was-service", True)]:
        label = "전체" if not error_only else "에러만"
        print(f"\n[{service}] {label} 트레이스")
        result = list_tool._run(
            service_name=service,
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
            min_duration_ms=50,
            error_only=error_only,
        )
        data = json.loads(result)
        if data.get("ok"):
            print(f"  총 {data['total_traces']}건 | 에러율 {data['error_rate_pct']}%"
                  f" | 평균 {data['avg_duration_ms']}ms")
            for t in data["traces"][:2]:
                print(f"  [{t['start_time']}] {t['traceID']} "
                      f"— {t['total_ms']}ms, 에러 {t['error_count']}개")
                for cp in t.get("critical_path", [])[:2]:
                    stmt = f" [{cp['db_statement'][:40]}]" if cp.get("db_statement") else ""
                    print(f"    ↳ {cp['service']}/{cp['operation']} "
                          f"({cp['duration_ms']}ms){stmt}")
        else:
            print(f"  ERROR: {data.get('error')}")

    print(f"\n[TraceID 상세 조회]")
    result = detail_tool._run(trace_id="abc123def456789a")
    data = json.loads(result)
    if data.get("ok"):
        t = data["trace"]
        print(f"  {t['traceID']} — {t['total_ms']}ms, span {t['span_count']}개")
