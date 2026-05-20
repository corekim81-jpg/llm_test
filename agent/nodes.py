"""
nodes.py — role 폴백 헬퍼 + node_call_multi / node_call_error 수정본
────────────────────────────────────────────────────────────────────
추가 내용:
  - _resolve_role_fallback(): 공통 role 폴백 함수
    "WAS 서버", "web 서버" 처럼 서버 엔티티 미추출 시
    user 메시지에서 role 키워드 추출 → CMDB 조회 → 서버 dict 반환
  - node_call_multi: servers=[] 일 때 role 폴백 적용
  - node_call_error: servers=[] 일 때 role 폴백 적용
"""

from __future__ import annotations

import os
import re
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
        _pipeline = NLPipeline(use_llm=True) # ← True → False 임시
    return _pipeline 


# ── 시간 유틸 ────────────────────────────────────────────────────────
def _default_ts(minutes: int = 60):
    from monitoring_llm.nlp.time_parser import default_range
    tr = default_range(minutes)
    return tr.start_ts, tr.end_ts

def _ts_from_state(state: dict, minutes: int = 60):
    tr = state.get("last_time_range")
    if tr:
        return tr.start_ts, tr.end_ts
    return _default_ts(minutes)


# ── 서버 dict → Prometheus 파라미터 추출 ────────────────────────────
def _prom_params(s: dict) -> dict:
    return {
        "server_hostname": s.get("hostname", ""),
        "server_role":     s.get("role", "was"),
        "prometheus_job":  s.get("prometheus_job", ""),
        "app_job":         s.get("app_job", ""),
    }


# ── 서버 dict → Loki 파라미터 추출 ─────────────────────────────────
def _loki_params(s: dict) -> dict:
    return {
        "service_name": s.get("loki_service_name", ""),
        "server_role":  s.get("loki_server_role", s.get("role", "")),
    }


# ── 공통 role 폴백 ───────────────────────────────────────────────────
# def _resolve_role_fallback(state: dict) -> list[dict]:
#     """
#     servers=[] 일 때 user 메시지에서 role 키워드 추출 → CMDB 조회.
#     web/was/db 키워드가 없으면 빈 리스트 반환.
#     """
#     from monitoring_llm.tools.tools import CMDBLookupTool

#     messages  = state.get("messages", [])
#     user_text = next(
#         (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
#     )
#     m = re.search(r'\b(web|was|db)\b', user_text, re.IGNORECASE)
#     if not m:
#         return []

#     role = m.group(1).lower()
#     log.info(f"[role 폴백] '{role}' 추출 → CMDB 조회")

#     try:
#         cmdb_tool = CMDBLookupTool()
#         raw  = cmdb_tool._run(role)
#         data = json.loads(raw)
#         if data.get("ok"):
#             return data.get("servers", [])
#     except Exception as e:
#         log.warning(f"[role 폴백] CMDB 조회 실패: {e}")

#     return []
def _resolve_role_fallback(state: dict) -> list[dict]:
    from monitoring_llm.tools.tools import CMDBLookupTool
    import json

    messages  = state.get("messages", [])
    user_text = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
    )

    # 특정 role 키워드
    m = re.search(r'\b(web|was|db)\b', user_text, re.IGNORECASE)
    if m:
        role = m.group(1).lower()
        log.info(f"[role 폴백] '{role}' 추출 → CMDB 조회")
        try:
            raw  = CMDBLookupTool()._run(role)
            data = json.loads(raw)
            if data.get("ok"):
                return data.get("servers", [])
        except Exception as e:
            log.warning(f"[role 폴백] CMDB 조회 실패: {e}")
        return []

    # ← 추가: "전체", "모든", "BankSystem" → 전체 서버 조회
    if re.search(r'전체|모든|모두|BankSystem|전서버|점검', user_text, re.IGNORECASE):
        log.info("[role 폴백] 전체 서버 조회")
        try:
            from monitoring_llm.cmdb.database import CMDB
            import os
            cmdb    = CMDB(os.getenv("CMDB_DB_PATH", "cmdb.db"))
            servers = cmdb.get_all()
            return [
                {
                    "hostname":          s.hostname,
                    "ip":                s.ip,
                    "role":              s.role,
                    "os":                s.os,
                    "tier":              s.tier,
                    "prometheus_job":    s.prometheus_job,
                    "app_job":           s.app_job,
                    "loki_service_name": s.loki_service_name,
                    "loki_server_role":  s.loki_server_role,
                }
                for s in servers
            ]
        except Exception as e:
            log.warning(f"[role 폴백] 전체 서버 조회 실패: {e}")

    return []

# ══════════════════════════════════════════════════════════════════
# 노드 1: NLP 파싱
# ══════════════════════════════════════════════════════════════════
def node_nlp_parse(state: dict) -> dict:
    messages = state.get("messages", [])
    if not messages:
        return {"error": "메시지 없음"}

    user_text = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_text = m.content
            break
    if not user_text:
        return {"error": "사용자 메시지 없음"}

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
        real = [s for s in bp.servers if s.get("hostname") != "all"]
        if real:
            updates["current_servers"] = real

    if bp.time_range:
        updates["last_time_range"] = bp.time_range

    updates["_bp"] = bp
    return updates


# ══════════════════════════════════════════════════════════════════
# 노드 2: 장애 이력 조회
# ══════════════════════════════════════════════════════════════════
def node_call_incident(state: dict) -> dict:
    from monitoring_llm.tools.tools import IncidentHistoryTool
    start_ts, end_ts = _ts_from_state(state)
    servers  = state.get("current_servers", [])
    hostname = servers[0].get("hostname") if servers else None

    tool   = IncidentHistoryTool()
    result = tool._run(start_ts=start_ts, end_ts=end_ts, server_hostname=hostname)
    return {"tool_results": {"incident": result}}


# ══════════════════════════════════════════════════════════════════
# 노드 3: CMDB 조회
# ══════════════════════════════════════════════════════════════════
def node_call_cmdb(state: dict) -> dict:
    from monitoring_llm.tools.tools import CMDBLookupTool
    import re

    bp   = state.get("_bp")
    tool = CMDBLookupTool()
    results = {}

    # 1순위: bp.servers
    identifiers = []
    if bp and bp.servers:
        identifiers = [
            s.get("hostname") or s.get("ip")
            for s in bp.servers
            if s.get("hostname") != "all"
        ]

    # 2순위: current_servers
    if not identifiers:
        identifiers = [
            s.get("ip", "") or s.get("hostname", "")
            for s in state.get("current_servers", [])
        ]

    # 3순위: user 메시지에서 role 키워드
    if not identifiers:
        messages  = state.get("messages", [])
        user_text = next(
            (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
        )
        m = re.search(r'\b(web|was|db)\b', user_text, re.IGNORECASE)
        if m:
            identifiers = [m.group(1).lower()]
            log.info(f"[CMDB] role 키워드 폴백: '{m.group(1)}'")

    for ident in [i for i in identifiers if i][:3]:
        results[ident] = tool._run(ident)

    return {"tool_results": {"cmdb": results}}


# ══════════════════════════════════════════════════════════════════
# 노드 4: Prometheus 메트릭 조회
# ══════════════════════════════════════════════════════════════════
def node_call_prometheus(state: dict) -> dict:
    from monitoring_llm.tools.prometheus_tool import PrometheusQueryTool
    from monitoring_llm.tools.tools import CMDBLookupTool

    bp       = state.get("_bp")
    start_ts, end_ts = _ts_from_state(state, minutes=60)
    servers  = (bp.servers if bp else None) or state.get("current_servers", [])

    # role 폴백
    if not servers or all(s.get("hostname") == "all" for s in servers):
        servers = _resolve_role_fallback(state)

    tool    = PrometheusQueryTool()
    results = {}

    for s in servers[:3]:
        hostname = s.get("hostname", "")
        if not hostname or hostname == "all":
            continue
        p = _prom_params(s)
        if not p["prometheus_job"]:
            log.warning(f"[Prometheus] {hostname}: prometheus_job 없음 — 스킵")
            continue
        result = tool._run(
            server_hostname = p["server_hostname"],
            server_role     = p["server_role"],
            prometheus_job  = p["prometheus_job"],
            app_job         = p["app_job"],
            start_ts        = start_ts,
            end_ts          = end_ts,
            tier1_only      = True,
        )
        results[hostname] = result

    return {"tool_results": {"prometheus": results}}


# ══════════════════════════════════════════════════════════════════
# 노드 5: Prometheus + Loki 복합 조회
# ══════════════════════════════════════════════════════════════════
def node_call_multi(state: dict) -> dict:
    from monitoring_llm.tools.prometheus_tool import PrometheusQueryTool
    from monitoring_llm.tools.loki_tool       import LokiQueryTool
    from monitoring_llm.tools.tools           import IncidentHistoryTool

    bp       = state.get("_bp")
    prom_start, prom_end = _ts_from_state(state, minutes=60)
    loki_start, loki_end = _ts_from_state(state, minutes=60 * 24)
    servers  = (bp.servers if bp else None) or state.get("current_servers", [])

    # role 폴백 ← 추가
    if not servers or all(s.get("hostname") == "all" for s in servers):
        servers = _resolve_role_fallback(state)
        # 폴백도 없으면 전체 서버 조회
        # if not servers:
        #     from monitoring_llm.tools.tools import CMDBLookupTool
        #     try:
        #         raw  = CMDBLookupTool()._run("all")
        #         data = json.loads(raw)
        #         if data.get("ok"):
        #             servers = data.get("servers", [])
        #     except Exception:
        #         pass
        if not servers:
            from monitoring_llm.cmdb.database import CMDB
            import os
            try:
                cmdb    = CMDB(os.getenv("CMDB_DB_PATH", "cmdb.db"))
                servers = [
                    {
                        "hostname":          s.hostname,
                        "ip":                s.ip,
                        "role":              s.role,
                        "prometheus_job":    s.prometheus_job,
                        "app_job":           s.app_job,
                        "loki_service_name": s.loki_service_name,
                        "loki_server_role":  s.loki_server_role,
                    }
                    for s in cmdb.get_all()
                ]
            except Exception:
                pass

    prom_tool     = PrometheusQueryTool()
    loki_tool_obj = LokiQueryTool()
    incident_tool = IncidentHistoryTool()
    results       = {}

    loki_kw = bp.loki_keyword      if bp else ""
    loki_lv = bp.loki_level_filter if bp else ""

    for s in [sv for sv in servers if sv.get("hostname") != "all"][:2]:
        hostname = s.get("hostname", "")
        p = _prom_params(s)
        l = _loki_params(s)
        entry: dict = {}

        if p["prometheus_job"]:
            entry["metrics"] = prom_tool._run(
                server_hostname = p["server_hostname"],
                server_role     = p["server_role"],
                prometheus_job  = p["prometheus_job"],
                app_job         = p["app_job"],
                start_ts        = prom_start,
                end_ts          = prom_end,
                tier1_only      = True,
            )
        else:
            entry["metrics"] = {"error": "prometheus_job 미설정"}

        if l["service_name"]:
            entry["logs"] = loki_tool_obj._run(
                service_name = l["service_name"],
                server_role  = l["server_role"],
                start_ts     = loki_start,
                end_ts       = loki_end,
                level_filter = loki_lv,
                keyword      = loki_kw,
                status_code  = "5xx",
                limit        = 100,
            )
        else:
            entry["logs"] = {"info": "loki 미수집 (DB 서버)"}

        results[hostname] = entry

    results["incidents"] = incident_tool._run(
        start_ts=prom_start, end_ts=prom_end
    )
    return {"tool_results": {"multi": results}}


# ══════════════════════════════════════════════════════════════════
# 노드 6: 에러 로그 + Jaeger 트레이스
# ══════════════════════════════════════════════════════════════════
def node_call_error(state: dict) -> dict:
    from monitoring_llm.tools.loki_tool   import LokiQueryTool
    from monitoring_llm.tools.jaeger_tool import JaegerTraceListTool

    bp       = state.get("_bp")
    start_ts, end_ts = _ts_from_state(state, minutes=60 * 24)
    servers  = (bp.servers if bp else None) or state.get("current_servers", [])

    # role 폴백 ← 추가
    if not servers or all(s.get("hostname") == "all" for s in servers):
        servers = _resolve_role_fallback(state)
        # 폴백도 없으면 web+was 전체 (Loki 수집 대상만)
        if not servers:
            from monitoring_llm.tools.tools import CMDBLookupTool
            all_servers = []
            for role in ("web", "was"):
                try:
                    raw  = CMDBLookupTool()._run(role)
                    data = json.loads(raw)
                    if data.get("ok"):
                        all_servers += data.get("servers", [])
                except Exception:
                    pass
            servers = all_servers

    loki_tool_obj = LokiQueryTool()
    jaeger_tool   = JaegerTraceListTool()
    results       = {}

    kw          = (bp.loki_keyword      if bp else "") or ""
    status      = (bp.loki_status_code  if bp else "") or "5xx"
    level       = (bp.loki_level_filter if bp else "") or ""
    jaeger_svcs = (bp.jaeger_services   if bp else []) or ["was-service"]

    for s in [sv for sv in servers if sv.get("hostname") != "all"][:2]:
        hostname = s.get("hostname", "")
        l = _loki_params(s)

        if l["service_name"]:
            results[hostname] = {
                "logs": loki_tool_obj._run(
                    service_name = l["service_name"],
                    server_role  = l["server_role"],
                    start_ts     = start_ts,
                    end_ts       = end_ts,
                    level_filter = level,
                    keyword      = kw,
                    status_code  = status,
                    limit        = 200,
                ),
            }
        else:
            results[hostname] = {"info": "loki 미수집 서버"}

    real_servers = [sv for sv in servers if sv.get("hostname") != "all"]
    if not real_servers:
        results["global"] = {"info": "조회할 서버가 지정되지 않았습니다."}

    for svc in jaeger_svcs[:2]:
        results[f"trace_{svc}"] = jaeger_tool._run(
            service_name    = svc,
            start_ts        = start_ts,
            end_ts          = end_ts,
            error_only      = True,
            min_duration_ms = 50,
        )

    return {"tool_results": {"error": results}}


# ══════════════════════════════════════════════════════════════════
# 노드 7: Runbook 매칭 조치 추천
# ══════════════════════════════════════════════════════════════════
def node_call_action(state: dict) -> dict:
    bp = state.get("_bp")

    error_patterns = []
    if bp:
        error_patterns = list(bp.keywords) + [
            f"HTTP_{c}" for c in (getattr(bp, "error_codes", []) or [])
        ]

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
        "detected_patterns":       error_patterns,
        "matched_runbooks":        matched,
        "human_approval_required": [
            k for k in matched if k in ("OOM", "DISK_FULL", "DEADLOCK")
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
6. 한국어로 답변하세요.
7. Jaeger/Alertmanager 미설정은 즉시 조치가 아닙니다. 낮은 우선순위로만 언급하세요."""


def node_respond(state: dict) -> dict:
    tool_results = state.get("tool_results", {})
    messages     = state.get("messages", [])
    intent       = state.get("last_intent", "unknown")

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
        *messages[:-1],
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
