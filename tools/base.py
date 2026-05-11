"""
tools/base.py — 공통 설정·HTTP 세션·Mock 모드
──────────────────────────────────────────────
모든 도구가 공유하는 설정, requests.Session 풀, Mock 지원.
환경변수 한 곳에서 관리.
"""

import os
import json
import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("monitoring_llm.tools")

# ── 환경변수 설정 ──────────────────────────────────────────────────
PROMETHEUS_URL    = os.getenv("PROMETHEUS_URL",    "http://localhost:9090")
LOKI_URL          = os.getenv("LOKI_URL",          "http://localhost:3100")
JAEGER_URL        = os.getenv("JAEGER_URL",        "http://localhost:16686")
ALERTMANAGER_URL  = os.getenv("ALERTMANAGER_URL",  "http://localhost:9093")
CMDB_DB_PATH      = os.getenv("CMDB_DB_PATH",      "cmdb.db")

# MOCK_MODE=true 이면 실제 HTTP 없이 샘플 데이터 반환
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"

# ── HTTP 세션 (재사용 + 재시도) ───────────────────────────────────
def _build_session(retries: int = 2, backoff: float = 0.3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

_session: requests.Session | None = None

def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _build_session()
    return _session


def safe_get(url: str, params: dict = None, timeout: int = 10) -> dict:
    """
    GET 요청 실행. 실패 시 {"error": "...", "ok": false} 반환.
    MOCK_MODE 에서는 호출하지 않음 (각 도구에서 처리).
    """
    try:
        resp = get_session().get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return {"ok": True, "data": resp.json()}
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "error": f"연결 실패 ({url}) — {str(e)[:80]}"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": f"타임아웃 ({timeout}s) — {url}"}
    except requests.exceptions.HTTPError as e:
        return {"ok": False, "error": f"HTTP {resp.status_code} — {url}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


# ── 공통 결과 포맷터 ───────────────────────────────────────────────
def ok(data: dict) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, indent=2)

def err(message: str, **extra) -> str:
    return json.dumps({"ok": False, "error": message, **extra}, ensure_ascii=False, indent=2)

def ts_to_str(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
