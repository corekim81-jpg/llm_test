"""
agent/nodes.py — LangGraph 노드 함수들
─────────────────────────────────────────
각 함수는 (state: dict) → dict 시그니처.
반환 dict의 키만 state에 merge됨 — 명시하지 않은 키는 유지.

노드 목록:
    node_nlp_parse          자연어 → BoundParams (Phase 3 파이프라인)
    node_call_incident      장애 이력 조회
    node_call_cmdb          서버 자산 조회
    node_call_prometheus    메트릭 범위 조회
    node_call_multi         메트릭 + 로그 복합 조회
    node_call_error         로그 + 트레이스 에러 분석
    node_call_action        Runbook 매칭 조치 추천
    node_respond            LLM 응답 생성
"""

from __future__ import annotations

import os
import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_ollama import ChatOllama

log = logging.getLogger("monitoring_llm.agent")

OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
CMDB_DB_PATH    = os.getenv("CMDB_DB_PATH",    "cmdb.db")


# ── LLM 인스턴스 ────────────────────────────────────────────────────
def _llm(temperature: float = 0.2, num_predict: int = 2048) -> ChatOllama:
    return ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=temperature,
        num_predict=num_predict,
        num_ctx=8192,
    )


# ── Phase 3 파이프라인 싱글톤 ────────────────────────────────────────
_pipeline = None

def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from monitoring_llm.nlp.pipeline import NLPipeline
        _pipeline = NLPipeline(use_llm=True)
    return _pipeline


# ── 시간 유틸 ────────────────────────────────────────────────────────
def _default_ts():
    from monitoring_llm.nlp.time_parser import default_range
    tr = default_range(60)
    return tr.start_ts, tr.end_ts

def _ts_from_state(state: dict):
    tr = state.get("last_time_range")
    if tr:
        return tr.start_ts, tr.end_ts
    return _default_ts()


# ══════════════════════════════════════════════════════════════════
# 노드 1: NLP 파싱
# ══════════════════════════════════════════════════════════════════
def node_nlp_parse(state: dict) -> dict:
    """
    사용자 최신 메시지 → BoundParams 변환.
    current_servers / last_time_range / last_intent 갱신.
    """
    messages = state.get("messages", [])
    if not messages:
        return {"error": "메시지 없음"}

    # 가장 마지막 HumanMessage 추출
    user_text = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_text = m.content
            break
    if not user_text:
        return {"error": "사용자 메시지 없음"}

    # 최근 3턴 대화 컨텍스트 (LLM fallback용)
    history = messages[:-1][-6:]
    context = "\n".join(
        f"{'사용자' if isinstance(m, HumanMessage) else 'AI'}: {m.content[:200]}"
        for m in history
    )

    pipeline = _get_pipeline()
    bp = pipeline.run(user_text, state=state, conversation_history=context)

    log.info(f"[NLP] intent={bp.intent} servers={[s.get('hostname') for s in bp.servers]}")

    updates: dict = {"last_intent": bp.intent}

    if bp.servers:
        # 'all' 가상 서버는 컨텍스트에 저장하지 않음
        real = [s for s in bp.servers if s.get("hostname") != "all"]
        if real:
            updates["current_servers"] = real

    if bp.time_range:
        updates["last_time_range"] = bp.time_range

    # BoundParams를 state에 임시 저장 (tool 노드에서 사용)
    updates["_bp"] = bp  # type: ignore

    return updates


# ══════════════════════════════════════════════════════════════════
# 노드 2~7: 도구 호출 노드들
# ══════════════════════════════════════════════════════════════════

def node_call_incident(state: dict) -> dict:
    """장애 이력 조회 → Alertmanager / Prometheus ALERTS"""
    from monitoring_llm.tools.tools import IncidentHistoryTool
    bp = state.get("_bp")
    start_ts, end_ts = _ts_from_state(state)
    servers = state.get("current_servers", [])
    hostname = servers[0].get("hostname") if servers else None

    tool = IncidentHistoryTool()
    result = tool._run(start_ts=start_ts, end_ts=end_ts, server_hostname=hostname)
    return {"tool_results": {"incident": result}}


def node_call_cmdb(state: dict) -> dict:
    """서버 자산 정보 조회"""
    from monitoring_llm.tools.tools import CMDBLookupTool
    bp = state.get("_bp")
    tool = CMDBLookupTool()
    results = {}

    identifiers = []
    if bp:
        from monitoring_llm.nlp.entity_extractor import ExtractedEntities
        identifiers = (bp.servers and [s.get("hostname") or s.get("ip") for s in bp.servers]) or []

    if not identifiers:
        identifiers = [s.get("ip", "") for s in state.get("current_servers", [])]

    for ident in identifiers[:3]:
        if ident:
            results[ident] = tool._run(ident)

    return {"tool_results": {"cmdb": results}}


def node_call_prometheus(state: dict) -> dict:
    """메트릭 범위 조회 (Tier1 우선)"""
    from monitoring_llm.tools.prometheus_tool import PrometheusQueryTool
    bp = state.get("_bp")
    start_ts, end_ts = _ts_from_state(state)
    servers = (bp.servers if bp else None) or state.get("current_servers", [])

    tool = PrometheusQueryTool()
    results = {}
    for s in servers[:3]:
        hostname = s.get("hostname", "")
        if not hostname or hostname == "all":
            continue
        result = tool._run(
            server_hostname     = hostname,
            server_role         = s.get("role", "was"),
            prometheus_instance = s.get("prometheus_instance", ""),
            start_ts            = start_ts,
            end_ts              = end_ts,
            tier1_only          = True,
        )
        results[hostname] = result

    return {"tool_results": {"prometheus": results}}


def node_call_multi(state: dict) -> dict:
    """Prometheus + Loki 복합 조회"""
    from monitoring_llm.tools.prometheus_tool import PrometheusQueryTool
    from monitoring_llm.tools.loki_tool import LokiQueryTool
    from monitoring_llm.tools.tools import IncidentHistoryTool

    bp       = state.get("_bp")
    start_ts, end_ts = _ts_from_state(state)
    servers  = (bp.servers if bp else None) or state.get("current_servers", [])

    prom_tool     = PrometheusQueryTool()
    loki_tool     = LokiQueryTool()
    incident_tool = IncidentHistoryTool()
    results = {}

    loki_kw = bp.loki_keyword if bp else ""
    loki_lv = bp.loki_level_filter if bp else "ERROR|WARN|error|warn"

    for s in [sv for sv in servers if sv.get("hostname") != "all"][:2]:
        hostname = s.get("hostname", "")
        results[hostname] = {
            "metrics": prom_tool._run(
                server_hostname     = hostname,
                server_role         = s.get("role", "was"),
                prometheus_instance = s.get("prometheus_instance", ""),
                start_ts=start_ts, end_ts=end_ts, tier1_only=True,
            ),
            "logs": loki_tool._run(
                loki_host    = s.get("loki_host", hostname),
                start_ts=start_ts, end_ts=end_ts,
                level_filter = loki_lv,
                keyword      = loki_kw,
                limit        = 100,
            ),
        }

    results["incidents"] = incident_tool._run(start_ts=start_ts, end_ts=end_ts)
    return {"tool_results": {"multi": results}}


def node_call_error(state: dict) -> dict:
    """Loki 에러 로그 + Jaeger 트레이스 분석"""
    from monitoring_llm.tools.loki_tool import LokiQueryTool
    from monitoring_llm.tools.jaeger_tool import JaegerTraceListTool

    bp       = state.get("_bp")
    start_ts, end_ts = _ts_from_state(state)
    servers  = (bp.servers if bp else None) or state.get("current_servers", [])

    loki_tool   = LokiQueryTool()
    jaeger_tool = JaegerTraceListTool()
    results     = {}

    kw         = (bp.loki_keyword    if bp else "") or ""
    status     = (bp.loki_status_code if bp else "") or ""
    level      = (bp.loki_level_filter if bp else "") or "ERROR|FATAL|error|fatal"
    jaeger_svcs = (bp.jaeger_services  if bp else []) or ["was-service"]

    for s in [sv for sv in servers if sv.get("hostname") != "all"][:2]:
        hostname = s.get("hostname", "")
        results[hostname] = {
            "logs": loki_tool._run(
                loki_host   = s.get("loki_host", hostname),
                start_ts=start_ts, end_ts=end_ts,
                level_filter = level,
                keyword      = kw,
                status_code  = status,
                limit        = 200,
            ),
        }

    # 서버 미지정이면 전체 조회
    if not [sv for sv in servers if sv.get("hostname") != "all"]:
        results["global"] = {
            "logs": loki_tool._run(
                loki_host="", start_ts=start_ts, end_ts=end_ts,
                level_filter=level, keyword=kw or status, limit=200,
            )
        }

    # Jaeger 트레이스
    for svc in jaeger_svcs[:2]:
        results[f"trace_{svc}"] = jaeger_tool._run(
            service_name=svc,
            start_ts=start_ts, end_ts=end_ts,
            error_only=True, min_duration_ms=50,
        )

    return {"tool_results": {"error": results}}


def node_call_action(state: dict) -> dict:
    """Runbook 매칭 + 조치 플랜 구성"""
    bp          = state.get("_bp")
    tool_results = state.get("tool_results", {})

    # 이전 분석 결과에서 에러 패턴 추출
    error_patterns = []
    if bp:
        error_patterns = list(bp.keywords) + [f"HTTP_{c}" for c in (getattr(bp, "error_codes", []) or [])]

    # 간단한 Runbook 사전 (실제 운영에서는 DB/파일 조회)
    RUNBOOKS = {
        "OOM":                ["jstack으로 스레드 덤프 수집", "WAS 재시작 (트래픽 전환 후)", "JVM -Xmx 증설 검토"],
        "GC_OVERHEAD":        ["GC 로그 수집 활성화", "힙 덤프 분석 (jmap -dump)", "메모리 누수 코드 리뷰"],
        "SLOW_QUERY":         ["SHOW PROCESSLIST 확인", "장시간 쿼리 KILL", "인덱스 추가 / EXPLAIN 분석"],
        "DEADLOCK":           ["deadlock 로그 확인 (SHOW ENGINE INNODB STATUS)", "트랜잭션 순서 정렬"],
        "TIMEOUT":            ["외부 API/DB 응답시간 점검", "타임아웃 임계값 조정", "서킷브레이커 검토"],
        "CONNECTION_REFUSED": ["AJP 포트(8009) telnet 연결 테스트", "방화벽 허용 여부 확인"],
        "DISK_FULL":          ["logrotate -f 강제 실행", "대용량 파일 탐색 (du -sh /*)", "디스크 증설 요청"],
        "HTTP_500":           ["WAS 로그 스택트레이스 확인", "최근 배포 이력 검토", "DB 연결 풀 상태 확인"],
        "AJP":                ["WAS AJP address 설정 확인", "web→was 방화벽 점검", "mod_proxy_ajp 설정 검토"],
    }

    matched = {}
    for pat in error_patterns:
        for key, actions in RUNBOOKS.items():
            if key.lower() in pat.lower() or pat.lower() in key.lower():
                matched[key] = actions

    if not matched and error_patterns:
        matched["일반"] = ["로그 재확인", "담당팀 에스컬레이션", "모니터링 강화"]

    runbook_result = json.dumps({
        "detected_patterns": error_patterns,
        "matched_runbooks":  matched,
        "human_approval_required": [
            k for k in matched
            if k in ("OOM", "DISK_FULL", "DEADLOCK")
        ],
    }, ensure_ascii=False, indent=2)

    return {"tool_results": {"action": runbook_result}}


# ══════════════════════════════════════════════════════════════════
# 노드 8: LLM 응답 생성
# ══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """/no_think
당신은 BankSystem_16 (Apache→Tomcat→MySQL 3-tier) 운영팀 AIOps 전문가입니다.

응답 원칙:
1. 수집된 데이터(메트릭/로그/트레이스)를 반드시 근거로 인용하세요.
2. 중요 수치는 명시하세요. (예: CPU 최대 87%, 평균 62%)
3. 조치 추천 시 [즉시 조치] / [단기 개선] / [모니터링] 섹션으로 구분하세요.
4. 운영자 승인이 필요한 조치는 ⚠️ 표시하세요.
5. 근거 없는 추측은 하지 마세요.
6. 한국어로 답변하세요."""


def node_respond(state: dict) -> dict:
    """
    수집된 도구 결과 + 대화 히스토리 → LLM 최종 응답.
    응답을 messages에 추가하고 final_response에도 저장.
    """
    tool_results = state.get("tool_results", {})
    messages     = state.get("messages", [])
    intent       = state.get("last_intent", "unknown")

    # 도구 결과를 프롬프트용 텍스트로 변환 (4000자 상한)
    tool_json = json.dumps(tool_results, ensure_ascii=False, indent=2)
    if len(tool_json) > 4000:
        tool_json = tool_json[:4000] + "\n... (일부 생략)"

    analysis_prompt = (
        f"[수집된 모니터링 데이터]\n{tool_json}\n\n"
        f"인텐트: {intent}\n\n"
        "위 데이터를 근거로 사용자 질문에 답변하세요."
    )

    llm_messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        *messages[:-1],           # 이전 대화 히스토리 (마지막 Human 제외)
        HumanMessage(content=analysis_prompt),
    ]

    try:
        llm      = _llm(temperature=0.2)
        response = llm.invoke(llm_messages)
        text     = response.content
    except Exception as e:
        text = f"⚠️ LLM 응답 생성 실패: {e}\n\n수집 데이터를 직접 확인하세요."

    return {
        "messages":       [AIMessage(content=text)],
        "final_response": text,
    }
