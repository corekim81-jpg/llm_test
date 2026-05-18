"""
nlp/param_binder.py — 파라미터 바인더
──────────────────────────────────────
역할: 추출된 엔티티 + Intent → 각 도구(Tool)의 실제 호출 파라미터로 변환.
      CMDB 조회로 IP/hostname → prometheus_instance, loki_host 완성.

출력 예시:
{
  "intent": "metric_range",
  "time": {"start_ts": 1234567890, "end_ts": 1234571490, "step": "1m"},
  "servers": [
    {
      "hostname": "was01-bank16",
      "role": "was",
      "prometheus_instance": "192.168.16.20:9090",
      "loki_host": "was01-bank16",
    }
  ],
  "loki_params": {"level_filter": "ERROR|WARN", "keyword": "OOM", "limit": 200},
  "jaeger_params": {"service_name": "was-service", "error_only": True},
  "error_codes": ["500"],
  "keywords": ["OOM"],
}
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional


import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from monitoring_llm.nlp.entity_extractor import ExtractedEntities
from monitoring_llm.nlp.intent_classifier import QueryIntent
from monitoring_llm.nlp.time_parser import TimeRange, default_range

CMDB_DB_PATH = os.getenv("CMDB_DB_PATH", "cmdb.db")

# Jaeger 서비스명 매핑 (CMDB role → Jaeger service.name)
JAEGER_SERVICE_MAP: dict[str, str] = {
    "web": "web-service",
    "was": "was-service",
    "db":  "db-service",
}


@dataclass
class BoundParams:
    """도구 호출에 필요한 모든 파라미터를 담은 구조체"""
    intent: str = "unknown"

    # 시간 파라미터
    time_range: Optional[TimeRange] = None

    # 서버 파라미터 (CMDB 조회 후 완성)
    servers: list[dict] = field(default_factory=list)

    # Loki 파라미터
    loki_level_filter: str = "ERROR|WARN|error|warn"
    loki_keyword: str = ""
    loki_status_code: str = ""
    loki_limit: int = 200

    # Jaeger 파라미터
    jaeger_services: list[str] = field(default_factory=list)
    jaeger_error_only: bool = False
    jaeger_min_duration_ms: int = 100

    # 에러/키워드 (분석에서 사용)
    error_codes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # 컨텍스트에서 가져온 서버인지
    from_context: bool = False

    def has_servers(self) -> bool:
        return bool(self.servers)

    def prometheus_args(self, server: dict) -> dict:
        """PrometheusQueryTool._run() 에 바로 전달 가능한 dict"""
        tr = self.time_range or default_range(60)
        return {
            "server_hostname":     server.get("hostname", ""),
            "server_role":         server.get("role", "web"),
            "prometheus_instance": server.get("prometheus_instance", ""),
            "start_ts":            tr.start_ts,
            "end_ts":              tr.end_ts,
        }

    def loki_args(self, server: dict) -> dict:
        """LokiQueryTool._run() 에 바로 전달 가능한 dict"""
        tr = self.time_range or default_range(60)
        return {
            "loki_host":    server.get("loki_host", server.get("hostname", "")),
            "start_ts":     tr.start_ts,
            "end_ts":       tr.end_ts,
            "level_filter": self.loki_level_filter,
            "keyword":      self.loki_keyword,
            "status_code":  self.loki_status_code,
            "limit":        self.loki_limit,
        }

    def jaeger_args(self, service_name: str) -> dict:
        """JaegerTraceListTool._run() 에 바로 전달 가능한 dict"""
        tr = self.time_range or default_range(60)
        return {
            "service_name":      service_name,
            "start_ts":          tr.start_ts,
            "end_ts":            tr.end_ts,
            "error_only":        self.jaeger_error_only,
            "min_duration_ms":   self.jaeger_min_duration_ms,
        }

    def to_dict(self) -> dict:
        return {
            "intent":       self.intent,
            "time_range":   str(self.time_range) if self.time_range else None,
            "servers":      self.servers,
            "error_codes":  self.error_codes,
            "keywords":     self.keywords,
            "loki_keyword": self.loki_keyword,
            "from_context": self.from_context,
        }


# ── CMDB 조회 ─────────────────────────────────────────────────────
# def _resolve_servers(entities: ExtractedEntities) -> list[dict]:
def _resolve_servers(entities: ExtractedEntities, cmdb=None) -> list[dict]:    
    """IP/hostname → CMDB → 서버 파라미터 dict 반환"""
    try:
        if cmdb is None:
            from monitoring_llm.cmdb.database import CMDB
            cmdb = CMDB(CMDB_DB_PATH)
    except Exception:
        return []

    resolved = []
    seen = set()

    for identifier in entities.all_servers:
        server = cmdb.resolve(identifier)
        if server and server.ip not in seen:
            seen.add(server.ip)
            resolved.append({
                "hostname":            server.hostname,
                "ip":                  server.ip,
                "role":                server.role,
                "os":                  server.os,
                "tier":                server.tier,
                "prometheus_instance": server.prometheus_instance,
                "prometheus_job":      server.prometheus_job,
                "loki_host":           server.loki_host,
            })
    return resolved


def _role_to_jaeger(role: str) -> str:
    return JAEGER_SERVICE_MAP.get(role, f"{role}-service")


# ── 핵심 바인딩 로직 ───────────────────────────────────────────────
def bind_params(
    intent: QueryIntent,
    entities: ExtractedEntities,
    time_range: Optional[TimeRange],
    state: Optional[dict] = None,
    cmdb=None
) -> BoundParams:
    """
    Intent + Entities + TimeRange → BoundParams

    state: LangGraph State (멀티턴 컨텍스트 유지)
    """
    from monitoring_llm.nlp.entity_extractor import needs_context

    bp = BoundParams(intent=intent.value)
    bp.time_range   = time_range or default_range(60)
    bp.error_codes  = entities.http_errors + entities.mysql_errors
    bp.keywords     = entities.keywords

    # ── 서버 해석 ───────────────────────────────────────────────────
    from_context = needs_context("", entities)  # 대명사 감지는 상위에서

    if entities.all_servers:
        bp.servers = _resolve_servers(entities, cmdb=cmdb)  # cmdb 주입
    elif state and state.get("current_servers"):
        # 컨텍스트에서 이전 서버 가져오기
        bp.servers     = state["current_servers"]
        bp.from_context = True

    # ── Intent별 파라미터 세팅 ─────────────────────────────────────

    if intent == QueryIntent.INCIDENT_HISTORY:
        # 기본 시간: 어제 하루
        if not time_range:
            from monitoring_llm.nlp.time_parser import parse_time_expression
            bp.time_range = parse_time_expression("어제") or default_range(1440)

    elif intent == QueryIntent.ERROR_ANALYSIS:
        # Loki: 에러코드 + 키워드 필터
        bp.loki_level_filter = "ERROR|FATAL|error|fatal"
        bp.loki_limit        = 300
        bp.jaeger_error_only = True

        if bp.error_codes:
            bp.loki_status_code = bp.error_codes[0]
        if bp.keywords:
            bp.loki_keyword = "|".join(
                kw.replace("_", "\\s*") for kw in bp.keywords[:3]
            )
        # 서버 지정 없으면 전 서버 로그 조회 → loki_host=""
        if not bp.servers:
            bp.servers = [{"hostname": "all", "loki_host": "",
                           "role": "web", "prometheus_instance": ""}]

        # Jaeger 서비스 목록
        if bp.servers:
            bp.jaeger_services = [
                _role_to_jaeger(s.get("role", "was"))
                for s in bp.servers
                if s.get("hostname") != "all"
            ] or ["was-service"]

    elif intent == QueryIntent.MULTI_MODAL:
        # 복합 조회: 에러 로그만 (토큰 절약)
        bp.loki_level_filter = "ERROR|WARN|error|warn"
        bp.loki_limit        = 100
        if bp.keywords:
            bp.loki_keyword = "|".join(bp.keywords[:2])
        if bp.servers:
            bp.jaeger_services = [_role_to_jaeger(s.get("role","was")) for s in bp.servers[:2]]

    elif intent == QueryIntent.ACTION_RECOMMEND:
        # 조치 추천: 이전 분석 컨텍스트 참조, 시간 범위 불필요
        bp.time_range = state.get("last_time_range") if state else None

    # ── Tier 1 우선 처리를 위한 role 정보 확인 ─────────────────────
    for server in bp.servers:
        if not server.get("role"):
            server["role"] = "was"  # 기본값

    return bp


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "../..")
    from monitoring_llm.cmdb.database import CMDB, seed_banksystem_16
    from monitoring_llm.nlp.entity_extractor import extract_entities
    from monitoring_llm.nlp.time_parser import parse_time_expression, default_range

    # CMDB 초기화
    # cmdb = CMDB("/tmp/test_binder.db")
    cmdb = CMDB("cmdb.db")    
    seed_banksystem_16(cmdb)
    
    # seed된 전체 서버 목록 출력
    all_servers = cmdb.get_all()   # 혹은 cmdb.list_all() 등 조회 메서드
    for s in all_servers:
        print(f"  {s.ip} / {s.hostname} / {s.role}")

    TEST_CASES = [
        ("어제 web에서 500 에러가 왜 발생했어?",  QueryIntent.ERROR_ANALYSIS), # web01
        ("5월 6일 14시~16시 was 메트릭 조회해줘", QueryIntent.METRIC_RANGE),   # was01
        ("문제있는 서버 메트릭이랑 로그 같이 보여줘", QueryIntent.MULTI_MODAL),
        ("192.168.0.63 서버는 뭐하는 서버야?",     QueryIntent.ASSET_INFO),
    ]

    print("파라미터 바인딩 테스트\n" + "="*55)
    for text, intent in TEST_CASES:
        entities  = extract_entities(text)
        time_range = parse_time_expression(text) or default_range(60)
        
        
        # ── 디버그 출력 ──────────────────────────
        print(f"\n▶ 원문: {text}")
        print(f"  entities.ips       = {entities.ips}")
        print(f"  entities.hostnames = {entities.hostnames}")
        print(f"  entities.all_servers = {entities.all_servers}")
        
        # CMDB resolve 직접 테스트
        for identifier in entities.all_servers:
            result = cmdb.resolve(identifier)
            print(f"  cmdb.resolve({identifier!r}) = {result}")
        # ────────────────────────────────────────
        
        
        bp = bind_params(intent, entities, time_range, cmdb=cmdb)   # ← cmdb 전달

        print(f"\n[{intent.value}] {text}")
        print(f"  서버: {[s['hostname'] for s in bp.servers]}")
        print(f"  시간: {bp.time_range}")
        if bp.servers and intent == QueryIntent.METRIC_RANGE:
            s = bp.servers[0]
            args = bp.prometheus_args(s)
            print(f"  prometheus_args: instance={args['prometheus_instance']}, "
                  f"role={args['server_role']}")
        if intent in (QueryIntent.ERROR_ANALYSIS, QueryIntent.MULTI_MODAL):
            print(f"  loki: level={bp.loki_level_filter}, "
                  f"keyword={bp.loki_keyword}, code={bp.loki_status_code}")
            print(f"  jaeger: services={bp.jaeger_services}, error_only={bp.jaeger_error_only}")
        if intent == QueryIntent.ASSET_INFO and bp.servers:
            s = bp.servers[0]
            print(f"  hostname : {s['hostname']}")
            print(f"  ip       : {s['ip']}")
            print(f"  role     : {s['role']}")
            print(f"  os       : {s['os']}")
            print(f"  tier     : {s['tier']}")
