"""
tools/connection_test.py — 연결 상태 진단 + 도구 레지스트리
─────────────────────────────────────────────────────────────
Phase 2 완료 조건 확인:
  1. 각 서비스(Prometheus/Loki/Jaeger) HTTP 연결 상태
  2. 실제 BankSystem_16 exporter 메트릭 존재 확인
  3. LangChain Tool 스키마 정상 초기화
  4. Mock 모드로 end-to-end 흐름 확인

실행:
    # 연결 테스트 (Mock 없이 실제 서버 대상)
    python -m monitoring_llm.tools.connection_test

    # Mock 모드로 전체 흐름 테스트
    MOCK_MODE=true python -m monitoring_llm.tools.connection_test
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


# ── ANSI 컬러 ─────────────────────────────────────────────────────
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

    checks = [
        ("Prometheus", f"{PROMETHEUS_URL}/-/healthy",   "Prometheus is Healthy"),
        ("Loki",       f"{LOKI_URL}/ready",              "ready"),
        ("Jaeger",     f"{JAEGER_URL}/api/services",     None),
        ("Alertmanager", f"{ALERTMANAGER_URL}/-/healthy", "OK"),
    ]

    for name, url, expected in checks:
        t0 = time.time()
        r = safe_get(url, timeout=3)
        elapsed = round((time.time() - t0) * 1000)
        if r["ok"]:
            body = str(r.get("data", ""))
            ok_flag = expected is None or expected in body
            results[name] = ok_flag
            _check(f"{name:<15} {elapsed}ms", ok_flag,
                   "" if ok_flag else f"예상 응답 없음: {body[:50]}")
        else:
            results[name] = False
            _check(f"{name:<15}", False, r["error"][:60])

    return results


# ── 2. Prometheus Exporter 메트릭 확인 ───────────────────────────
def test_prometheus_metrics() -> dict[str, bool]:
    print("\n[2] Prometheus Exporter 메트릭 수집 확인")
    results = {}

    # BankSystem_16 exporter instance 목록 (CMDB 기준)
    instances = {
        "web (apache_exporter)":  ("apache_accesses_total", "192.168.16.10:9117"),
        "web (node_exporter)":    ("node_cpu_seconds_total", "192.168.16.10:9100"),
        "was (jmx_exporter)":     ("jvm_memory_bytes_used", "192.168.16.20:9090"),
        "db  (mysqld_exporter)":  ("mysql_up", "192.168.16.30:9104"),
    }

    for label, (metric, instance) in instances.items():
        query = f'{metric}{{instance="{instance}"}}'
        r = safe_get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5,
        )
        if not r["ok"]:
            results[label] = False
            _check(f"{label}", False, r["error"][:50])
            continue

        data = r["data"]
        has_data = (
            data.get("status") == "success"
            and len(data.get("data", {}).get("result", [])) > 0
        )
        results[label] = has_data
        val = ""
        if has_data:
            val = data["data"]["result"][0]["value"][1]
            val = f"→ {float(val):.3f}"
        _check(f"{label:<35} {metric}", has_data, val)

    return results


# ── 3. Loki 로그 스트림 확인 ──────────────────────────────────────
def test_loki_streams() -> dict[str, bool]:
    print("\n[3] Loki 로그 스트림 확인 (최근 5분)")
    results = {}
    import time as t_mod
    now = int(t_mod.time())
    start = now - 300  # 5분

    # hosts = ["web01-bank16", "web02-bank16", "was01-bank16", "was02-bank16",
    #          "db01-bank16",  "db02-bank16"]
    hosts = ["web-bank16", "was-bank16", "db-bank16"]

    for host in hosts:
        logql = f'{{host="{host}"}}'
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
            results[host] = False
            _check(f"{host}", False, r["error"][:50])
            continue

        data = r["data"]
        has_logs = (
            data.get("status") == "success"
            and len(data.get("data", {}).get("result", [])) > 0
        )
        results[host] = has_logs
        _check(f"{host}", has_logs, "로그 없음 (host 레이블 확인 필요)" if not has_logs else "")

    return results


# ── 4. Jaeger 서비스 확인 ─────────────────────────────────────────
def test_jaeger_services() -> dict[str, bool]:
    print("\n[4] Jaeger 서비스 목록 확인")
    services = list_jaeger_services()
    results = {}

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

    # Prometheus
    try:
        tool = PrometheusQueryTool()
        assert isinstance(tool, BaseTool), "BaseTool 아님"
        os.environ["MOCK_MODE"] = "true"

        import monitoring_llm.tools.base as base_mod
        import monitoring_llm.tools.prometheus_tool as prom_mod
        old_mock = base_mod.MOCK_MODE
        base_mod.MOCK_MODE = True
        prom_mod.MOCK_MODE = True

        result = tool._run(
            server_hostname="web01-bank16",
            server_role="web",
            prometheus_instance="192.168.16.10:9117",
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
        )
        data = json.loads(result)
        ok_flag = data.get("ok") and bool(data.get("metrics"))
        results["PrometheusQueryTool"] = ok_flag
        _check("PrometheusQueryTool", ok_flag,
               f"메트릭 {len(data.get('metrics', {}))}개" if ok_flag else str(data))

        base_mod.MOCK_MODE = old_mock
        prom_mod.MOCK_MODE = old_mock
    except Exception as e:
        results["PrometheusQueryTool"] = False
        _check("PrometheusQueryTool", False, str(e)[:60])

    # Loki
    try:
        import monitoring_llm.tools.loki_tool as loki_mod
        loki_mod.MOCK_MODE = True
        tool = LokiQueryTool()
        result = tool._run(
            loki_host="web01-bank16",
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
        )
        data = json.loads(result)
        ok_flag = data.get("ok") and data.get("total", 0) > 0
        results["LokiQueryTool"] = ok_flag
        _check("LokiQueryTool", ok_flag,
               f"로그 {data.get('total', 0)}건" if ok_flag else str(data))
        loki_mod.MOCK_MODE = old_mock
    except Exception as e:
        results["LokiQueryTool"] = False
        _check("LokiQueryTool", False, str(e)[:60])

    # Jaeger
    try:
        import monitoring_llm.tools.jaeger_tool as jaeger_mod
        jaeger_mod.MOCK_MODE = True
        tool = JaegerTraceListTool()
        result = tool._run(
            service_name="was-service",
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
        )
        data = json.loads(result)
        ok_flag = data.get("ok") and data.get("total_traces", 0) > 0
        results["JaegerTraceListTool"] = ok_flag
        _check("JaegerTraceListTool", ok_flag,
               f"트레이스 {data.get('total_traces', 0)}건" if ok_flag else str(data))
        jaeger_mod.MOCK_MODE = old_mock
    except Exception as e:
        results["JaegerTraceListTool"] = False
        _check("JaegerTraceListTool", False, str(e)[:60])

    # CMDB
    try:
        from monitoring_llm.tools.tools import CMDBLookupTool
        from monitoring_llm.cmdb.database import CMDB, seed_banksystem_16
        cmdb = CMDB("/tmp/test_phase2.db")
        seed_banksystem_16(cmdb)
        tool = CMDBLookupTool()
        result = tool._run("192.168.16.10")
        data = json.loads(result)
        ok_flag = data.get("ok") and data.get("hostname") == "web01-bank16"
        results["CMDBLookupTool"] = ok_flag
        _check("CMDBLookupTool", ok_flag,
               data.get("hostname", "") if ok_flag else str(data))
    except Exception as e:
        results["CMDBLookupTool"] = False
        _check("CMDBLookupTool", False, str(e)[:60])

    return results


# ── 최종 보고 ─────────────────────────────────────────────────────
def print_report(all_results: dict[str, dict]) -> bool:
    print(f"\n{'='*55}")
    print("Phase 2 완료 조건 체크")
    print(f"{'='*55}")
    total_ok = 0
    total_all = 0
    for section, results in all_results.items():
        ok_count = sum(1 for v in results.values() if v)
        total_ok  += ok_count
        total_all += len(results)
        icon = OK if ok_count == len(results) else (WARN if ok_count > 0 else FAIL)
        print(f"  {icon} {section}: {ok_count}/{len(results)}")

    print(f"\n  전체: {total_ok}/{total_all} 통과")
    passed = total_ok == total_all

    if passed:
        print(f"\n  {OK} Phase 2 완료 조건 충족 — Phase 3 (NL 처리 파이프라인) 진행 가능")
    else:
        print(f"\n  {FAIL} 실패 항목 확인 후 재시도. Mock 모드로 우선 진행 가능:")
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
    print(f"Phase 2 도구 레이어 연결 테스트")
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
        all_results["서비스 연결"]     = test_connections()
        all_results["Prometheus 메트릭"] = test_prometheus_metrics()
        all_results["Loki 스트림"]      = test_loki_streams()
        all_results["Jaeger 서비스"]    = test_jaeger_services()
        all_results["Tool Mock 테스트"] = test_tools_mock()

    print_report(all_results)
