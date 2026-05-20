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
from datetime import datetime

log = logging.getLogger("monitoring_llm.tools")

# ── 환경변수 설정 ──────────────────────────────────────────────────
PROMETHEUS_URL    = os.getenv("PROMETHEUS_URL",    "http://192.168.0.41:9092")
LOKI_URL          = os.getenv("LOKI_URL",          "http://192.168.0.41:3101")
JAEGER_URL        = os.getenv("JAEGER_URL",        "")
ALERTMANAGER_URL  = os.getenv("ALERTMANAGER_URL",  "")
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



# def get_session() -> requests.Session:
#     global _session
#     if _session is None:
#         _session = _build_session()
#     return _session

# FastAPI는 async/멀티스레드 환경이므로 threading.Lock으로 보호하는 것이 안전합니다:
import threading
_session_lock = threading.Lock()

def get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:  # double-checked locking
                _session = _build_session()
    return _session


import atexit
def _close_session():
    global _session
    if _session:
        _session.close()
        _session = None          # ← 이것도 추가 권장 (재사용 방지)
        log.debug("HTTP 세션 종료")

atexit.register(_close_session)  # ← 모듈 임포트 시 자동 등록




# def safe_get(url: str, params: dict = None, timeout: int = 10) -> dict:
#     """
#     GET 요청 실행. 실패 시 {"error": "...", "ok": false} 반환.
#     MOCK_MODE 에서는 호출하지 않음 (각 도구에서 처리).
#     """
#     try:
#         resp = get_session().get(url, params=params, timeout=timeout)
#         resp.raise_for_status()
#         return {"ok": True, "data": resp.json()}
#     except requests.exceptions.ConnectionError as e:
#         return {"ok": False, "error": f"연결 실패 ({url}) — {str(e)[:80]}"}
#     except requests.exceptions.Timeout:
#         return {"ok": False, "error": f"타임아웃 ({timeout}s) — {url}"}
#     except requests.exceptions.HTTPError as e:
#         return {"ok": False, "error": f"HTTP {resp.status_code} — {url}"}
#     except Exception as e:
#         return {"ok": False, "error": str(e)[:100]}


def safe_get(url: str, params: dict | None = None, timeout: int = 10) -> dict:    
    """
    GET 요청 실행. 실패 시 {"ok": false, "error": "..."} 반환.
    MOCK_MODE 에서는 호출하면 안 됨 — 각 도구에서 진입 전 분기 처리 필요.
    """
    
    if MOCK_MODE:
        # 도구에서 Mock 분기를 빠뜨린 버그를 조기에 발견
        log.error("safe_get MOCK_MODE 호출 감지 | url=%s", url)
        assert False, f"safe_get은 MOCK_MODE에서 호출 금지 — {url}"
    
    
    
    resp: requests.Response | None = None  # ① 명시적 초기화

    try:
        resp = get_session().get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return {"ok": True, "data": resp.json()}

    except requests.exceptions.ConnectionError as e:
        log.warning("연결 실패 | url=%s | %s", url, str(e)[:120])
        return {"ok": False, "error": f"연결 실패 ({url}) — {str(e)[:80]}"}

    except requests.exceptions.Timeout:
        log.warning("타임아웃 | url=%s | timeout=%ss", url, timeout)
        return {"ok": False, "error": f"타임아웃 ({timeout}s) — {url}"}

    except requests.exceptions.HTTPError as e:
        # ② resp가 None인 극단적 케이스까지 안전하게 처리
        status = resp.status_code if resp is not None else "unknown"
        body   = ""
        if resp is not None:
            try:
                body = resp.json().get("error", "")  # ③ 서버 에러 메시지 추출 시도
            except Exception:
                body = resp.text[:80]

        log.error("HTTP 오류 | url=%s | status=%s | body=%s", url, status, body)
        return {
            "ok": False,
            "error": f"HTTP {status} — {url}",
            "detail": body,          # ④ LLM이 원인 파악에 활용 가능
        }

    # except ValueError as e:
    #     # ⑤ resp.json() 파싱 실패 (응답이 JSON이 아닌 경우)
    #     log.error("JSON 파싱 실패 | url=%s | %s", url, e)
    #     return {"ok": False, "error": f"JSON 파싱 실패 — {url}"}
    except ValueError as e:
        err_str = str(e)
        if "No connection adapters" in err_str:
            log.error("URL 스킴 오류 (http:// 누락?) | url=%s", url)
            return {"ok": False, "error": f"URL 형식 오류 — http:// 포함 여부 확인: {url}"}
        log.error("JSON 파싱 실패 | url=%s | %s", url, e)
        return {"ok": False, "error": f"JSON 파싱 실패 — {url}"}

    except Exception as e:
        log.exception("예상치 못한 오류 | url=%s", url)
        return {"ok": False, "error": str(e)[:100]}


# ── 공통 결과 포맷터 ───────────────────────────────────────────────
def ok(data: dict) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, indent=2)

def ok_dict(data: dict) -> dict:
    return {"ok": True, **data}

def err(message: str, **extra) -> str:
    return json.dumps({"ok": False, "error": message, **extra}, ensure_ascii=False, indent=2)

def ts_to_str(ts: int) -> str:    
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
