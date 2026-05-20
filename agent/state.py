
"""
agent/state.py — LangGraph State 정의
──────────────────────────────────────
역할: 대화 세션 전체에 걸쳐 유지되는 상태 구조 정의.
      LangGraph TypedDict 방식으로 정의 — 노드 반환값이
      기존 state에 merge 됨 (messages는 누적, 나머지는 덮어쓰기).

필드 설명:
    messages        대화 히스토리 (HumanMessage / AIMessage 누적)
    current_servers CMDB 조회 완료된 서버 목록 — 멀티턴 컨텍스트 핵심
    last_time_range 마지막 파싱된 TimeRange — "그 시간대" 재참조용
    last_intent     마지막 분류된 인텐트
    tool_results    도구 호출 결과 raw (intent → json str)
    final_response  LLM 최종 응답 텍스트
    error           처리 중 발생한 오류 메시지
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Optional
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages  # messages 누적 reducer


class MonitoringState(TypedDict, total=False):
    # ── 대화 ───────────────────────────────────────────────────────
    # add_messages: 기존 리스트에 새 메시지를 append (덮어쓰지 않음)
    messages: Annotated[list[BaseMessage], add_messages]

    # ── 컨텍스트 (멀티턴 유지 핵심) ────────────────────────────────
    current_servers: list[dict]   # [{hostname, ip, role, prometheus_instance, loki_host}] 아래로 변경됨. 
                                  # [{hostname, ip, role, prometheus_job, app_job, loki_service_name, loki_server_role}] 
    last_time_range: Any          # TimeRange 객체 또는 None
    last_intent: str              # 마지막 인텐트 값

    # ── 현재 턴 처리 결과 ───────────────────────────────────────────
    tool_results: dict            # {tool_name: json_str}
    final_response: str
    error: Optional[str]


def make_initial_state() -> dict:
    """새 세션의 초기 상태"""
    return {
        "messages":       [],
        "current_servers": [],
        "last_time_range": None,
        "last_intent":    "unknown",
        "tool_results":   {},
        "final_response": "",
        "error":          None,
    }
