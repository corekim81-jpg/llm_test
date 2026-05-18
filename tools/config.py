"""
tools/config.py — 공통 임계값 설정
──────────────────────────────────
MicroRCA METRIC_TIERS와 Prometheus 이상 감지 임계값을 한 곳에서 관리.
"""

# ── Prometheus 이상 감지 임계값 ────────────────────────────────────
# max_limit  : 순간 스파이크 감지 (max 기준)
# p95_ratio  : 지속 고부하 감지 (p95 > max_limit * p95_ratio)
ANOMALY_THRESHOLDS: dict[str, dict[str, dict]] = {
    "web": {
        "cpu_util_pct":  {"max_limit": 80,   "p95_ratio": 0.90},
        "mem_used_pct":  {"max_limit": 85,   "p95_ratio": 0.90},
        "disk_used_pct": {"max_limit": 90,   "p95_ratio": 0.95},  # 디스크는 감소 안 함 → ratio 높게
        "workers_busy":  {"max_limit": 180,  "p95_ratio": 0.85},
    },
    "was": {
        "heap_used_pct":  {"max_limit": 85,  "p95_ratio": 0.90},
        "threads_active": {"max_limit": 190, "p95_ratio": 0.85},
        "gc_time_rate":   {"max_limit": 0.1, "p95_ratio": 0.80},  # GC는 민감하게
        "error_per_sec":  {"max_limit": 1.0, "p95_ratio": 0.70},
    },
    "db": {
        "connections_max_pct":  {"max_limit": 70,  "p95_ratio": 0.85},
        "slow_queries_per_sec": {"max_limit": 5.0, "p95_ratio": 0.80},
        "threads_running":      {"max_limit": 30,  "p95_ratio": 0.85},
    },
}