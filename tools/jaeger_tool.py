"""
tools/jaeger_tool.py  — Jaeger / Grafana Tempo 듀얼 백엔드
──────────────────────────────────────────────────────────
백엔드 선택 우선순위:
  1. JAEGER_URL 설정 시 → Jaeger API (/api/traces, /api/services)
  2. TEMPO_URL  설정 시 → Tempo  API (/api/search TraceQL, /api/traces/{id})
  3. 둘 다 없으면 → 즉시 스킵, "미설정" 응답

외부 인터페이스(JaegerTraceListTool, JaegerTraceDetailTool,
list_jaeger_services)는 변경 없음 — nodes.py / param_binder.py 수정 불필요.
"""

import json
from datetime import datetime
from typing import Type
from pydantic import BaseModel, Field

try:
    from langchain_core.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool

from monitoring_llm.tools.base import (
    JAEGER_URL, TEMPO_URL, MOCK_MODE, safe_get, ok, err, ts_to_str
)

# connection_test.py 에서 jaeger_mod.MOCK_MODE = True 로 override 가능
MOCK_MODE = MOCK_MODE


# ── 백엔드 선택 ────────────────────────────────────────────────────
def _get_backend() -> tuple[str, str]:
    """Returns ('jaeger' | 'tempo' | 'none', base_url)"""
    if JAEGER_URL:
        return "jaeger", JAEGER_URL
    if TEMPO_URL:
        return "tempo", TEMPO_URL
    return "none", ""


# ── 트레이스 분석 유틸 (Jaeger + Tempo /api/traces/{id} 공용) ──────
def analyze_trace(trace: dict) -> dict:
    """
    Jaeger 포맷 트레이스 분석.
    Tempo /api/traces/{id} 도 Jaeger 포맷으로 반환하므로 동일하게 사용.
    """
    spans     = trace.get("spans", [])
    processes = trace.get("processes", {})

    if not spans:
        return {"traceID": trace.get("traceID", ""), "spans": 0}

    span_summaries = []
    error_spans    = []

    for span in spans:
        duration_ms  = round(span.get("duration", 0) / 1000, 2)
        service_name = processes.get(
            span.get("processID", ""), {}
        ).get("serviceName", "unknown")
        tags     = {t["key"]: t["value"] for t in span.get("tags", [])}
        is_error = tags.get("error", False) or tags.get("http.status_code", 200) >= 500

        summary = {
            "operation":    span.get("operationName", ""),
            "service":      service_name,
            "duration_ms":  duration_ms,
            "is_error":     is_error,
            "status_code":  tags.get("http.status_code"),
            "db_statement": tags.get("db.statement", "")[:100] if "db.statement" in tags else None,
        }
        span_summaries.append(summary)
        if is_error:
            error_spans.append(summary)

    root     = min(spans, key=lambda s: s.get("startTime", 0))
    total_ms = round(root.get("duration", 0) / 1000, 2)
    critical = sorted(span_summaries, key=lambda s: s["duration_ms"], reverse=True)[:3]

    return {
        "traceID":       trace.get("traceID", "")[:16],
        "total_ms":      total_ms,
        "span_count":    len(spans),
        "error_count":   len(error_spans),
        "critical_path": critical,
        "error_spans":   error_spans[:5],
        "start_time":    datetime.fromtimestamp(
            root.get("startTime", 0) / 10**6
        ).strftime("%H:%M:%S.%f")[:12],
    }


# ── Tempo 전용 유틸 ────────────────────────────────────────────────
def _tempo_search(
    base_url: str,
    service_name: str,
    start_ts: int,
    end_ts: int,
    limit: int,
    error_only: bool,
    min_duration_ms: int,
) -> dict:
    """Tempo /api/search — TraceQL 쿼리"""
    conditions = [f'resource.service.name="{service_name}"']
    if error_only:
        conditions.append("status=error")
    if min_duration_ms > 0:
        conditions.append(f"duration>{min_duration_ms}ms")

    traceql = "{" + " && ".join(conditions) + "}"
    params  = {
        "q":     traceql,
        "start": start_ts,
        "end":   end_ts,
        "limit": min(limit, 50),
    }
    return safe_get(f"{base_url}/api/search", params=params, timeout=15)


def _tempo_to_analyzed(traces: list, error_only: bool) -> list[dict]:
    """Tempo /api/search 결과 → analyze_trace() 호환 포맷"""
    results = []
    for t in traces:
        duration_ms = float(t.get("durationMs", 0))

        # serviceStats: Tempo가 집계한 서비스별 에러 카운트 (가장 신뢰도 높음)
        service_stats      = t.get("serviceStats", {})
        stats_error_count  = sum(v.get("errorCount", 0) for v in service_stats.values())

        # spanSet 에서 에러 스팬 추출 (TraceQL 필터 시 포함됨)
        span_set = t.get("spanSet") or (
            t.get("spanSets", [{}])[0] if t.get("spanSets") else {}
        )
        spans       = span_set.get("spans", [])
        error_count = 0
        error_spans = []

        for span in spans:
            attrs = {}
            for a in span.get("attributes", []):
                v = a.get("value", {})
                attrs[a["key"]] = (
                    v.get("stringValue")
                    or v.get("intValue")
                    or v.get("boolValue")
                )
            status_code = attrs.get("http.status_code", 0)
            is_error    = (
                str(attrs.get("otel.status_code", "")).upper() == "ERROR"
                or str(attrs.get("status", "")).lower() == "error"   # ← Tempo 실제 키
                or (status_code and int(status_code) >= 500)
            )
            if is_error:
                error_count += 1
                error_spans.append({
                    "operation":    t.get("rootTraceName", ""),
                    "service":      t.get("rootServiceName", ""),
                    "duration_ms":  duration_ms,
                    "is_error":     True,
                    "status_code":  status_code,
                    "db_statement": None,
                })

        # serviceStats.errorCount 우선 반영 (span 파싱보다 신뢰도 높음)
        if stats_error_count > 0 and error_count == 0:
            error_count = stats_error_count

        # error_only 쿼리면 반환된 모든 트레이스는 에러 (TraceQL이 이미 필터)
        if error_only and error_count == 0:
            error_count = 1

        start_ns   = int(t.get("startTimeUnixNano", "0") or "0")
        start_time = ""
        if start_ns:
            try:
                start_time = datetime.fromtimestamp(start_ns / 1e9).strftime("%H:%M:%S.%f")[:12]
            except Exception:
                pass

        # error_spans 비어 있으면 serviceStats 기반으로 채움
        if not error_spans and stats_error_count > 0:
            for svc_name, svc_stat in service_stats.items():
                if svc_stat.get("errorCount", 0) > 0:
                    error_spans.append({
                        "operation":    t.get("rootTraceName", ""),
                        "service":      svc_name,
                        "duration_ms":  duration_ms,
                        "is_error":     True,
                        "status_code":  None,
                        "db_statement": None,
                    })

        results.append({
            "traceID":       t.get("traceID", "")[:16],
            "total_ms":      duration_ms,
            "span_count":    span_set.get("matched", len(spans)),
            "error_count":   error_count,
            "critical_path": [{
                "operation":    t.get("rootTraceName", ""),
                "service":      t.get("rootServiceName", ""),
                "duration_ms":  duration_ms,
                "is_error":     error_count > 0,
                "status_code":  None,
                "db_statement": None,
            }],
            "error_spans":   error_spans[:5],
            "service_stats": service_stats,   # ← 서비스별 span/error 카운트
            "start_time":    start_time,
        })
    return results


def _list_tempo_services(base_url: str) -> list[str]:
    """Tempo 서비스 목록 조회 — /api/search/tag/service.name/values"""
    result = safe_get(f"{base_url}/api/search/tag/service.name/values", timeout=5)
    if not result["ok"]:
        return []
    return result["data"].get("tagValues", [])


# ── Mock 데이터 ────────────────────────────────────────────────────
def _mock_traces(service: str, error_only: bool) -> list[dict]:
    base = [
        {
            "traceID": "abc123def456789a",
            "total_ms": 342.5, "span_count": 8, "error_count": 2,
            "critical_path": [
                {"operation": "SELECT transactions", "service": "db-service",
                 "duration_ms": 280.0, "is_error": False, "status_code": None,
                 "db_statement": "SELECT * FROM transactions WHERE account_id=? LIMIT 1000"},
                {"operation": "POST /api/transfer", "service": "was-service",
                 "duration_ms": 340.0, "is_error": True, "status_code": 500, "db_statement": None},
            ],
            "error_spans": [
                {"operation": "POST /api/transfer", "service": "was-service",
                 "duration_ms": 340.0, "is_error": True, "status_code": 500, "db_statement": None},
            ],
            "start_time": "14:32:03.421",
        },
        {
            "traceID": "def789abc123456b",
            "total_ms": 89.3, "span_count": 5, "error_count": 0,
            "critical_path": [
                {"operation": "GET /api/balance", "service": "web-service",
                 "duration_ms": 89.3, "is_error": False, "status_code": 200, "db_statement": None},
            ],
            "error_spans": [],
            "start_time": "14:32:08.103",
        },
    ]
    return [t for t in base if not error_only or t["error_count"] > 0]


# ── Tool 입력 스키마 ───────────────────────────────────────────────
class JaegerTraceListInput(BaseModel):
    service_name:    str  = Field(description="서비스 이름 (예: was-service)")
    start_ts:        int  = Field(description="시작 Unix timestamp")
    end_ts:          int  = Field(description="종료 Unix timestamp")
    min_duration_ms: int  = Field(default=100, description="최소 트레이스 지속시간 (ms)")
    limit:           int  = Field(default=20,  description="최대 반환 트레이스 수")
    error_only:      bool = Field(default=False, description="에러 트레이스만 조회")


class JaegerTraceDetailInput(BaseModel):
    trace_id: str = Field(description="조회할 TraceID (16~32자 hex)")


# ── JaegerTraceListTool ────────────────────────────────────────────
class JaegerTraceListTool(BaseTool):
    name: str = "jaeger_trace_list"
    description: str = (
        "서비스의 분산 트레이스 목록을 조회하여 지연·에러 패턴을 분석한다. "
        "Jaeger 또는 Grafana Tempo 백엔드 자동 선택. 미설정 시 빈 결과 반환."
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
                "service":         service_name,
                "backend":         "mock",
                "mock":            True,
                "period":          f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                "total_traces":    len(traces),
                "error_rate_pct":  round(
                    100 * sum(1 for t in traces if t["error_count"] > 0) / max(len(traces), 1), 1
                ),
                "avg_duration_ms": round(
                    sum(t["total_ms"] for t in traces) / max(len(traces), 1), 1
                ),
                "traces": traces,
            })

        backend, base_url = _get_backend()

        if backend == "none":
            return ok({
                "service":      service_name,
                "total_traces": 0,
                "info":         "트레이스 백엔드 미설정 (JAEGER_URL / TEMPO_URL)",
            })

        if backend == "jaeger":
            return self._run_jaeger(
                base_url, service_name, start_ts, end_ts, limit, error_only, min_duration_ms
            )
        else:  # tempo
            return self._run_tempo(
                base_url, service_name, start_ts, end_ts, limit, error_only, min_duration_ms
            )

    # ── Jaeger 백엔드 ──────────────────────────────────────────────
    def _run_jaeger(
        self, base_url: str, service_name: str,
        start_ts: int, end_ts: int, limit: int,
        error_only: bool, min_duration_ms: int,
    ) -> str:
        params: dict = {
            "service":     service_name,
            "start":       start_ts * 10**6,
            "end":         end_ts   * 10**6,
            "minDuration": f"{min_duration_ms}ms",
            "limit":       limit,
        }
        if error_only:
            params["tags"] = json.dumps({"error": "true"})

        result = safe_get(f"{base_url}/api/traces", params=params, timeout=15)
        if not result["ok"]:
            return err(result["error"], service=service_name)

        raw_traces = result["data"].get("data", [])
        if not raw_traces:
            return ok({
                "service":      service_name,
                "backend":      "jaeger",
                "message":      "조건에 맞는 트레이스 없음",
                "period":       f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                "total_traces": 0,
            })

        analyzed    = [analyze_trace(t) for t in raw_traces[:limit]]
        error_count = sum(1 for t in analyzed if t.get("error_count", 0) > 0)
        durations   = [t["total_ms"] for t in analyzed if "total_ms" in t]

        return ok({
            "service":         service_name,
            "backend":         "jaeger",
            "period":          f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            "total_traces":    len(analyzed),
            "error_rate_pct":  round(100 * error_count / max(len(analyzed), 1), 1),
            "avg_duration_ms": round(sum(durations) / max(len(durations), 1), 1),
            "max_duration_ms": round(max(durations, default=0), 1),
            "traces":          analyzed,
        })

    # ── Tempo 백엔드 ───────────────────────────────────────────────
    def _run_tempo(
        self, base_url: str, service_name: str,
        start_ts: int, end_ts: int, limit: int,
        error_only: bool, min_duration_ms: int,
    ) -> str:
        result = _tempo_search(
            base_url, service_name, start_ts, end_ts, limit, error_only, min_duration_ms
        )
        if not result["ok"]:
            return err(result["error"], service=service_name)

        raw_traces = result["data"].get("traces", [])
        if not raw_traces:
            return ok({
                "service":      service_name,
                "backend":      "tempo",
                "message":      "조건에 맞는 트레이스 없음",
                "period":       f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                "total_traces": 0,
            })

        analyzed    = _tempo_to_analyzed(raw_traces[:limit], error_only)
        error_count = sum(1 for t in analyzed if t.get("error_count", 0) > 0)
        durations   = [t["total_ms"] for t in analyzed if "total_ms" in t]

        return ok({
            "service":         service_name,
            "backend":         "tempo",
            "period":          f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            "total_traces":    len(analyzed),
            "error_rate_pct":  round(100 * error_count / max(len(analyzed), 1), 1),
            "avg_duration_ms": round(sum(durations) / max(len(durations), 1), 1),
            "max_duration_ms": round(max(durations, default=0), 1),
            "traces":          analyzed,
        })

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


# ── JaegerTraceDetailTool ──────────────────────────────────────────
class JaegerTraceDetailTool(BaseTool):
    name: str = "jaeger_trace_detail"
    description: str = (
        "특정 TraceID의 상세 스팬 정보를 조회한다. "
        "Jaeger 또는 Grafana Tempo 백엔드 자동 선택. 미설정 시 빈 결과 반환."
    )
    args_schema: Type[BaseModel] = JaegerTraceDetailInput

    def _run(self, trace_id: str) -> str:
        if MOCK_MODE:
            mock = _mock_traces("was-service", False)
            t    = mock[0] if mock else {}
            t["traceID"] = trace_id[:16]
            return ok({"trace": t, "mock": True})

        backend, base_url = _get_backend()

        if backend == "none":
            return ok({
                "trace_id": trace_id,
                "info":     "트레이스 백엔드 미설정 (JAEGER_URL / TEMPO_URL)",
            })

        # Jaeger와 Tempo 모두 /api/traces/{id} 에서 Jaeger 포맷으로 반환
        result = safe_get(f"{base_url}/api/traces/{trace_id}", timeout=10)
        if not result["ok"]:
            return err(result["error"], trace_id=trace_id)

        data = result["data"].get("data", [])
        if not data:
            return ok({"message": f"TraceID {trace_id} 를 찾을 수 없습니다."})

        return ok({"trace": analyze_trace(data[0]), "backend": backend})

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


# ── 서비스 목록 조회 ──────────────────────────────────────────────
def list_jaeger_services() -> list[str]:
    """백엔드에 따라 서비스 목록을 조회한다."""
    if MOCK_MODE:
        return ["web-service", "was-service", "db-service", "BankSystem16"]

    backend, base_url = _get_backend()

    if backend == "none":
        return []

    if backend == "jaeger":
        result = safe_get(f"{base_url}/api/services", timeout=5)
        if not result["ok"]:
            return []
        return result["data"].get("data", [])

    # tempo
    return _list_tempo_services(base_url)


# ── 독립 실행 테스트 ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.insert(0, "../..")
    from monitoring_llm.nlp.time_parser import default_range

    backend, base_url = _get_backend()

    print(f"{'='*55}")
    print(f"Trace Tool 테스트 — MOCK_MODE={MOCK_MODE}")
    print(f"백엔드: {backend.upper()} ({base_url or '미설정'})")
    print(f"JAEGER_URL={JAEGER_URL or '(미설정)'}")
    print(f"TEMPO_URL={TEMPO_URL  or '(미설정)'}")
    print(f"{'='*55}")

    tr          = default_range(60)
    list_tool   = JaegerTraceListTool()
    detail_tool = JaegerTraceDetailTool()

    for service, error_only in [("was-service", False), ("was-service", True)]:
        label  = "전체" if not error_only else "에러만"
        print(f"\n[{service}] {label} 트레이스")
        result = list_tool._run(
            service_name=service, start_ts=tr.start_ts, end_ts=tr.end_ts,
            min_duration_ms=50, error_only=error_only,
        )
        data = json.loads(result)
        if data.get("ok"):
            if data.get("info"):
                print(f"  ℹ {data['info']}")
            else:
                print(f"  백엔드: {data.get('backend', '-')} | "
                      f"총 {data['total_traces']}건 | "
                      f"에러율 {data.get('error_rate_pct', 0)}%")
        else:
            print(f"  ERROR: {data.get('error')}")
