import requests
from datetime import datetime, timezone, timedelta


class TelemetryClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"ngrok-skip-browser-warning": "1"})

    # ----- Prometheus -----
    def prom_query(self, query: str):
        r = self.s.get(f"{self.base}/prom/api/v1/query",
                        params={"query": query})
        r.raise_for_status()
        return r.json()["data"]["result"]

    def prom_query_range(self, query: str, start: datetime, end: datetime, step: int = 60):
        r = self.s.get(f"{self.base}/prom/api/v1/query_range", params={
            "query": query,
            "start": int(start.timestamp()),
            "end":   int(end.timestamp()),
            "step":  step,
        })
        r.raise_for_status()
        return r.json()["data"]["result"]

    def prom_labels(self):
        return self.s.get(f"{self.base}/prom/api/v1/labels").json()["data"]

    # ----- Loki -----
    def loki_query_range(self, query: str, hours: float = 1, limit: int = 100):
        end_ns   = int(datetime.now(timezone.utc).timestamp() * 1e9)
        start_ns = end_ns - int(hours * 3600 * 1e9)
        r = self.s.get(f"{self.base}/loki/loki/api/v1/query_range", params={
            "query": query,
            "start": start_ns,
            "end":   end_ns,
            "limit": limit,
            "direction": "backward",
        })
        r.raise_for_status()
        return r.json()["data"]["result"]

    def loki_labels(self):
        return self.s.get(f"{self.base}/loki/loki/api/v1/labels").json()["data"]

    def loki_label_values(self, label: str):
        return self.s.get(f"{self.base}/loki/loki/api/v1/label/{label}/values").json()["data"]

from datetime import datetime, timezone, timedelta

c = TelemetryClient("https://chafe-obnoxious-iodine.ngrok-free.dev")

# ============================================================
# Prometheus
# ============================================================

# 1) instant query — "up" 시리즈 (모든 target의 살아있음 여부)
print("=== Prometheus 'up' ===")
for series in c.prom_query("up"):
    metric = series["metric"]
    ts, val = series["value"]
    label = metric.get("instance") or metric.get("job") or str(metric)
    print(f"  {label:40} = {val}")

# 2) 어떤 label이 있나
print("\n=== Prometheus labels (앞 15개) ===")
print(c.prom_labels()[:15])

# 3) range query — 최근 1시간 prometheus 자체 HTTP 요청 rate
print("\n=== rate(prometheus_http_requests_total[5m]) over 1h ===")
end = datetime.now(timezone.utc)
start = end - timedelta(hours=1)
result = c.prom_query_range(
    "rate(prometheus_http_requests_total[5m])",
    start=start, end=end, step=60,
)
for series in result[:5]:
    handler = series["metric"].get("handler", "?")
    code    = series["metric"].get("code", "?")
    points  = series["values"]
    last_ts, last_val = points[-1]
    print(f"  handler={handler:30} code={code:4}  {len(points)} pts, last={float(last_val):.4f}")

# 4) 즉석 응용 — 활성 target 수
n_up = sum(1 for s in c.prom_query("up") if s["value"][1] == "1")
n_total = len(c.prom_query("up"))
print(f"\nactive targets: {n_up}/{n_total}")

# ============================================================
# Loki
# ============================================================

print("\n=== Loki services ===")
print(c.loki_label_values("service_name"))

print("\n=== bank-was-app recent logs (limit=10) ===")
logs = c.loki_query_range('{service_name="bank-was-app"}', hours=1, limit=10)
for stream in logs:
    svc = stream["stream"].get("service_name", "?")
    for ts_ns, line in stream["values"][:3]:
        ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc).astimezone()
        print(f"  {ts:%H:%M:%S}  [{svc}]  {line[:120]}")