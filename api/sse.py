"""
api/sse.py — SSE 이벤트 유틸리티
──────────────────────────────────
역할: SSE 이벤트 포맷 생성 + Phase 4 stream_query() 래핑.

이벤트 타입:
    start     세션 ID, 파싱된 인텐트 전달
    progress  노드별 진행 상황 (nlp_parse / call_* / respond)
    chunk     응답 텍스트 청크 (40자 단위)
    done      최종 응답 + 세션 컨텍스트
    error     오류 메시지

클라이언트 JS 예시:
    const es = new EventSource('/monitoring/chat?...');
    es.addEventListener('chunk',    e => appendText(JSON.parse(e.data).content));
    es.addEventListener('done',     e => finalize(JSON.parse(e.data)));
    es.addEventListener('error',    e => showError(JSON.parse(e.data).message));
    es.addEventListener('progress', e => updateStatus(JSON.parse(e.data).message));
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

log = logging.getLogger("monitoring_llm.api")

# 노드별 사용자 친화적 메시지
NODE_MESSAGES: dict[str, str] = {
    "nlp_parse": "쿼리 분석 중...",
    "call_incident": "장애 이력 DB 조회 중...",
    "call_cmdb": "서버 자산 정보 조회 중...",
    "call_prometheus": "Prometheus 메트릭 수집 중...",
    "call_multi": "Prometheus + Loki 복합 조회 중...",
    "call_error": "로그 + 트레이스 에러 분석 중...",
    "call_action": "Runbook 매칭 + 조치 플랜 생성 중...",
    "respond": "LLM 응답 생성 중...",
}


def sse_event(data: dict, event: str = "message") -> str:
    """SSE 포맷: 'event: <name>\\ndata: <json>\\n\\n'"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_agent(
    user_input: str,
    session_state: dict,
    session_id: str,
    out_state: dict | None = None,  # ✅ 추가: 스트리밍 완료 후 상태를 호출자에게 전달
) -> AsyncGenerator[str, None]:
    """
    Phase 4 stream_query() 를 비동기로 래핑.
    노드 단위 진행 이벤트 + 응답 청크를 SSE 형식으로 yield.

    Args:
        out_state: None이 아니면 스트리밍 완료 시 최종 세션 상태를 채워 반환.
                   routes.py에서 run_query 재실행 없이 세션을 갱신하는 데 사용.
    """
    from monitoring_llm.agent.graph import stream_query

    # ── start 이벤트 ──────────────────────────────────────────────
    yield sse_event(
        {"type": "start", "session_id": session_id, "message": "분석 시작"},
        event="start",
    )
    await asyncio.sleep(0)

    final_response = ""
    updated_state = dict(session_state)

    try:
        # stream_query 는 동기 제너레이터 → asyncio.to_thread 에서 실행
        events = await asyncio.to_thread(
            lambda: list(stream_query(user_input, session_state))
        )

        for event in events:
            etype = event.get("type")

            if etype == "node":
                node_name = event.get("name", "")
                node_data = event.get("data", {})
                message = NODE_MESSAGES.get(node_name, f"{node_name} 처리 중...")

                # 노드 데이터에서 유용한 정보 추출
                detail = _extract_node_detail(node_name, node_data)

                yield sse_event(
                    {
                        "type": "progress",
                        "node": node_name,
                        "message": message,
                        "detail": detail,
                    },
                    event="progress",
                )
                await asyncio.sleep(0.05)

                # 상태 누적
                updated_state.update(node_data)

            elif etype == "done":
                final_response = event.get("response", "")

        # ── 응답 청크 스트리밍 ──────────────────────────────────
        chunk_size = 40
        for i in range(0, len(final_response), chunk_size):
            yield sse_event(
                {"type": "chunk", "content": final_response[i : i + chunk_size]},
                event="chunk",
            )
            await asyncio.sleep(0.02)

        # ── done 이벤트 ──────────────────────────────────────────
        servers = [
            s.get("hostname", "") for s in updated_state.get("current_servers", [])
        ]
        yield sse_event(
            {
                "type": "done",
                "session_id": session_id,
                "intent": updated_state.get("last_intent", "unknown"),
                "servers": servers,
                "time_range": str(updated_state.get("last_time_range") or ""),
                "response": final_response,
            },
            event="done",
        )

        # ✅ 호출자에게 최종 상태 전달 (run_query 재실행 불필요)
        if out_state is not None:
            out_state.update(updated_state)

    except Exception as e:
        log.error(f"[SSE] 스트리밍 오류: {e}", exc_info=True)
        yield sse_event(
            {"type": "error", "message": str(e)},
            event="error",
        )

    return  # async generator는 값 반환 불가


def _extract_node_detail(node_name: str, data: dict) -> str:
    """노드 출력에서 사용자에게 보여줄 핵심 정보 추출."""
    if node_name == "nlp_parse":
        intent = data.get("last_intent", "")
        servers = [s.get("hostname") for s in data.get("current_servers", [])]
        parts = [f"인텐트: {intent}"]
        if servers:
            parts.append(f"서버: {', '.join(servers)}")
        return " | ".join(parts)

    if node_name.startswith("call_"):
        results = data.get("tool_results", {})
        counts = []
        for key, val in results.items():
            if isinstance(val, dict):
                counts.append(f"{key}: {len(val)}개")
            # elif isinstance(val, str) and val.startswith("{"):
            #     counts.append(key)
            elif isinstance(val, str):
                try:
                    json.loads(val)  # ✅ 기존 startswith("{") 추정 → 실제 파싱으로 교체
                    counts.append(key)
                except json.JSONDecodeError:
                    pass

        return ", ".join(counts) if counts else "조회 완료"

    if node_name == "respond":
        resp = data.get("final_response", "")
        return f"{len(resp)}자 응답 생성" if resp else "응답 생성 중"

    return ""
