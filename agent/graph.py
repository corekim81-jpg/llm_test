"""
agent/graph.py — LangGraph 그래프 조립
────────────────────────────────────────
노드 흐름:
    START → nlp_parse → intent_router → [tool_node] → respond → END
"""

from __future__ import annotations
import os, logging
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from typing import Optional
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage

from monitoring_llm.agent.state import MonitoringState, make_initial_state
from monitoring_llm.agent.nodes import (
    node_nlp_parse, node_call_incident, node_call_cmdb,
    node_call_prometheus, node_call_multi, node_call_error,
    node_call_action, node_respond,
)
from monitoring_llm.nlp.intent_classifier import QueryIntent

log = logging.getLogger("monitoring_llm.agent")

_ROUTE_MAP = {
    QueryIntent.INCIDENT_HISTORY.value: "call_incident",
    QueryIntent.ASSET_INFO.value:       "call_cmdb",
    QueryIntent.METRIC_RANGE.value:     "call_prometheus",
    QueryIntent.MULTI_MODAL.value:      "call_multi",
    QueryIntent.ERROR_ANALYSIS.value:   "call_error",
    QueryIntent.ACTION_RECOMMEND.value: "call_action",
}

def intent_router(state: dict) -> str:
    intent = state.get("last_intent", "unknown")
    dest   = _ROUTE_MAP.get(intent, "call_multi")
    log.info(f"[Router] {intent} → {dest}")
    return dest


def build_graph():
    g = StateGraph(MonitoringState)

    g.add_node("nlp_parse",       node_nlp_parse)
    g.add_node("call_incident",   node_call_incident)
    g.add_node("call_cmdb",       node_call_cmdb)
    g.add_node("call_prometheus", node_call_prometheus)
    g.add_node("call_multi",      node_call_multi)
    g.add_node("call_error",      node_call_error)
    g.add_node("call_action",     node_call_action)
    g.add_node("respond",         node_respond)

    g.add_edge(START, "nlp_parse")
    g.add_conditional_edges(
        "nlp_parse", intent_router,
        {v: v for v in _ROUTE_MAP.values()},
    )
    for node in _ROUTE_MAP.values():
        g.add_edge(node, "respond")
    g.add_edge("respond", END)

    return g.compile()


_graph = None

def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def run_query(user_input: str, session_state: Optional[dict] = None) -> tuple[str, dict]:
    """
    단일 쿼리 실행.
    반환: (응답 텍스트, 업데이트된 세션 상태)

    사용 예시:
        state = make_initial_state()
        resp, state = run_query("어제 문제 있었어?", state)
        resp, state = run_query("그 서버 메트릭 보여줘", state)
    """
    if session_state is None:
        session_state = make_initial_state()
    state = dict(session_state)
    state["messages"] = list(state.get("messages", [])) + [HumanMessage(content=user_input)]
    result = get_graph().invoke(state)
    return result.get("final_response", ""), result


def stream_query(user_input: str, session_state: Optional[dict] = None):
    """
    노드 단위 스트리밍 (FastAPI SSE용).
    yield: {"type":"node","name":"...","data":{...}}
            {"type":"done","response":"최종 응답"}
    """
    if session_state is None:
        session_state = make_initial_state()
    state = dict(session_state)
    state["messages"] = list(state.get("messages", [])) + [HumanMessage(content=user_input)]
    final = ""
    for event in get_graph().stream(state, stream_mode="updates"):
        for node_name, node_output in event.items():
            yield {"type": "node", "name": node_name, "data": node_output}
            if "final_response" in node_output:
                final = node_output["final_response"]
    yield {"type": "done", "response": final}


if __name__ == "__main__":
    print("그래프 빌드 테스트")
    g = build_graph()
    print(f"  노드: {list(g.nodes.keys())}")
    print("  빌드 성공 ✓\n라우터 테스트:")
    for intent, expected in [
        ("incident_history","call_incident"), ("asset_info","call_cmdb"),
        ("metric_range","call_prometheus"),   ("multi_modal","call_multi"),
        ("error_analysis","call_error"),      ("action_recommend","call_action"),
        ("unknown","call_multi"),
    ]:
        result = intent_router({"last_intent": intent})
        print(f"  {'✓' if result==expected else '✗'} {intent:<22} → {result}")
