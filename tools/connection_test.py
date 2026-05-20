"""
tools/connection_test.py — 연결 상태 진단 + 도구 레지스트리  [수정본]
───────────────────────────────────────────────────────────────────────
변경 요약:
  - test_prometheus_metrics: instance IP:port → job 레이블 + OTel 메트릭명
  - test_loki_streams:       host= → service_name=, 5분→24h, DB 스킵
  - test_tools_mock:         prometheus_instance/loki_host → 신규 파라미터
  - CMDBLookupTool 검증:     IP 192.168.0.140 / hostname dev-masternode
"""

import json
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from monitoring_llm.tools.base import (
    PROMETHEUS_URL, LOKI_URL, JAEGER_URL, ALERTMANAGER_URL,
    MOCK_MODE, safe_get, get_session,
)
from monitoring_llm.tools.prometheus_tool import PrometheusQueryTool, PROMQL
from monitoring_llm.tools.loki_tool import LokiQueryTool, LogQLBuilder
from monitoring_llm.tools.jaeger_tool import JaegerTraceListTool, list_jaeger_services

try:
    from langchain_core.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool


OK   = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"
INFO = "\033[94m·\033[0m"


def _check(label: str, result: bool, detail: str = "") -> bool:
    icon = OK if result else FAIL
    print(f"  {icon} {label}" + (f"  {detail}" if detail else ""))
    return result


# ── 1. HTTP 연결 테스트 ────────────────────────────────────────────
def test_connections() -> dict[str, bool]:
    print("\n[1] 서비스 연결 상태")
    results = {}

    import requests as _req

    checks = [
        ("Prometheus",   f"{PROMETHEUS_URL}/-/healthy",   "Healthy"),  # "Prometheus Server is Healthy." 포함
        ("Loki",         f"{LOKI_URL}/ready",              "ready"),
        ("Jaeger",       f"{JAEGER_URL}/api/services",     None),
        ("Alertmanager", f"{ALERTMANAGER_URL}/-/healthy",  "OK"),
    ]

    for name, url, expected in checks:
        t0 = time.time()
        try:
            # safe_get 대신 requests 직접 사용 — plain text 응답 허용
            resp    = _req.get(url, timeout=3)
            elapsed = round((time.time() - t0) * 1000)
            body    = resp.text[:200]
            ok_flag = resp.status_code < 400 and (
                expected is None or expected in body
            )
            results[name] = ok_flag
            _check(f"{name:<15} {elapsed}ms  HTTP {resp.status_code}", ok_flag,
                   "" if ok_flag else f"응답: {body[:50]}")
        except Exception as e:
            elapsed = round((time.time() - t0) * 1000)
            results[name] = False
            _check(f"{name:<15} {elapsed}ms", False, str(e)[:60])

    return results


# ── 2. Prometheus 메트릭 수집 확인 ───────────────────────────────
def test_prometheus_metrics() -> dict[str, bool]:
    print("\n[2] Prometheus 메트릭 수집 확인 (job 레이블 기준)")
    results = {}

    # 실제 BankSystem_16 job + OTel 메트릭명
    checks = {
        "web  hostmetrics": (
            "system_cpu_time_seconds_total",
            "bank-web-hostmetrics",
        ),
        "was  hostmetrics": (
            "system_memory_usage_bytes",
            "bank-was-hostmetrics",
        ),
        "was  app (JVM)": (
            "jvm_memory_committed_bytes",
            "bank-was-app",
        ),
        "db   hostmetrics": (
            "system_cpu_time_seconds_total",
            "bank-db-hostmetrics",
        ),
    }

    for label, (metric, job) in checks.items():
        # instance 필터 제거 — job 레이블만 사용
        query = f'{metric}{{job="{job}"}}'
        r = safe_get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5,
        )
        if not r["ok"]:
            results[label] = False
            _check(f"{label}", False, r["error"][:50])
            continue

        data     = r["data"]
        has_data = (
            data.get("status") == "success"
            and len(data.get("data", {}).get("result", [])) > 0
        )
        results[label] = has_data
        val = ""
        if has_data:
            v   = data["data"]["result"][0]["value"][1]
            val = f"→ {float(v):.3f}"
        _check(f"{label:<35} {metric}", has_data, val)

    return results


# ── 3. Loki 로그 스트림 확인 ──────────────────────────────────────
def test_loki_streams() -> dict[str, bool]:
    print("\n[3] Loki 로그 스트림 확인 (최근 24시간)")
    results = {}

    now   = int(time.time())
    start = now - 86400   # 24h — access 로그는 최근 5분에 없을 수 있음

    # 실제 service_name 기준 (DB는 Loki 미수집)
    streams = {
        "bank-web-httpd-logs":  '{service_name="bank-web-httpd-logs"}',
        "bank-was-tomcat-logs": '{service_name="bank-was-tomcat-logs"}',
        # "bank-db-*":  DB 로그 미수집 — 스킵
    }

    for name, logql in streams.items():
        r = safe_get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": start * 10**9,
                "end":   now   * 10**9,
                "limit": 1,
            },
            timeout=5,
        )
        if not r["ok"]:
            results[name] = False
            _check(f"{name}", False, r["error"][:50])
            continue

        data     = r["data"]
        has_logs = (
            data.get("status") == "success"
            and len(data.get("data", {}).get("result", [])) > 0
        )
        results[name] = has_logs
        _check(f"{name}", has_logs,
               "로그 없음 (service_name 레이블 확인 필요)" if not has_logs else "")

    # DB 미수집 안내
    print(f"  {INFO} db 로그: 미수집 (OTel filelog 미설정)")

    return results


# ── 4. Jaeger 서비스 확인 ─────────────────────────────────────────
def test_jaeger_services() -> dict[str, bool]:
    print("\n[4] Jaeger 서비스 목록 확인")
    services = list_jaeger_services()
    results  = {}

    expected = ["web-service", "was-service", "db-service"]
    print(f"  {INFO} 발견된 서비스: {services[:10]}")
    for svc in expected:
        found = any(svc.lower() in s.lower() for s in services)
        results[svc] = found
        _check(f"{svc}", found,
               "" if found else "OTel SDK service.name 설정 확인 필요")

    return results


# ── 5. Tool 스키마 초기화 + Mock end-to-end 테스트 ─────────────────
def test_tools_mock() -> dict[str, bool]:
    print("\n[5] Tool 초기화 + Mock 모드 end-to-end 테스트")
    results = {}

    from monitoring_llm.nlp.time_parser import default_range
    tr = default_range(60)

    import monitoring_llm.tools.base as base_mod
    old_mock = base_mod.MOCK_MODE

    # ── PrometheusQueryTool ─────────────────────────────────────
    try:
        import monitoring_llm.tools.prometheus_tool as prom_mod
        base_mod.MOCK_MODE = True
        prom_mod.MOCK_MODE  = True

        tool   = PrometheusQueryTool()
        assert isinstance(tool, BaseTool), "BaseTool 아님"

        result = tool._run(
            server_hostname = "dev-masternode",      # ← 실제 hostname
            server_role     = "web",
            prometheus_job  = "bank-web-hostmetrics", # ← 변경
            app_job         = "",
            start_ts        = tr.start_ts,
            end_ts          = tr.end_ts,
        )
        data    = json.loads(result)
        ok_flag = data.get("ok") and bool(data.get("metrics"))
        results["PrometheusQueryTool"] = ok_flag
        _check("PrometheusQueryTool", ok_flag,
               f"메트릭 {len(data.get('metrics', {}))}개" if ok_flag else str(data))

        base_mod.MOCK_MODE = old_mock
        prom_mod.MOCK_MODE  = old_mock
    except Exception as e:
        results["PrometheusQueryTool"] = False
        _check("PrometheusQueryTool", False, str(e)[:60])

    # ── LokiQueryTool ───────────────────────────────────────────
    try:
        import monitoring_llm.tools.loki_tool as loki_mod
        base_mod.MOCK_MODE = True
        loki_mod.MOCK_MODE  = True

        tool   = LokiQueryTool()
        result = tool._run(
            service_name = "bank-web-httpd-logs",   # ← 변경
            server_role  = "web",                   # ← 추가
            start_ts     = tr.start_ts,
            end_ts       = tr.end_ts,
        )
        data    = json.loads(result)
        ok_flag = data.get("ok") and data.get("total", 0) > 0
        results["LokiQueryTool"] = ok_flag
        _check("LokiQueryTool", ok_flag,
               f"로그 {data.get('total', 0)}건" if ok_flag else str(data))

        base_mod.MOCK_MODE = old_mock
        loki_mod.MOCK_MODE  = old_mock
    except Exception as e:
        results["LokiQueryTool"] = False
        _check("LokiQueryTool", False, str(e)[:60])

    # ── JaegerTraceListTool ─────────────────────────────────────
    try:
        import monitoring_llm.tools.jaeger_tool as jaeger_mod
        base_mod.MOCK_MODE  = True
        jaeger_mod.MOCK_MODE = True

        tool   = JaegerTraceListTool()
        result = tool._run(
            service_name = "was-service",
            start_ts     = tr.start_ts,
            end_ts       = tr.end_ts,
        )
        data    = json.loads(result)
        ok_flag = data.get("ok") and data.get("total_traces", 0) > 0
        results["JaegerTraceListTool"] = ok_flag
        _check("JaegerTraceListTool", ok_flag,
               f"트레이스 {data.get('total_traces', 0)}건" if ok_flag else str(data))

        base_mod.MOCK_MODE  = old_mock
        jaeger_mod.MOCK_MODE = old_mock
    except Exception as e:
        results["JaegerTraceListTool"] = False
        _check("JaegerTraceListTool", False, str(e)[:60])

    # ── CMDBLookupTool ──────────────────────────────────────────
    try:
        from monitoring_llm.tools.tools import CMDBLookupTool
        from monitoring_llm.cmdb.database import CMDB, seed_banksystem_16

        cmdb = CMDB(":memory:")
        seed_banksystem_16(cmdb)

        tool   = CMDBLookupTool()
        result = tool._run("192.168.0.140")          # ← 실제 Web IP
        data   = json.loads(result)

        # 응답 구조: {"ok": true, "servers": [{"hostname": "dev-masternode", ...}]}
        servers = data.get("servers", [])
        ok_flag = (
            data.get("ok")
            and len(servers) > 0
            and servers[0].get("hostname") == "dev-masternode"  # ← 실제 hostname
        )
        results["CMDBLookupTool"] = ok_flag
        _check("CMDBLookupTool", ok_flag,
               servers[0].get("hostname", "") if ok_flag else str(data)[:80])
    except Exception as e:
        results["CMDBLookupTool"] = False
        _check("CMDBLookupTool", False, str(e)[:60])

    return results


# ── 최종 보고 ─────────────────────────────────────────────────────
def print_report(all_results: dict[str, dict]) -> bool:
    print(f"\n{'='*55}")
    print("Phase 2 완료 조건 체크")
    print(f"{'='*55}")
    total_ok = total_all = 0
    for section, results in all_results.items():
        ok_count   = sum(1 for v in results.values() if v)
        total_ok  += ok_count
        total_all += len(results)
        icon = OK if ok_count == len(results) else (WARN if ok_count > 0 else FAIL)
        print(f"  {icon} {section}: {ok_count}/{len(results)}")

    print(f"\n  전체: {total_ok}/{total_all} 통과")
    passed = total_ok == total_all

    if passed:
        print(f"\n  {OK} Phase 2 완료 — Phase 3 진행 가능")
    else:
        print(f"\n  {FAIL} 실패 항목 확인 후 재시도.")
        print(f"       MOCK_MODE=true python -m monitoring_llm.tools.connection_test")

    return passed


# ── 도구 레지스트리 ────────────────────────────────────────────────
def get_all_tools():
    """LangGraph 에이전트에 주입할 도구 전체 목록"""
    from monitoring_llm.tools.prometheus_tool import PrometheusQueryTool
    from monitoring_llm.tools.loki_tool       import LokiQueryTool
    from monitoring_llm.tools.jaeger_tool     import JaegerTraceListTool, JaegerTraceDetailTool
    from monitoring_llm.tools.tools           import CMDBLookupTool, IncidentHistoryTool

    return [
        PrometheusQueryTool(),
        LokiQueryTool(),
        JaegerTraceListTool(),
        JaegerTraceDetailTool(),
        CMDBLookupTool(),
        IncidentHistoryTool(),
    ]


# ── main ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"{'='*55}")
    print("Phase 2 도구 레이어 연결 테스트")
    print(f"MOCK_MODE={MOCK_MODE}")
    print(f"PROMETHEUS_URL={PROMETHEUS_URL}")
    print(f"LOKI_URL={LOKI_URL}")
    print(f"JAEGER_URL={JAEGER_URL}")
    print(f"{'='*55}")

    all_results = {}

    if MOCK_MODE:
        print(f"\n{WARN} Mock 모드 — 실제 서버 연결 없이 Tool 동작만 확인")
        all_results["Tool Mock 테스트"] = test_tools_mock()
    else:
        all_results["서비스 연결"]        = test_connections()
        all_results["Prometheus 메트릭"]  = test_prometheus_metrics()
        all_results["Loki 스트림"]        = test_loki_streams()
        all_results["Jaeger 서비스"]      = test_jaeger_services()
        all_results["Tool Mock 테스트"]   = test_tools_mock()

    print_report(all_results)
