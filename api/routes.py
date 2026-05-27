"""
api/routes.py — FastAPI 모니터링 채팅 라우터
──────────────────────────────────────────────
기존 FastAPI 서버에 3줄로 통합:

    from monitoring_llm.api.routes import router as monitoring_router
    app.include_router(monitoring_router, prefix="/monitoring")

엔드포인트:
    POST   /monitoring/chat           SSE 스트리밍 채팅
    POST   /monitoring/chat/sync      동기 응답 (테스트)
    GET    /monitoring/session/{id}   세션 상태 조회
    DELETE /monitoring/session/{id}   세션 초기화
    GET    /monitoring/sessions       전체 활성 세션 목록
    GET    /monitoring/health         헬스체크
"""

from __future__ import annotations

import os
import asyncio
import logging
from datetime import datetime, timedelta, date as _date
from typing import Optional, AsyncGenerator
import httpx

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from monitoring_llm.api.session import get_store
from monitoring_llm.api.sse import stream_agent, sse_event
from monitoring_llm.llm_factory import LLM_PROVIDER, OLLAMA_BASE_URL, provider_info

log = logging.getLogger("monitoring_llm.api")
router = APIRouter(tags=["monitoring-llm"])


class ChatRequest(BaseModel):
    message:    str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    response:   str
    intent:     str
    servers:    list[str] = []
    time_range: Optional[str] = None


# ── 엔드포인트 1: SSE 스트리밍 채팅 ─────────────────────────────────
@router.post("/chat")
async def chat_stream(req: ChatRequest, request: Request):
    """
    SSE 스트리밍 채팅.
    이벤트 순서: start → progress(×N) → chunk(×N) → done

    curl 테스트:
        curl -N -X POST http://localhost:8000/monitoring/chat \\
          -H 'Content-Type: application/json' \\
          -d '{"message":"어제 was01에서 500 에러가 왜 발생했어?","session_id":"s01"}'
    """
    store = get_store()
    sid, state = store.get_or_create(req.session_id)

    async def event_generator() -> AsyncGenerator[str, None]:        
        # ✅ out_state: stream_agent가 최종 상태를 채워줌 → run_query 재실행 불필요
        out_state: dict = {}
        # async for chunk in stream_agent(req.message, state, sid):
        async for chunk in stream_agent(req.message, state, sid, out_state):
            if await request.is_disconnected():
                log.info(f"[SSE] 연결 끊김: {sid}")
                break
            yield chunk

        # # 스트리밍 완료 후 세션 상태 갱신 (별도 실행)
        # try:
        #     from monitoring_llm.agent.graph import run_query
        #     _, updated = await asyncio.to_thread(run_query, req.message, state)
        #     store.update(sid, updated)
        # except Exception as e:
        #     log.error(f"[Session] 상태 저장 실패: {e}")
        # ✅ stream_agent가 채운 out_state로 세션 갱신 (LLM 2회 호출 제거)
        if out_state:
            store.update(sid, out_state)
            # 대화 이력 저장
            from monitoring_llm.api.chat_history import get_history_store
            hist = get_history_store()
            if hist:
                servers = [s.get("hostname", "") for s in out_state.get("current_servers", [])]
                asyncio.create_task(hist.save_turn(
                    sid,
                    req.message,
                    out_state.get("final_response", ""),
                    out_state.get("last_intent", ""),
                    servers,
                ))
        else:
            log.warning(f"[Session] out_state 비어있음, 세션 갱신 생략: {sid}")


    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":             "no-cache",
            "Connection":                "keep-alive",
            "X-Accel-Buffering":         "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── 엔드포인트 2: 동기 응답 (테스트용) ──────────────────────────────
@router.post("/chat/sync", response_model=ChatResponse)
async def chat_sync(req: ChatRequest):
    """
    동기 응답 — 테스트 / CI 에서 사용.
    운영에서는 /chat (SSE) 권장.
    """
    from monitoring_llm.agent.graph import run_query

    store = get_store()
    sid, state = store.get_or_create(req.session_id)

    response_text, updated = await asyncio.to_thread(run_query, req.message, state)
    store.update(sid, updated)

    # 대화 이력 저장
    from monitoring_llm.api.chat_history import get_history_store
    hist = get_history_store()
    if hist:
        servers = [s.get("hostname", "") for s in updated.get("current_servers", [])]
        asyncio.create_task(hist.save_turn(
            sid, req.message, response_text,
            updated.get("last_intent", ""), servers,
        ))

    return ChatResponse(
        session_id = sid,
        response   = response_text,
        intent     = updated.get("last_intent", "unknown"),
        servers    = [s.get("hostname","") for s in updated.get("current_servers",[])],
        time_range = str(updated.get("last_time_range") or ""),
    )


# ── 엔드포인트 3: 세션 관리 ─────────────────────────────────────────
@router.get("/session/{session_id}")
async def get_session(session_id: str):
    store = get_store()
    meta  = store.meta(session_id)
    if not meta:
        raise HTTPException(404, f"세션 '{session_id}' 없음 또는 만료됨")
    return meta


@router.post("/session/restore/{session_id}")
async def restore_session(session_id: str):
    """이력 DB에서 세션 상태 복원 — 이전 대화 이어받기."""
    from langchain_core.messages import HumanMessage, AIMessage
    from monitoring_llm.agent.state import make_initial_state
    from monitoring_llm.agent.nodes import LLM_HISTORY_TURNS
    from monitoring_llm.api.chat_history import get_history_store

    hist = get_history_store()
    if hist is None:
        return {"restored": False, "reason": "history disabled"}

    messages = await hist.get_messages(session_id)
    if not messages:
        return {"restored": False, "reason": "no history"}

    # 최근 LLM_HISTORY_TURNS 턴만 복원 (컨텍스트 윈도우 절약)
    recent = messages[-(LLM_HISTORY_TURNS * 2):]

    lc_messages = []
    for msg in recent:
        if msg["role"] == "user":
            lc_messages.append(HumanMessage(content=msg["content"]))
        else:
            lc_messages.append(AIMessage(content=msg["content"]))

    state = make_initial_state()
    state["messages"] = lc_messages

    store = get_store()
    store.restore(session_id, state)

    return {"restored": True, "messages_loaded": len(lc_messages)}


@router.delete("/session/{session_id}")
async def delete_session(session_id: str):
    store = get_store()
    if not store.delete(session_id):
        raise HTTPException(404, f"세션 '{session_id}' 없음")
    return {"message": f"세션 '{session_id}' 삭제 완료"}


@router.get("/sessions")
async def list_sessions():
    store = get_store()
    return {"active_count": store.active_count, "sessions": store.all_meta()}


# ── 엔드포인트 5: 대화 이력 ─────────────────────────────────────────
@router.get("/history")
async def list_history(days: int = 30, per_day: int = 20):
    """날짜별로 그룹핑된 대화 세션 목록."""
    from monitoring_llm.api.chat_history import get_history_store
    hist = get_history_store()
    if hist is None:
        return {"sessions": [], "enabled": False}
    sessions = await hist.list_sessions(days=days, per_day=per_day)
    return {"sessions": _group_by_date(sessions), "enabled": True}


@router.get("/history/{session_id}")
async def get_history_messages(session_id: str):
    """특정 세션의 메시지 목록."""
    from monitoring_llm.api.chat_history import get_history_store
    hist = get_history_store()
    if hist is None:
        raise HTTPException(503, "대화 이력 비활성화 상태 (PGVECTOR_URL 미설정)")
    messages = await hist.get_messages(session_id)
    if not messages:
        raise HTTPException(404, f"세션 '{session_id}' 이력 없음")
    return {"session_id": session_id, "messages": messages}


def _group_by_date(sessions: list[dict]) -> list[dict]:
    today     = _date.today()
    yesterday = today - timedelta(days=1)
    groups: dict[str, list] = {}

    for s in sessions:
        raw = s.get("last_active", "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except Exception:
            dt = today

        if dt == today:
            label = "오늘"
        elif dt == yesterday:
            label = "어제"
        elif dt >= today - timedelta(days=7):
            label = "이번 주"
        else:
            label = dt.strftime("%Y년 %m월")

        groups.setdefault(label, []).append(s)

    order  = ["오늘", "어제", "이번 주"]
    result = []
    for label in order:
        if label in groups:
            result.append({"label": label, "items": groups.pop(label)})
    for label, items in groups.items():
        result.append({"label": label, "items": items})
    return result


def _llm_health_entry() -> list[tuple]:
    """LLM 프로바이더에 따라 헬스체크 엔드포인트 반환"""
    if LLM_PROVIDER == "anthropic":
        # Anthropic은 직접 HTTP 헬스체크 불가 — API key 유무만 확인
        from monitoring_llm.llm_factory import ANTHROPIC_API_KEY
        return [("anthropic", "https://api.anthropic.com" if ANTHROPIC_API_KEY else "http://localhost:0/")]
    return [("ollama", OLLAMA_BASE_URL + "/api/tags")]


# ── 엔드포인트 4: 헬스체크 ──────────────────────────────────────────
@router.get("/health")
async def health_check():
    # import requests
    from datetime import datetime

    # 트레이스 백엔드: JAEGER_URL 우선, 없으면 TEMPO_URL
    _jaeger = os.getenv("JAEGER_URL", "")
    _tempo  = os.getenv("TEMPO_URL",  "")
    if _jaeger:
        _trace_entry = ("jaeger", _jaeger + "/api/services")
    elif _tempo:
        _trace_entry = ("tempo",  _tempo  + "/api/search/tag/service.name/values")
    else:
        _trace_entry = ("trace",  "http://localhost:0/")  # 미설정 → unreachable 예상

    endpoints = [
        ("prometheus", os.getenv("PROMETHEUS_URL",  "http://localhost:9090") + "/-/healthy"),
        ("loki",       os.getenv("LOKI_URL",        "http://localhost:3100")  + "/ready"),
        _trace_entry,
        *(_llm_health_entry()),
    ]
    checks: dict[str, str] = {}
    # for name, url in endpoints:
    #     try:
    #         r = requests.get(url, timeout=3)
    #         checks[name] = "ok" if r.status_code < 400 else f"http_{r.status_code}"
    #     except requests.exceptions.ConnectionError:
    #         checks[name] = "unreachable"
    #     except Exception as e:
    #         checks[name] = f"error:{str(e)[:25]}"
    
    # ✅ httpx.AsyncClient: 4개 요청을 이벤트 루프 블로킹 없이 동시에 처리
    async with httpx.AsyncClient() as client:
        tasks = {
            name: client.get(url, timeout=3.0)
            for name, url in endpoints
        }
        for name, coro in tasks.items():
            try:
                r = await coro
                checks[name] = "ok" if r.status_code < 400 else f"http_{r.status_code}"
            except httpx.ConnectError:
                checks[name] = "unreachable"
            except httpx.TimeoutException:
                checks[name] = "timeout"
            except Exception as e:
                checks[name] = f"error:{str(e)[:25]}"

    status = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {
        "status":          status,
        "checks":          checks,
        "llm_provider":    provider_info(),
        "active_sessions": get_store().active_count,
        "timestamp":       datetime.now().isoformat(),
    }
