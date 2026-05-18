"""
tools/tools.py — CMDB & Incident 도구
──────────────────────────────────────────────
CMDBLookupTool   : 서버 자산 정보 조회 (cmdb/database.py 래핑)
IncidentHistoryTool : 장애 이력 조회 (Alertmanager / Prometheus ALERTS)

nodes.py 호출 규약
    CMDBLookupTool()._run(identifier)
    IncidentHistoryTool()._run(start_ts, end_ts, server_hostname)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import re
import json
import logging
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
        "hostname":            p.hostname,
        "ip":                  p.ip,
        "role":                p.role,
        "os":                  p.os,
        "tier":                p.tier,
        "team":                p.team,
        "services":            p.services,
        "prometheus_job":      p.prometheus_job,
        "prometheus_instance": p.prometheus_instance,
        "loki_host":           p.loki_host,
        "description":         p.description,
        "summary":             p.to_text(),
    }


# ═════════════════════════════════════════════════════════════════
#  CMDBLookupTool
# ═════════════════════════════════════════════════════════════════

# 싱글턴 CMDB 인스턴스
_cmdb_instance: CMDB | None = None

import threading
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

# def _get_cmdb() -> CMDB:
#     global _cmdb_instance
#     if _cmdb_instance is None:
#         _cmdb_instance = CMDB(db_path=CMDB_DB_PATH)
#         if not _cmdb_instance.get_all():
#             seed_banksystem_16(_cmdb_instance)
#             log.info("[CMDB] BankSystem_16 시드 데이터 초기화 완료")
#     return _cmdb_instance


_mock_cmdb: CMDB | None = None

def _get_mock_cmdb() -> CMDB:
    global _mock_cmdb
    if _mock_cmdb is None:
        _mock_cmdb = CMDB(db_path=":memory:")  # SQLite 인메모리
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

    p = cmdb.get_by_hostname(keyword)
    if p:
        return [p]

    return cmdb.search(keyword)


class CMDBLookupTool:
    """
    서버 자산 정보 조회.

    nodes.py 호출:
        CMDBLookupTool()._run("192.168.0.54")
        CMDBLookupTool()._run("was-bank16")
    """

    name: str = "cmdb_lookup"
    description: str = (
        "IP, hostname, role(web/was/db)로 서버 자산 정보를 조회합니다."
    )

    def _run(self, identifier: str) -> str:
        """nodes.py 호출 인터페이스"""
        if not identifier or not identifier.strip():
            return err("검색어를 입력해 주세요.")

        keyword = _extract_keyword(identifier)
        log.info("[CMDBLookupTool] identifier=%r keyword=%r mock=%s",
                 identifier, keyword, MOCK_MODE)

        if MOCK_MODE:
            return self._mock_run(keyword)
        return self._db_run(keyword)

    # run() 도 동일하게 동작 (호환성)
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
        import tempfile, os as _os
        tmp = tempfile.mktemp(suffix=".db")
        try:
            # cmdb = CMDB(db_path=tmp)
            # seed_banksystem_16(cmdb)
            # results = _cmdb_search(cmdb, keyword)
            
            results = _cmdb_search(_get_mock_cmdb(), keyword)
        finally:
            try:
                _os.unlink(tmp)
            except Exception:
                pass

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

# Mock 장애 이력 (MOCK_MODE=true)
_MOCK_INCIDENTS = [
    {
        "alert_name": "HighCpuUsage",
        "severity":   "warning",
        "server":     "was-bank16",
        "start":      "2026-05-10 14:32",
        "end":        "2026-05-10 15:01",
        "summary":    "WAS CPU 사용률 85% 초과",
    },
    {
        "alert_name": "HTTP500ErrorRate",
        "severity":   "critical",
        "server":     "was-bank16",
        "start":      "2026-05-10 14:35",
        "end":        "2026-05-10 14:58",
        "summary":    "HTTP 500 에러율 5% 초과 (피크 12%)",
    },
    {
        "alert_name": "SlowQuery",
        "severity":   "warning",
        "server":     "db-bank16",
        "start":      "2026-05-10 14:33",
        "end":        "2026-05-10 14:55",
        "summary":    "MySQL 슬로우 쿼리 급증 (>3s)",
    },
]


class IncidentHistoryTool:
    """
    장애 이력 조회 (Alertmanager silence/alerts + Prometheus ALERTS).

    nodes.py 호출:
        IncidentHistoryTool()._run(
            start_ts=1234567890,
            end_ts=1234567890,
            server_hostname="was-bank16",   # None 이면 전체
        )
    """

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

        # alerts_am   = self._fetch_alertmanager(server_hostname)
        # alerts_prom = self._fetch_prometheus_alerts(server_hostname)
        # ← start_ts, end_ts가 fetch 메서드로 전달되지 않음
        
        # ① start_ts, end_ts를 fetch 메서드로 전달
        alerts_am   = self._fetch_alertmanager(server_hostname, start_ts, end_ts)
        alerts_prom = self._fetch_prometheus_alerts(server_hostname, start_ts, end_ts)

        # all_alerts = alerts_am + alerts_prom
        
        # ② 중복 제거 (alertname + instance 기준)
        all_alerts = self._deduplicate(alerts_am + alerts_prom)
        
                
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

    # run() 호환
    def run(self, start_ts: int, end_ts: int, server_hostname: str | None = None) -> str:
        return self._run(start_ts, end_ts, server_hostname)
    
    # ── 시간 범위 체크 유틸 ───────────────────────────────────────
    @staticmethod
    def _in_range(iso_str: str, start_ts: int, end_ts: int) -> bool:
        """
        ISO 8601 문자열이 [start_ts, end_ts] 범위 내인지 확인.
        파싱 실패 시 True 반환 (필터링 안 함 — 놓치는 것보다 안전)
        """
        if not iso_str:
            return True
        try:
            # "2026-05-10T14:32:00.000Z" 형태 처리
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
            return start_ts <= ts <= end_ts
        except (ValueError, TypeError):
            return True   # 파싱 실패 → 필터 통과

    # ── 중복 제거 ─────────────────────────────────────────────────
    @staticmethod
    def _deduplicate(alerts: list[dict]) -> list[dict]:
        """
        alertname + instance 기준 중복 제거.
        Alertmanager와 Prometheus 양쪽에서 동일 알림이 올라오는 경우 방어.
        alertmanager 소스를 우선 유지.
        """
        seen: set[tuple] = set()
        deduped: list[dict] = []

        # alertmanager 우선 처리 (소스 정렬)
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

    # ── Alertmanager ──────────────────────────────────────────────
    # def _fetch_alertmanager(self, hostname: str | None) -> list[dict]:
    def _fetch_alertmanager(
        self,
        hostname: str | None,
        start_ts: int,
        end_ts: int,
    ) -> list[dict]:
        """
        GET /api/v2/alerts?active=true&silenced=false
        Alertmanager v2 응답: 최상위가 배열 [...] 또는 {"data": [...]}
        """
        params = {"active": "true", "silenced": "false", "inhibited": "false"}
                        
        res = safe_get(f"{ALERTMANAGER_URL}/api/v2/alerts", params=params)

        if not res.get("ok"):
            log.warning("[IncidentHistoryTool] Alertmanager 조회 실패: %s", res.get("error"))
            return []
        
        # ③ Alertmanager v2 응답 구조 방어: 배열 직접 or {"data": [...]}
        raw = res.get("data", [])
        if isinstance(raw, dict):
            raw = raw.get("alerts", raw.get("data", []))

        alerts = []
        # for a in res.get("data", []):
        for a in raw:
            labels   = a.get("labels", {})
            annots   = a.get("annotations", {})
            instance = labels.get("instance", "")
            starts_at = a.get("startsAt", "")

            # 서버 필터
            if hostname and hostname not in instance:
                continue
            
            # ① 시간 범위 필터
            if not self._in_range(starts_at, start_ts, end_ts):
                continue

            # alerts.append({
            #     "source":     "alertmanager",
            #     "alert_name": labels.get("alertname", "unknown"),
            #     "severity":   labels.get("severity", "unknown"),
            #     "instance":   instance,
            #     "start":      a.get("startsAt", ""),
            #     "end":        a.get("endsAt", ""),
            #     "summary":    annots.get("summary", ""),
            #     "description": annots.get("description", ""),
            # })
            
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

    # ── Prometheus ALERTS ─────────────────────────────────────────
    # def _fetch_prometheus_alerts(self, hostname: str | None) -> list[dict]:
    def _fetch_prometheus_alerts(
        self,
        hostname: str | None,
        start_ts: int,
        end_ts: int,
    ) -> list[dict]:        
        """
        GET /api/v1/alerts  (현재 발화 중인 알림)
        """
        res = safe_get(f"{PROMETHEUS_URL}/api/v1/alerts")

        if not res.get("ok"):
            log.warning("[IncidentHistoryTool] Prometheus alerts 조회 실패: %s", res.get("error"))
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
            
            # ① 시간 범위 필터
            if not self._in_range(active_at, start_ts, end_ts):
                continue

            # alerts.append({
            #     "source":     "prometheus",
            #     "alert_name": labels.get("alertname", "unknown"),
            #     "severity":   labels.get("severity", "unknown"),
            #     "state":      state,
            #     "instance":   instance,
            #     "start":      a.get("activeAt", ""),
            #     "summary":    annots.get("summary", ""),
            #     "description": annots.get("description", ""),
            # })
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

    # ── Mock ──────────────────────────────────────────────────────
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
