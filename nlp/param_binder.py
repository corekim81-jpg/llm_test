"""
nlp/param_binder.py — 파라미터 바인더  [수정본]
─────────────────────────────────────────────────
변경 요약:
  - _resolve_servers: cmdb.resolve() → list 반환 처리 + 필드 교체
      prometheus_instance → prometheus_job + app_job
      loki_host           → loki_service_name + loki_server_role
  - BoundParams.prometheus_args(): prometheus_job + app_job 반환
  - BoundParams.loki_args():       service_name + server_role 반환
  - ERROR_ANALYSIS 폴백 서버 dict: loki_service_name 기반으로 교체
  - loki_level_filter 기본값: "ERROR|WARN" ((?i) 플래그로 대소문자 처리)
  - 테스트 출력: prometheus_job / loki service_name 기준으로 수정
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

JAEGER_SERVICE_MAP: dict[str, str] = {
    "web": "web-service",
    "was": "was-service",
    "db":  "db-service",
}


@dataclass
class BoundParams:
    """도구 호출에 필요한 모든 파라미터를 담은 구조체"""
    intent: str = "unknown"

    time_range: Optional[TimeRange] = None

    # 서버 파라미터 (CMDB 조회 후 완성)
    # dict 키: hostname, ip, role, os, tier,
    #          prometheus_job, app_job,
    #          loki_service_name, loki_server_role
    servers: list[dict] = field(default_factory=list)

    # Loki 파라미터
    loki_level_filter: str  = "ERROR|WARN"   # (?i) 플래그로 대소문자 처리
    loki_keyword: str       = ""
    loki_status_code: str   = ""
    loki_limit: int         = 200

    # Jaeger 파라미터
    jaeger_services: list[str]  = field(default_factory=list)
    jaeger_error_only: bool     = False
    jaeger_min_duration_ms: int = 100

    error_codes: list[str]  = field(default_factory=list)
    keywords: list[str]     = field(default_factory=list)
    from_context: bool      = False

    def has_servers(self) -> bool:
        return bool(self.servers)

    def prometheus_args(self, server: dict) -> dict:
        """PrometheusQueryTool._run() 에 바로 전달 가능한 dict"""
        tr = self.time_range or default_range(60)
        return {
            "server_hostname": server.get("hostname", ""),
            "server_role":     server.get("role", "web"),
            "prometheus_job":  server.get("prometheus_job", ""),   # ← 변경
            "app_job":         server.get("app_job", ""),          # ← 추가
            "start_ts":        tr.start_ts,
            "end_ts":          tr.end_ts,
        }

    def loki_args(self, server: dict) -> dict:
        """LokiQueryTool._run() 에 바로 전달 가능한 dict"""
        tr = self.time_range or default_range(60 * 24)  # Loki: 24h 기본
        return {
            "service_name": server.get("loki_service_name", ""),  # ← 변경
            "server_role":  server.get("loki_server_role",        # ← 변경
                                       server.get("role", "")),
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
            "service_name":    service_name,
            "start_ts":        tr.start_ts,
            "end_ts":          tr.end_ts,
            "error_only":      self.jaeger_error_only,
            "min_duration_ms": self.jaeger_min_duration_ms,
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
        # cmdb.resolve() 는 항상 list 반환
        results = cmdb.resolve(identifier)
        for server in results:
            if server and server.ip not in seen:
                seen.add(server.ip)
                resolved.append({
                    "hostname":          server.hostname,
                    "ip":                server.ip,
                    "role":              server.role,
                    "os":                server.os,
                    "tier":              server.tier,
                    "prometheus_job":    server.prometheus_job,     # ← 변경
                    "app_job":           server.app_job,            # ← 추가
                    "loki_service_name": server.loki_service_name,  # ← 변경
                    "loki_server_role":  server.loki_server_role,   # ← 변경
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
    cmdb=None,
) -> BoundParams:
    from monitoring_llm.nlp.entity_extractor import needs_context

    bp              = BoundParams(intent=intent.value)
    bp.time_range   = time_range or default_range(60)
    bp.error_codes  = entities.http_errors + entities.mysql_errors
    bp.keywords     = entities.keywords

    # ── 서버 해석 ───────────────────────────────────────────────────
    if entities.all_servers:
        bp.servers = _resolve_servers(entities, cmdb=cmdb)
    elif state and state.get("current_servers"):
        bp.servers      = state["current_servers"]
        bp.from_context = True

    # ── Intent별 파라미터 세팅 ─────────────────────────────────────
    if intent == QueryIntent.INCIDENT_HISTORY:
        if not time_range:
            from monitoring_llm.nlp.time_parser import parse_time_expression
            bp.time_range = parse_time_expression("어제") or default_range(1440)

    elif intent == QueryIntent.ERROR_ANALYSIS:
        bp.loki_level_filter = "ERROR|FATAL"
        bp.loki_limit        = 300
        bp.jaeger_error_only = True

        if bp.error_codes:
            bp.loki_status_code = bp.error_codes[0]
        if bp.keywords:
            bp.loki_keyword = "|".join(
                kw.replace("_", "\\s*") for kw in bp.keywords[:3]
            )

        # 서버 미지정 → 전체 조회 폴백 (loki_service_name 없이 빈 값)
        if not bp.servers:
            bp.servers = [{
                "hostname":          "all",
                "loki_service_name": "",   # ← 변경 (loki_host 제거)
                "loki_server_role":  "",
                "role":              "web",
                "prometheus_job":    "",
                "app_job":           "",
            }]

        if bp.servers:
            bp.jaeger_services = [
                _role_to_jaeger(s.get("role", "was"))
                for s in bp.servers
                if s.get("hostname") != "all"
            ] or ["was-service"]

    elif intent == QueryIntent.MULTI_MODAL:
        bp.loki_level_filter = "ERROR|WARN"
        bp.loki_limit        = 100
        if bp.keywords:
            bp.loki_keyword = "|".join(bp.keywords[:2])
        if bp.servers:
            bp.jaeger_services = [
                _role_to_jaeger(s.get("role", "was")) for s in bp.servers[:2]
            ]

    elif intent == QueryIntent.ACTION_RECOMMEND:
        bp.time_range = state.get("last_time_range") if state else None

    # ── role 기본값 보정 ───────────────────────────────────────────
    for server in bp.servers:
        if not server.get("role"):
            server["role"] = "was"

    return bp


# ── 독립 실행 테스트 ──────────────────────────────────────────────
if __name__ == "__main__":
    from monitoring_llm.cmdb.database import CMDB, seed_banksystem_16
    from monitoring_llm.nlp.entity_extractor import extract_entities
    from monitoring_llm.nlp.time_parser import parse_time_expression, default_range

    cmdb = CMDB("cmdb.db")
    seed_banksystem_16(cmdb)

    print("전체 서버 목록:")
    for s in cmdb.get_all():
        print(f"  {s.ip} / {s.hostname} / {s.role} / job={s.prometheus_job}")

    TEST_CASES = [
        ("어제 web에서 500 에러가 왜 발생했어?",   QueryIntent.ERROR_ANALYSIS),
        ("5월 6일 14시~16시 was 메트릭 조회해줘",  QueryIntent.METRIC_RANGE),
        ("문제있는 서버 메트릭이랑 로그 같이 보여줘", QueryIntent.MULTI_MODAL),
        ("192.168.0.63 서버는 뭐하는 서버야?",     QueryIntent.ASSET_INFO),
    ]

    print("\n파라미터 바인딩 테스트\n" + "=" * 55)
    for text, intent in TEST_CASES:
        entities   = extract_entities(text)
        time_range = parse_time_expression(text) or default_range(60)

        print(f"\n▶ 원문: {text}")
        print(f"  entities.all_servers = {entities.all_servers}")

        for identifier in entities.all_servers:
            results = cmdb.resolve(identifier)   # ← list 반환
            for r in results:
                print(f"  cmdb.resolve({identifier!r}) = {r.hostname}")

        bp = bind_params(intent, entities, time_range, cmdb=cmdb)

        print(f"\n[{intent.value}] {text}")
        print(f"  서버: {[s['hostname'] for s in bp.servers]}")
        print(f"  시간: {bp.time_range}")

        if bp.servers and intent == QueryIntent.METRIC_RANGE:
            s    = bp.servers[0]
            args = bp.prometheus_args(s)
            print(f"  prometheus_args: job={args['prometheus_job']}"   # ← 변경
                  f"  app_job={args['app_job'] or '-'}"
                  f"  role={args['server_role']}")

        if intent in (QueryIntent.ERROR_ANALYSIS, QueryIntent.MULTI_MODAL):
            s    = bp.servers[0] if bp.servers else {}
            args = bp.loki_args(s)
            print(f"  loki_args: service_name={args['service_name']}"  # ← 변경
                  f"  level={args['level_filter']}"
                  f"  keyword={args['keyword']}"
                  f"  status={args['status_code']}")
            print(f"  jaeger: services={bp.jaeger_services}"
                  f"  error_only={bp.jaeger_error_only}")

        if intent == QueryIntent.ASSET_INFO and bp.servers:
            s = bp.servers[0]
            print(f"  hostname          : {s['hostname']}")
            print(f"  ip                : {s['ip']}")
            print(f"  role              : {s['role']}")
            print(f"  prometheus_job    : {s['prometheus_job']}")
            print(f"  app_job           : {s.get('app_job') or '-'}")
            print(f"  loki_service_name : {s.get('loki_service_name') or '-'}")