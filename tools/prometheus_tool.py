"""
tools/prometheus_tool.py — Prometheus 메트릭 조회 도구
──────────────────────────────────────────────────────
BankSystem_16 실제 exporter 기준 PromQL 템플릿:
  Web  (Linux)   : node_exporter + apache_exporter(:9117)
  WAS  (Windows) : jmx_exporter(:9090)  [Tomcat JVM]
  DB   (Windows) : mysqld_exporter(:9104) + windows_exporter

독립 실행 테스트:
    MOCK_MODE=true python -m monitoring_llm.tools.prometheus_tool
    PROMETHEUS_URL=http://localhost:9090 python -m monitoring_llm.tools.prometheus_tool
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
    PROMETHEUS_URL, MOCK_MODE, safe_get, ok, err, ts_to_str
)

# ── PromQL 템플릿 (실제 exporter 메트릭명 기준) ───────────────────
PROMQL: dict[str, dict[str, str]] = {

    "web": {
        # node_exporter (Linux)
        "cpu_util_pct": (
            '100 - (avg by(instance) ('
            '  rate(node_cpu_seconds_total{mode="idle",instance="{instance}"}[1m])'
            ') * 100)'
        ),
        "mem_used_pct": (
            '100 * (1 - ('
            '  node_memory_MemAvailable_bytes{instance="{instance}"}'
            '  / node_memory_MemTotal_bytes{instance="{instance}"}'
            '))'
        ),
        "disk_used_pct": (
            '100 * (1 - ('
            '  node_filesystem_avail_bytes{instance="{instance}",mountpoint="/"}'
            '  / node_filesystem_size_bytes{instance="{instance}",mountpoint="/"}'
            '))'
        ),
        # apache_exporter
        "req_per_sec": (
            'rate(apache_accesses_total{instance="{instance}"}[1m])'
        ),
        "bytes_per_sec": (
            'rate(apache_sent_kilobytes_total{instance="{instance}"}[1m]) * 1024'
        ),
        "workers_busy": (
            'apache_workers{instance="{instance}",state="busy"}'
        ),
        "workers_idle": (
            'apache_workers{instance="{instance}",state="idle"}'
        ),
    },

    "was": {
        # jmx_exporter (Tomcat JVM) — Windows
        "heap_used_pct": (
            '100 * ('
            '  jvm_memory_bytes_used{instance="{instance}",area="heap"}'
            '  / jvm_memory_bytes_max{instance="{instance}",area="heap"}'
            ')'
        ),
        "heap_used_mb": (
            'jvm_memory_bytes_used{instance="{instance}",area="heap"} / 1048576'
        ),
        "threads_active": (
            'jvm_threads_current{instance="{instance}"}'
        ),
        "threads_daemon": (
            'jvm_threads_daemon_current{instance="{instance}"}'
        ),
        "gc_time_rate": (
            'rate(jvm_gc_collection_seconds_sum{instance="{instance}"}[1m])'
        ),
        "gc_count_rate": (
            'rate(jvm_gc_collection_seconds_count{instance="{instance}"}[1m])'
        ),
        "req_per_sec": (
            'rate(tomcat_requestcount_total{instance="{instance}"}[1m])'
        ),
        "error_per_sec": (
            'rate(tomcat_errorcount_total{instance="{instance}"}[1m])'
        ),
        "req_processing_time_ms": (
            'rate(tomcat_processingtime_total{instance="{instance}"}[1m])'
            ' / rate(tomcat_requestcount_total{instance="{instance}"}[1m])'
        ),
    },

    "db": {
        # mysqld_exporter — Windows
        "connections_used": (
            'mysql_global_status_threads_connected{instance="{instance}"}'
        ),
        "connections_max_pct": (
            '100 * mysql_global_status_threads_connected{instance="{instance}"}'
            ' / mysql_global_variables_max_connections{instance="{instance}"}'
        ),
        "queries_per_sec": (
            'rate(mysql_global_status_queries{instance="{instance}"}[1m])'
        ),
        "slow_queries_per_sec": (
            'rate(mysql_global_status_slow_queries{instance="{instance}"}[1m])'
        ),
        "innodb_bp_hit_rate": (
            '100 * ('
            '  rate(mysql_global_status_innodb_buffer_pool_reads{instance="{instance}"}[1m])'  # data 없음 → hit
            '  / (rate(mysql_global_status_innodb_buffer_pool_read_requests{instance="{instance}"}[1m]) + 0.001)'
            ')'
        ),
        "threads_running": (
            'mysql_global_status_threads_running{instance="{instance}"}'
        ),
        "replication_lag_sec": (
            'mysql_slave_status_seconds_behind_master{instance="{instance}"}'
        ),
    },
}


# ── Tier 매핑 (MicroRCA METRIC_TIERS 연동) ───────────────────────
TIER1_METRICS: dict[str, list[str]] = {
    "web": ["cpu_util_pct", "mem_used_pct", "req_per_sec", "workers_busy"],
    "was": ["heap_used_pct", "threads_active", "gc_time_rate", "req_per_sec", "error_per_sec"],
    "db":  ["connections_used", "slow_queries_per_sec", "threads_running", "queries_per_sec"],
}


# ── Mock 데이터 ────────────────────────────────────────────────────
def _mock_metrics(role: str, server: str) -> dict:
    templates = {
        "web": {
            "cpu_util_pct":  {"avg": 34.2, "max": 78.5, "min": 12.1, "p95": 71.3},
            "mem_used_pct":  {"avg": 52.1, "max": 63.8, "min": 49.4, "p95": 61.2},
            "disk_used_pct": {"avg": 68.0, "max": 68.3, "min": 67.8, "p95": 68.2},
            "req_per_sec":   {"avg": 42.7, "max": 120.0, "min": 8.3,  "p95": 98.4},
            "workers_busy":  {"avg": 12.4, "max": 48.0,  "min": 2.0,  "p95": 38.0},
            "workers_idle":  {"avg": 148.0,"max": 198.0, "min": 102.0,"p95": 185.0},
        },
        "was": {
            "heap_used_pct": {"avg": 71.3, "max": 91.4, "min": 62.0, "p95": 88.2},
            "heap_used_mb":  {"avg": 2891, "max": 3710,  "min": 2519, "p95": 3585},
            "threads_active":{"avg": 87.0, "max": 198.0, "min": 42.0, "p95": 178.0},
            "gc_time_rate":  {"avg": 0.023,"max": 0.18,  "min": 0.001,"p95": 0.12},
            "req_per_sec":   {"avg": 38.2, "max": 112.0, "min": 6.1,  "p95": 94.3},
            "error_per_sec": {"avg": 0.12, "max": 3.8,   "min": 0.0,  "p95": 1.4},
        },
        "db": {
            "connections_used":      {"avg": 48.0,  "max": 112.0, "min": 18.0, "p95": 98.0},
            "connections_max_pct":   {"avg": 32.0,  "max": 74.7,  "min": 12.0, "p95": 65.3},
            "queries_per_sec":       {"avg": 284.0, "max": 891.0, "min": 42.0, "p95": 712.0},
            "slow_queries_per_sec":  {"avg": 0.8,   "max": 12.4,  "min": 0.0,  "p95": 8.2},
            "threads_running":       {"avg": 6.2,   "max": 38.0,  "min": 1.0,  "p95": 28.0},
            "innodb_bp_hit_rate":    {"avg": 96.8,  "max": 99.1,  "min": 91.2, "p95": 98.7},
        },
    }
    return templates.get(role, templates["web"])


# ── PromQL 실행 ────────────────────────────────────────────────────
def _run_range_query(
    query: str, start_ts: int, end_ts: int, step: str = "1m"
) -> Optional[dict]:
    """
    단일 PromQL range_query 실행.
    반환: {"avg", "max", "min", "p95", "samples"} 또는 None (데이터 없음)
    """
    result = safe_get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={"query": query, "start": start_ts, "end": end_ts, "step": step},
        timeout=12,
    )
    if not result["ok"]:
        return {"error": result["error"]}

    data = result["data"]
    if data.get("status") != "success":
        return {"error": data.get("error", "unknown prometheus error")}

    series = data["data"]["result"]
    if not series:
        return None  # 데이터 없음 (exporter 미수집 가능성)

    nums = []
    for s in series:
        for _, v in s["values"]:
            try:
                f = float(v)
                if f == f and f != float("inf"):  # NaN, Inf 제외
                    nums.append(f)
            except (ValueError, TypeError):
                pass

    if not nums:
        return None

    nums_sorted = sorted(nums)
    p95_idx = int(len(nums_sorted) * 0.95)
    return {
        "avg": round(sum(nums) / len(nums), 3),
        "max": round(max(nums), 3),
        "min": round(min(nums), 3),
        "p95": round(nums_sorted[p95_idx], 3),
        "samples": len(nums),
    }


# ── Tool 입력 스키마 ───────────────────────────────────────────────
class PrometheusInput(BaseModel):
    server_hostname: str = Field(description="서버 hostname (예: web01-bank16)")
    server_role: str     = Field(description="서버 역할: web | was | db")
    prometheus_instance: str = Field(
        description="Prometheus instance 레이블 값 (예: 192.168.16.10:9117)"
    )
    start_ts: int  = Field(description="시작 Unix timestamp")
    end_ts: int    = Field(description="종료 Unix timestamp")
    step: str      = Field(default="1m", description="집계 간격: 30s | 1m | 5m")
    tier1_only: bool = Field(default=False, description="Tier 1 핵심 메트릭만 조회")


# ── BaseTool 구현 ──────────────────────────────────────────────────
class PrometheusQueryTool(BaseTool):
    name: str = "prometheus_query"
    description: str = (
        "서버의 성능 메트릭을 Prometheus에서 조회한다. "
        "CPU, 메모리, 요청수, 응답시간, GC, DB 연결 등을 포함. "
        "서버 hostname, 역할(web/was/db), prometheus_instance, 시간 범위가 필요하다."
    )
    args_schema: Type[BaseModel] = PrometheusInput

    def _run(
        self,
        server_hostname: str,
        server_role: str,
        prometheus_instance: str,
        start_ts: int,
        end_ts: int,
        step: str = "1m",
        tier1_only: bool = False,
    ) -> str:
        role = server_role.lower()
        queries = PROMQL.get(role, PROMQL["web"])

        # tier1_only 이면 핵심 메트릭만
        if tier1_only:
            tier1 = TIER1_METRICS.get(role, list(queries.keys())[:4])
            queries = {k: v for k, v in queries.items() if k in tier1}

        # Mock 모드
        if MOCK_MODE:
            metrics = _mock_metrics(role, server_hostname)
            return ok({
                "server": server_hostname,
                "role": role,
                "instance": prometheus_instance,
                "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                "step": step,
                "mock": True,
                "metrics": {k: v for k, v in metrics.items() if k in queries},
            })

        # 실제 쿼리 실행
        metrics = {}
        errors = {}
        for metric_name, query_tpl in queries.items():
            query = query_tpl.replace("{instance}", prometheus_instance)
            result = _run_range_query(query, start_ts, end_ts, step)
            if result is None:
                pass  # 데이터 없음 — 조용히 스킵
            elif "error" in result:
                errors[metric_name] = result["error"]
            else:
                metrics[metric_name] = result

        output = {
            "server": server_hostname,
            "role": role,
            "instance": prometheus_instance,
            "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            "step": step,
            "metrics": metrics,
        }
        if errors:
            output["query_errors"] = errors

        # 이상 징후 자동 플래그
        flags = _detect_anomalies(role, metrics)
        if flags:
            output["anomaly_flags"] = flags

        return ok(output)

    async def _arun(self, **kwargs) -> str:
        return self._run(**kwargs)


def _detect_anomalies(role: str, metrics: dict) -> list[str]:
    """수집된 메트릭에서 임계값 초과 항목 자동 감지"""
    flags = []
    thresholds = {
        "web":  {"cpu_util_pct": 80, "mem_used_pct": 85, "disk_used_pct": 90,
                 "workers_busy": 180},
        "was":  {"heap_used_pct": 85, "threads_active": 190, "gc_time_rate": 0.1,
                 "error_per_sec": 1.0},
        "db":   {"connections_max_pct": 70, "slow_queries_per_sec": 5.0,
                 "threads_running": 30},
    }
    limits = thresholds.get(role, {})
    for metric, limit in limits.items():
        val = metrics.get(metric, {})
        if isinstance(val, dict) and val.get("max", 0) > limit:
            flags.append(
                f"{metric} 임계값 초과: max={val['max']} (기준={limit})"
            )
    return flags


# ── 독립 실행 테스트 ──────────────────────────────────────────────
if __name__ == "__main__":
    import os, sys
    from monitoring_llm.nlp.time_parser import default_range

    print(f"{'='*55}")
    print(f"Prometheus Tool 테스트 — MOCK_MODE={MOCK_MODE}")
    print(f"{'='*55}")

    tr = default_range(60)
    tool = PrometheusQueryTool()

    test_cases = [
        ("web01-bank16", "web", "192.168.16.10:9117"),
        ("was01-bank16", "was", "192.168.16.20:9090"),
        ("db01-bank16",  "db",  "192.168.16.30:9104"),
    ]

    for hostname, role, instance in test_cases:
        print(f"\n[{hostname}] — role={role}")
        result = tool._run(
            server_hostname=hostname,
            server_role=role,
            prometheus_instance=instance,
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
            step="1m",
            tier1_only=True,
        )
        data = json.loads(result)
        if data.get("ok"):
            metrics = data.get("metrics", {})
            for name, val in metrics.items():
                if isinstance(val, dict) and "avg" in val:
                    print(f"  {name:<30} avg={val['avg']:<8} max={val['max']:<8} p95={val['p95']}")
            if data.get("anomaly_flags"):
                print(f"  ⚠ 이상 감지: {data['anomaly_flags']}")
        else:
            print(f"  ERROR: {data.get('error')}")
