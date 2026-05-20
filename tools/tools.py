"""
tools/tools.py — CMDB & Incident 도구  [수정본]
────────────────────────────────────────────────
변경 요약:
  - _profile_to_dict: prometheus_instance/loki_host 제거
                      prometheus_job/app_job/loki_service_name/loki_server_role 추가
  - _cmdb_search: get_by_hostname() → list 반환 처리
  - Mock 장애 이력: hostname 실제 값으로 교체 (ONTUNETEST2, DESKTOP-H0M89JB)
"""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import re
import json
import logging
import threading
from datetime import datetime

from monitoring_llm.tools.base import (
    CMDB_DB_PATH,
    ALERTMANAGER_URL,
    PROMETHEUS_URL,
    MOCK_MODE,
    safe_get,
    ok,
    err,
    ts_to_str,
)
from monitoring_llm.cmdb.database import CMDB, seed_banksystem_16

log = logging.getLogger("monitoring_llm.tools")


# ═════════════════════════════════════════════════════════════════
#  공통 유틸
# ═════════════════════════════════════════════════════════════════

def _profile_to_dict(p) -> dict:
    return {
        "hostname":          p.hostname,
        "ip":                p.ip,
        "role":              p.role,
        "os":                p.os,
        "tier":              p.tier,
        "team":              p.team,
        "services":          p.services,
        "prometheus_job":    p.prometheus_job,      # ← 유지
        "app_job":           p.app_job,             # ← 추가
        "loki_service_name": p.loki_service_name,   # ← 변경 (loki_host 제거)
        "loki_server_role":  p.loki_server_role,    # ← 추가
        "description":       p.description,
        "summary":           p.to_text(),
    }


# ═════════════════════════════════════════════════════════════════
#  CMDBLookupTool
# ═════════════════════════════════════════════════════════════════

_cmdb_instance: CMDB | None = None
_cmdb_lock = threading.Lock()

def _get_cmdb() -> CMDB:
    global _cmdb_instance
    if _cmdb_instance is None:
        with _cmdb_lock:
            if _cmdb_instance is None:
                _cmdb_instance = CMDB(db_path=CMDB_DB_PATH)
                if not _cmdb_instance.get_all():
                    seed_banksystem_16(_cmdb_instance)
                    log.info("[CMDB] BankSystem_16 시드 데이터 초기화 완료")
    return _cmdb_instance


_mock_cmdb: CMDB | None = None

def _get_mock_cmdb() -> CMDB:
    global _mock_cmdb
    if _mock_cmdb is None:
        _mock_cmdb = CMDB(db_path=":memory:")
        seed_banksystem_16(_mock_cmdb)
    return _mock_cmdb


def _extract_keyword(text: str) -> str:
    """자연어 → 검색 키워드 (IP > hostname > role > 원문)"""
    m = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", text)
    if m:
        return m.group()

    m = re.search(r"\b(web|was|db)[\w-]*\b", text, re.IGNORECASE)
    if m:
        return m.group().lower()

    for role in ("web", "was", "db"):
        if role in text.lower():
            return role

    return text.strip()


def _cmdb_search(cmdb: CMDB, keyword: str) -> list:
    """검색 전략: IP → role → hostname → 통합"""
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", keyword):
        p = cmdb.get_by_ip(keyword)
        return [p] if p else []

    if keyword.lower() in ("web", "was", "db"):
        return cmdb.get_by_role(keyword.lower())

    # get_by_hostname() 은 list 반환 — [p] 로 감싸지 않음
    results = cmdb.get_by_hostname(keyword)   # ← 수정
    if results:
        return results

    return cmdb.search(keyword)


class CMDBLookupTool:
    name: str = "cmdb_lookup"
    description: str = (
        "IP, hostname, role(web/was/db)로 서버 자산 정보를 조회합니다."
    )

    def _run(self, identifier: str) -> str:
        if not identifier or not identifier.strip():
            return err("검색어를 입력해 주세요.")

        keyword = _extract_keyword(identifier)
        log.info("[CMDBLookupTool] identifier=%r keyword=%r mock=%s",
                 identifier, keyword, MOCK_MODE)

        if MOCK_MODE:
            return self._mock_run(keyword)
        return self._db_run(keyword)

    def run(self, query: str) -> str:
        return self._run(query)

    def __call__(self, query: str) -> str:
        return self._run(query)

    def _db_run(self, keyword: str) -> str:
        try:
            results = _cmdb_search(_get_cmdb(), keyword)
        except Exception as e:
            log.exception("[CMDBLookupTool] DB 오류")
            return err(f"CMDB 조회 중 오류: {e}")

        if not results:
            return err(f"CMDB에서 '{keyword}'에 해당하는 서버를 찾을 수 없습니다.")

        return ok({
            "source":  "CMDB",
            "query":   keyword,
            "count":   len(results),
            "servers": [_profile_to_dict(p) for p in results],
        })

    def _mock_run(self, keyword: str) -> str:
        results = _cmdb_search(_get_mock_cmdb(), keyword)

        if not results:
            return err(f"CMDB(Mock)에서 '{keyword}'에 해당하는 서버를 찾을 수 없습니다.")

        return ok({
            "source":  "CMDB (Mock)",
            "query":   keyword,
            "count":   len(results),
            "servers": [_profile_to_dict(p) for p in results],
        })


# ═════════════════════════════════════════════════════════════════
#  IncidentHistoryTool
# ═════════════════════════════════════════════════════════════════

# 실제 hostname 기준으로 교체
_MOCK_INCIDENTS = [
    {
        "alert_name": "HighCpuUsage",
        "severity":   "warning",
        "server":     "ONTUNETEST2",          # ← 변경
        "start":      "2026-05-10 14:32",
        "end":        "2026-05-10 15:01",
        "summary":    "WAS CPU 사용률 85% 초과",
    },
    {
        "alert_name": "HTTP500ErrorRate",
        "severity":   "critical",
        "server":     "ONTUNETEST2",          # ← 변경
        "start":      "2026-05-10 14:35",
        "end":        "2026-05-10 14:58",
        "summary":    "HTTP 500 에러율 5% 초과 (피크 12%)",
    },
    {
        "alert_name": "SlowQuery",
        "severity":   "warning",
        "server":     "DESKTOP-H0M89JB",      # ← 변경
        "start":      "2026-05-10 14:33",
        "end":        "2026-05-10 14:55",
        "summary":    "MySQL 슬로우 쿼리 급증 (>3s)",
    },
    {
        "alert_name": "DiskUsageHigh",
        "severity":   "critical",
        "server":     "dev-masternode",        # web 서버 디스크 100%
        "start":      "2026-05-19 00:00",
        "end":        "",
        "summary":    "Web 서버 디스크 사용률 100% — logrotate 확인 필요",
    },
]


class IncidentHistoryTool:
    name: str = "incident_history"
    description: str = (
        "지정 기간 내 장애 알림 이력을 Alertmanager와 Prometheus에서 조회합니다."
    )

    def _run(
        self,
        start_ts: int,
        end_ts: int,
        server_hostname: str | None = None,
    ) -> str:
        log.info("[IncidentHistoryTool] %s ~ %s  server=%s  mock=%s",
                 ts_to_str(start_ts), ts_to_str(end_ts), server_hostname, MOCK_MODE)

        if MOCK_MODE:
            return self._mock_run(server_hostname)

        alerts_am   = self._fetch_alertmanager(server_hostname, start_ts, end_ts)
        alerts_prom = self._fetch_prometheus_alerts(server_hostname, start_ts, end_ts)
        all_alerts  = self._deduplicate(alerts_am + alerts_prom)

        if not all_alerts:
            return ok({
                "source":  "Alertmanager + Prometheus",
                "period":  f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
                "server":  server_hostname or "전체",
                "count":   0,
                "alerts":  [],
                "message": "조회 기간 내 장애 이력 없음",
            })

        return ok({
            "source":  "Alertmanager + Prometheus",
            "period":  f"{ts_to_str(start_ts)} ~ {ts_to_str(end_ts)}",
            "server":  server_hostname or "전체",
            "count":   len(all_alerts),
            "alerts":  all_alerts,
        })

    def run(self, start_ts: int, end_ts: int,
            server_hostname: str | None = None) -> str:
        return self._run(start_ts, end_ts, server_hostname)

    @staticmethod
    def _in_range(iso_str: str, start_ts: int, end_ts: int) -> bool:
        if not iso_str:
            return True
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
            return start_ts <= ts <= end_ts
        except (ValueError, TypeError):
            return True

    @staticmethod
    def _deduplicate(alerts: list[dict]) -> list[dict]:
        seen: set[tuple] = set()
        deduped: list[dict] = []
        sorted_alerts = sorted(
            alerts,
            key=lambda a: 0 if a.get("source") == "alertmanager" else 1
        )
        for a in sorted_alerts:
            key = (a.get("alert_name"), a.get("instance"))
            if key not in seen:
                seen.add(key)
                deduped.append(a)
        return deduped

    def _fetch_alertmanager(
        self, hostname: str | None, start_ts: int, end_ts: int
    ) -> list[dict]:
        if not ALERTMANAGER_URL:          # ← 추가
            return []
        
        params = {"active": "true", "silenced": "false", "inhibited": "false"}
        res = safe_get(f"{ALERTMANAGER_URL}/api/v2/alerts", params=params)

        if not res.get("ok"):
            log.warning("[IncidentHistoryTool] Alertmanager 조회 실패: %s",
                        res.get("error"))
            return []

        raw = res.get("data", [])
        if isinstance(raw, dict):
            raw = raw.get("alerts", raw.get("data", []))

        alerts = []
        for a in raw:
            labels    = a.get("labels", {})
            annots    = a.get("annotations", {})
            instance  = labels.get("instance", "")
            starts_at = a.get("startsAt", "")

            if hostname and hostname not in instance:
                continue
            if not self._in_range(starts_at, start_ts, end_ts):
                continue

            alerts.append({
                "source":      "alertmanager",
                "alert_name":  labels.get("alertname", "unknown"),
                "severity":    labels.get("severity", "unknown"),
                "instance":    instance,
                "start":       starts_at,
                "end":         a.get("endsAt", ""),
                "summary":     annots.get("summary", ""),
                "description": annots.get("description", ""),
            })
        return alerts

    def _fetch_prometheus_alerts(
        self, hostname: str | None, start_ts: int, end_ts: int
    ) -> list[dict]:
        res = safe_get(f"{PROMETHEUS_URL}/api/v1/alerts")

        if not res.get("ok"):
            log.warning("[IncidentHistoryTool] Prometheus alerts 조회 실패: %s",
                        res.get("error"))
            return []

        alerts = []
        for a in res.get("data", {}).get("alerts", []):
            labels    = a.get("labels", {})
            annots    = a.get("annotations", {})
            instance  = labels.get("instance", "")
            state     = a.get("state", "")
            active_at = a.get("activeAt", "")

            if state not in ("firing", "pending"):
                continue
            if hostname and hostname not in instance:
                continue
            if not self._in_range(active_at, start_ts, end_ts):
                continue

            alerts.append({
                "source":      "prometheus",
                "alert_name":  labels.get("alertname", "unknown"),
                "severity":    labels.get("severity", "unknown"),
                "state":       state,
                "instance":    instance,
                "start":       active_at,
                "summary":     annots.get("summary", ""),
                "description": annots.get("description", ""),
            })
        return alerts

    def _mock_run(self, hostname: str | None) -> str:
        alerts = [
            a for a in _MOCK_INCIDENTS
            if not hostname or hostname in a.get("server", "")
        ]
        return ok({
            "source":  "Alertmanager + Prometheus (Mock)",
            "server":  hostname or "전체",
            "count":   len(alerts),
            "alerts":  alerts,
        })