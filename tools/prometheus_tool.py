"""
tools/prometheus_tool.py — Prometheus 메트릭 조회 도구  [수정본]
────────────────────────────────────────────────────────────────
BankSystem_16 실제 수집 구조:
  Web  (Linux)   : OTel hostmetrics  job="bank-web-hostmetrics"
  WAS  (Windows) : OTel hostmetrics  job="bank-was-hostmetrics"
                   OTel Java agent   job="bank-was-app"
  DB   (Windows) : OTel hostmetrics  job="bank-db-hostmetrics"
                   MySQL exporter    job="bank-db-hostmetrics" (동일 job)

변경 요약:
  - prometheus_instance(IP:port) → prometheus_job(job 레이블)
  - PROMQL instance= 필터 → job= 필터
  - 메트릭명 node_exporter/jmx → OTel hostmetrics / OTel Java agent 포맷
  - WAS 앱 메트릭: http_server_request_duration_seconds_* 기반

독립 실행 테스트:
    MOCK_MODE=true python -m monitoring_llm.tools.prometheus_tool
    PROMETHEUS_URL=http://192.168.0.41:9092 python -m monitoring_llm.tools.prometheus_tool
"""

import asyncio
import json
from datetime import datetime
from typing import Optional, Type

from pydantic import BaseModel, Field

try:
    from langchain_core.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from monitoring_llm.tools.base import (
    MOCK_MODE,
    PROMETHEUS_URL,
    err,
    ok,
    safe_get,
    ts_to_str,
)

# ── 노드 설정 (job + host_name 매핑) ──────────────────────────────
NODE_CONFIG = {
    "dev-masternode": {
        "tier": "web",
        "job": "bank-web-hostmetrics",
        "app_job": None,
        "host_name": "dev-masternode",
        "server_role": "web",
    },
    "ONTUNETEST2": {
        "tier": "was",
        "job": "bank-was-hostmetrics",
        "app_job": "bank-was-app",  # JVM/HTTP 앱 메트릭
        "host_name": "ONTUNETEST2",
        "server_role": "was",
    },
    "DESKTOP-H0M89JB": {
        "tier": "db",
        "job": "bank-db-hostmetrics",
        "app_job": None,
        "host_name": "DESKTOP-H0M89JB",
        "server_role": "db",
    },
}

# ── PromQL 템플릿 (OTel 포맷 기준, {job} 치환) ────────────────────
PROMQL: dict[str, dict[str, str]] = {
    "web": {
        # ── OTel hostmetrics (Linux) ──
        # sum() 필수 — CPU코어별·state별로 series가 분리되어 있어
        # sum 없이 나누면 label 매칭 실패 → 0에 가까운 잘못된 값 반환
        "cpu_util_pct": (
            "100 * (1 - ("
            "  sum(rate(system_cpu_time_seconds_total"
            '{job="{job}", state="idle"}[1m]))'
            "  / (sum(rate(system_cpu_time_seconds_total"
            '{job="{job}"}[1m])) + 0.001)'
            "))"
        ),
        "mem_used_pct": (
            # state별 series가 다른 label set → sum()으로 scalar 변환 후 사칙연산
            "100 * ("
            '  sum(system_memory_usage_bytes{job="{job}", state="used"})'
            "  / ("
            '    sum(system_memory_usage_bytes{job="{job}", state="used"})'
            '    + sum(system_memory_usage_bytes{job="{job}", state="free"})'
            '    + sum(system_memory_usage_bytes{job="{job}", state="buffered"})'
            '    + sum(system_memory_usage_bytes{job="{job}", state="cached"}) + 1'
            "  )"
            ")"
        ),
        "disk_used_pct": (
            # mountpoint=\"/\" 로 루트만, device/type 레이블 차이 → sum()
            "100 * ("
            '  sum(system_filesystem_usage_bytes{job="{job}", mountpoint="/", state="used"})'
            "  / ("
            '    sum(system_filesystem_usage_bytes{job="{job}", mountpoint="/", state="used"})'
            '    + sum(system_filesystem_usage_bytes{job="{job}", mountpoint="/", state="free"}) + 1'
            "  )"
            ")"
        ),
        "net_recv_bytes_sec": (
            'rate(system_network_io_bytes_total{job="{job}", direction="receive"}[1m])'
        ),
        "net_sent_bytes_sec": (
            'rate(system_network_io_bytes_total{job="{job}", direction="transmit"}[1m])'
        ),
        # Apache — 수집 여부 확인 후 사용 (현재 미확인)
        # "req_per_sec":  'rate(apache_accesses_total{job="{job}"}[1m])',
        # "workers_busy": 'apache_workers{job="{job}", state="busy"}',
    },
    "was": {
        # ── OTel hostmetrics (Windows) ──
        "cpu_util_pct": (
            "100 * (1 - ("
            "  sum(rate(system_cpu_time_seconds_total"
            '{job="{job}", state="idle"}[1m]))'
            "  / (sum(rate(system_cpu_time_seconds_total"
            '{job="{job}"}[1m])) + 0.001)'
            "))"
        ),
        "mem_used_pct": (
            "100 * ("
            '  sum(system_memory_usage_bytes{job="{job}", state="used"})'
            "  / ("
            '    sum(system_memory_usage_bytes{job="{job}", state="used"})'
            '    + sum(system_memory_usage_bytes{job="{job}", state="free"}) + 1'
            "  )"
            ")"
        ),
        # JVM 힙 — GC 직후 기준값 (jvm_memory_used_bytes 없음, after_last_gc 사용)
        # 풀별(Eden/Survivor/OldGen)로 분리되어 있어 sum() 필수
        "heap_used_pct": (
            "100 * ("
            "  sum(jvm_memory_used_after_last_gc_bytes"
            '{job="{app_job}", jvm_memory_type="heap"})'
            "  / ("
            "    sum(jvm_memory_committed_bytes"
            '{job="{app_job}", jvm_memory_type="heap"}) + 1'
            "  )"
            ")"
        ),
        "heap_used_mb": (
            "sum(jvm_memory_used_after_last_gc_bytes"
            '{job="{app_job}", jvm_memory_type="heap"}) / 1048576'
        ),
        # 스레드 — state별 분리(waiting/runnable/blocked 등)이므로 sum() 필수
        "threads_active": ('sum(jvm_thread_count{job="{app_job}"})'),
        "threads_runnable": (
            'sum(jvm_thread_count{job="{app_job}", jvm_thread_state="runnable"})'
        ),
        # GC — histogram 기반, [5m] 윈도우
        "gc_duration_rate": (
            'sum(rate(jvm_gc_duration_seconds_sum{job="{app_job}"}[5m]))'
        ),
        "gc_count_rate": (
            'sum(rate(jvm_gc_duration_seconds_count{job="{app_job}"}[5m]))'
        ),
        # HTTP — OTel HTTP 시멘틱, [5m] 윈도우 사용 (scrape 간격 불일치 방지)
        # route별로 series가 분리되어 있어 sum() 으로 전체 합산
        "req_per_sec": (
            "sum(rate(http_server_request_duration_seconds_count"
            '{job="{app_job}"}[5m]))'
        ),
        "error_per_sec": (
            "sum(rate(http_server_request_duration_seconds_count"
            '{job="{app_job}", http_response_status_code=~"5.."}[5m]))'
        ),
        # HTTP 응답시간 평균 (ms) — sum으로 route 전체 가중평균
        "resp_time_ms": (
            "1000 * ("
            "  sum(rate(http_server_request_duration_seconds_sum"
            '{job="{app_job}"}[5m]))'
            "  / (sum(rate(http_server_request_duration_seconds_count"
            '{job="{app_job}"}[5m])) + 0.001)'
            ")"
        ),
        # Tomcat 스레드풀
        "tomcat_thread_limit": ('tomcat_thread_limit{job="{app_job}"}'),
    },
    "db": {
        # ── OTel hostmetrics (Windows) ──
        "cpu_util_pct": (
            "100 * (1 - ("
            "  sum(rate(system_cpu_time_seconds_total"
            '{job="{job}", state="idle"}[1m]))'
            "  / (sum(rate(system_cpu_time_seconds_total"
            '{job="{job}"}[1m])) + 0.001)'
            "))"
        ),
        "mem_used_pct": (
            "100 * ("
            '  sum(system_memory_usage_bytes{job="{job}", state="used"})'
            "  / ("
            '    sum(system_memory_usage_bytes{job="{job}", state="used"})'
            '    + sum(system_memory_usage_bytes{job="{job}", state="free"}) + 1'
            "  )"
            ")"
        ),
        "disk_used_pct": (
            "100 * ("
            '  sum(system_filesystem_usage_bytes{job="{job}", state="used"})'
            "  / ("
            '    sum(system_filesystem_usage_bytes{job="{job}", state="used"})'
            '    + sum(system_filesystem_usage_bytes{job="{job}", state="free"}) + 1'
            "  )"
            ")"
        ),
        "connections_used": ('mysql_global_status_threads_connected{job="{job}"}'),
        "connections_max_pct": (
            '100 * mysql_global_status_threads_connected{job="{job}"}'
            ' / (mysql_global_variables_max_connections{job="{job}"} + 1)'
        ),
        "queries_per_sec": ('rate(mysql_global_status_queries{job="{job}"}[1m])'),
        "slow_queries_per_sec": (
            'rate(mysql_global_status_slow_queries{job="{job}"}[1m])'
        ),
        "threads_running": ('mysql_global_status_threads_running{job="{job}"}'),
        "innodb_bp_hit_rate": (
            "100 * (1 - ("
            '  rate(mysql_global_status_innodb_buffer_pool_reads{job="{job}"}[1m])'
            "  / ("
            '    rate(mysql_global_status_innodb_buffer_pool_read_requests{job="{job}"}[1m]) + 0.001'
            "  )"
            "))"
        ),
    },
}

# ── Tier 매핑 ────────────────────────────────────────────────────
TIER1_METRICS: dict[str, list[str]] = {
    "web": ["cpu_util_pct", "mem_used_pct", "net_recv_bytes_sec", "disk_used_pct"],
    "was": [
        "cpu_util_pct",
        "mem_used_pct",
        "heap_used_pct",
        "threads_active",
        "req_per_sec",
        "error_per_sec",
        "resp_time_ms",
    ],
    # MySQL receiver 미수집 — 호스트 메트릭만 운영 (TODO: OTel MySQL receiver 추가 후 복원)
    "db": [
        "cpu_util_pct",
        "mem_used_pct",
        "disk_used_pct",
        # "connections_used", "slow_queries_per_sec", "threads_running",  # MySQL 수집 후 활성화
    ],
}


# ── Mock 데이터 ──────────────────────────────────────────────────
def _mock_metrics(role: str, server: str) -> dict:
    templates = {
        "web": {
            "cpu_util_pct": {"avg": 34.2, "max": 78.5, "min": 12.1, "p95": 71.3},
            "mem_used_pct": {"avg": 52.1, "max": 63.8, "min": 49.4, "p95": 61.2},
            "disk_used_pct": {"avg": 68.0, "max": 68.3, "min": 67.8, "p95": 68.2},
            "net_recv_bytes_sec": {
                "avg": 1_200_000,
                "max": 8_500_000,
                "min": 200_000,
                "p95": 6_800_000,
            },
        },
        "was": {
            "cpu_util_pct": {"avg": 41.0, "max": 88.0, "min": 10.0, "p95": 82.0},
            "mem_used_pct": {"avg": 61.0, "max": 79.0, "min": 55.0, "p95": 75.0},
            "heap_used_pct": {"avg": 71.3, "max": 91.4, "min": 62.0, "p95": 88.2},
            "heap_used_mb": {"avg": 2891, "max": 3710, "min": 2519, "p95": 3585},
            "threads_active": {"avg": 87.0, "max": 198.0, "min": 42.0, "p95": 178.0},
            "gc_duration_rate": {"avg": 0.023, "max": 0.18, "min": 0.001, "p95": 0.12},
            "req_per_sec": {"avg": 38.2, "max": 112.0, "min": 6.1, "p95": 94.3},
            "error_per_sec": {"avg": 0.12, "max": 3.8, "min": 0.0, "p95": 1.4},
            "resp_time_ms": {"avg": 45.0, "max": 320.0, "min": 8.0, "p95": 210.0},
        },
        "db": {
            "cpu_util_pct": {"avg": 28.0, "max": 72.0, "min": 5.0, "p95": 65.0},
            "mem_used_pct": {"avg": 55.0, "max": 70.0, "min": 50.0, "p95": 68.0},
            "connections_used": {"avg": 48.0, "max": 112.0, "min": 18.0, "p95": 98.0},
            "connections_max_pct": {"avg": 32.0, "max": 74.7, "min": 12.0, "p95": 65.3},
            "queries_per_sec": {"avg": 284.0, "max": 891.0, "min": 42.0, "p95": 712.0},
            "slow_queries_per_sec": {"avg": 0.8, "max": 12.4, "min": 0.0, "p95": 8.2},
            "threads_running": {"avg": 6.2, "max": 38.0, "min": 1.0, "p95": 28.0},
            "innodb_bp_hit_rate": {"avg": 96.8, "max": 99.1, "min": 91.2, "p95": 98.7},
        },
    }
    return templates.get(role, templates["web"])


# ── PromQL 실행 ──────────────────────────────────────────────────
def _run_range_query(
    query: str, start_ts: int, end_ts: int, step: str = "1m"
) -> Optional[dict]:
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
        return None

    nums = []
    for s in series:
        for _, v in s["values"]:
            try:
                f = float(v)
                if f == f and f != float("inf"):
                    nums.append(f)
            except (ValueError, TypeError):
                pass

    if not nums:
        return None

    nums_sorted = sorted(nums)
    p95_idx = min(int(len(nums_sorted) * 0.95), len(nums_sorted) - 1)
    return {
        "avg": round(sum(nums) / len(nums), 3),
        "max": round(max(nums), 3),
        "min": round(min(nums), 3),
        "p95": round(nums_sorted[p95_idx], 3),
        "samples": len(nums),
    }


# ── Tool 입력 스키마 ─────────────────────────────────────────────
class PrometheusInput(BaseModel):
    server_hostname: str = Field(
        description="서버 hostname (예: dev-masternode | ONTUNETEST2 | DESKTOP-H0M89JB)"
    )
    server_role: str = Field(description="서버 역할: web | was | db")
    prometheus_job: str = Field(  # ← instance → job
        description="Prometheus job 레이블 (예: bank-web-hostmetrics)"
    )
    app_job: Optional[str] = Field(
        default=None, description="앱 메트릭 job 레이블. WAS만 사용 (예: bank-was-app)"
    )
    start_ts: int = Field(description="시작 Unix timestamp")
    end_ts: int = Field(description="종료 Unix timestamp")
    step: str = Field(default="1m", description="집계 간격: 30s | 1m | 5m")
    tier1_only: bool = Field(default=False, description="Tier 1 핵심 메트릭만 조회")


# ── BaseTool 구현 ────────────────────────────────────────────────
class PrometheusQueryTool(BaseTool):
    name: str = "prometheus_query"
    description: str = (
        "서버의 성능 메트릭을 Prometheus에서 조회한다. "
        "CPU, 메모리, 요청수, 응답시간, GC, DB 연결 등을 포함. "
        "server_hostname, server_role(web/was/db), prometheus_job, 시간 범위가 필요하다."
    )
    args_schema: Type[BaseModel] = PrometheusInput

    def _run(
        self,
        server_hostname: str,
        server_role: str,
        prometheus_job: str,  # ← 변경
        app_job: Optional[str] = None,
        start_ts: int = 0,
        end_ts: int = 0,
        step: str = "1m",
        tier1_only: bool = False,
    ) -> str:
        role = server_role.lower()
        queries = PROMQL.get(role, PROMQL["web"])

        if tier1_only:
            tier1 = TIER1_METRICS.get(role, list(queries.keys())[:4])
            queries = {k: v for k, v in queries.items() if k in tier1}

        # Mock 모드
        if MOCK_MODE:
            metrics = _mock_metrics(role, server_hostname)
            return ok(
                {
                    "server": server_hostname,
                    "role": role,
                    "job": prometheus_job,
                    "app_job": app_job,
                    "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                    "step": step,
                    "mock": True,
                    "metrics": {k: v for k, v in metrics.items() if k in queries},
                }
            )

        # 실제 쿼리 — {job}, {app_job} 치환
        metrics = {}
        errors = {}
        for metric_name, query_tpl in queries.items():
            query = query_tpl.replace("{job}", prometheus_job).replace(
                "{app_job}", app_job or prometheus_job
            )
            result = _run_range_query(query, start_ts, end_ts, step)
            if result is None:
                pass
            elif "error" in result:
                errors[metric_name] = result["error"]
            else:
                metrics[metric_name] = result

        output = {
            "server": server_hostname,
            "role": role,
            "job": prometheus_job,
            "app_job": app_job,
            "period": f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            "step": step,
            "metrics": metrics,
        }
        if errors:
            output["query_errors"] = errors

        flags = _detect_anomalies(role, metrics)
        if flags:
            output["anomaly_flags"] = flags

        return ok(output)

    async def _arun(self, **kwargs) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._run(**kwargs))


# ── 이상 감지 ────────────────────────────────────────────────────
from monitoring_llm.tools.config import ANOMALY_THRESHOLDS


def _detect_anomalies(role: str, metrics: dict) -> list[str]:
    flags = []
    limits = ANOMALY_THRESHOLDS.get(role, {})
    for metric, cfg in limits.items():
        val = metrics.get(metric)
        if not isinstance(val, dict):
            continue
        max_limit = cfg["max_limit"]
        p95_ratio = cfg["p95_ratio"]
        v_max = val.get("max", 0)
        v_p95 = val.get("p95", 0)
        if v_max > max_limit:
            flags.append(f"[스파이크] {metric}: max={v_max} > 임계값={max_limit}")
        elif v_p95 > max_limit * p95_ratio:
            flags.append(
                f"[지속고부하] {metric}: p95={v_p95} > 기준={round(max_limit * p95_ratio, 3)}"
            )
    return flags


# ── 독립 실행 테스트 ─────────────────────────────────────────────
if __name__ == "__main__":
    from monitoring_llm.nlp.time_parser import default_range

    print(f"{'='*60}")
    print(f"Prometheus Tool 테스트 — MOCK_MODE={MOCK_MODE}")
    print(f"PROMETHEUS_URL={PROMETHEUS_URL}")
    print(f"{'='*60}")

    tr = default_range(60)
    tool = PrometheusQueryTool()

    # ── 실제 BankSystem_16 job/host 기준 ──────────────────────────
    test_cases = [
        {
            "server_hostname": "dev-masternode",
            "server_role": "web",
            "prometheus_job": "bank-web-hostmetrics",
            "app_job": None,
        },
        {
            "server_hostname": "ONTUNETEST2",
            "server_role": "was",
            "prometheus_job": "bank-was-hostmetrics",
            "app_job": "bank-was-app",
        },
        {
            "server_hostname": "DESKTOP-H0M89JB",
            "server_role": "db",
            "prometheus_job": "bank-db-hostmetrics",
            "app_job": None,
        },
    ]

    for tc in test_cases:
        print(f"\n[{tc['server_hostname']}] role={tc['server_role']}")
        result = tool._run(
            server_hostname=tc["server_hostname"],
            server_role=tc["server_role"],
            prometheus_job=tc["prometheus_job"],
            app_job=tc["app_job"],
            start_ts=tr.start_ts,
            end_ts=tr.end_ts,
            step="1m",
            tier1_only=True,
        )
        data = json.loads(result)
        if data.get("ok"):
            metrics = data.get("metrics", {})
            if not metrics:
                print("  ※ 수집된 메트릭 없음 (exporter 미응답 또는 쿼리 불일치)")
            for name, val in metrics.items():
                if isinstance(val, dict) and "avg" in val:
                    print(
                        f"  {name:<32} avg={val['avg']:<10} max={val['max']:<10} p95={val['p95']}"
                    )
            if data.get("anomaly_flags"):
                print(f"  ⚠ 이상 감지: {data['anomaly_flags']}")
            if data.get("query_errors"):
                print(f"  ✗ 쿼리 오류: {data['query_errors']}")
        else:
            print(f"  ERROR: {data.get('error')}")
