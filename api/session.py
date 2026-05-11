"""
api/session.py — 세션 관리
───────────────────────────
역할: 대화 세션(LangGraph State)의 생성·조회·갱신·만료를 관리.
      현재는 In-Memory 구현 — 운영 환경에서는 Redis로 교체 가능.

TTL: 기본 2시간 (SESSION_TTL_HOURS 환경변수로 조정)
최대 세션수: 1000개 (초과 시 오래된 순으로 정리)
"""

from __future__ import annotations

import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("monitoring_llm.api")

SESSION_TTL_HOURS = int(
    __import__("os").getenv("SESSION_TTL_HOURS", "2")
)
MAX_SESSIONS = 1000


class SessionStore:
    """
    In-Memory 세션 저장소.
    각 세션은 LangGraph State dict + 메타데이터를 보유.
    """

    def __init__(self):
        self._store: dict[str, dict] = {}

    # ── CRUD ──────────────────────────────────────────────────────
    def create(self, session_id: Optional[str] = None) -> tuple[str, dict]:
        """새 세션 생성. session_id 미지정 시 UUID 자동 생성."""
        from monitoring_llm.agent.state import make_initial_state

        sid   = session_id or str(uuid.uuid4())
        now   = datetime.now()
        state = make_initial_state()

        self._store[sid] = {
            "state":       state,
            "created":     now,
            "last_active": now,
            "turn_count":  0,
        }
        log.info(f"[Session] 생성: {sid}")
        return sid, state

    def get(self, session_id: str) -> Optional[dict]:
        """세션 state 반환. 없거나 만료 시 None."""
        entry = self._store.get(session_id)
        if not entry:
            return None
        if self._is_expired(entry):
            self.delete(session_id)
            return None
        entry["last_active"] = datetime.now()
        return entry["state"]

    def update(self, session_id: str, state: dict) -> bool:
        """세션 state 갱신."""
        entry = self._store.get(session_id)
        if not entry:
            return False
        entry["state"]       = state
        entry["last_active"] = datetime.now()
        entry["turn_count"]  = entry.get("turn_count", 0) + 1
        return True

    def delete(self, session_id: str) -> bool:
        if session_id in self._store:
            del self._store[session_id]
            log.info(f"[Session] 삭제: {session_id}")
            return True
        return False

    def get_or_create(self, session_id: Optional[str]) -> tuple[str, dict]:
        """조회 실패 시 자동 생성."""
        if session_id:
            state = self.get(session_id)
            if state is not None:
                return session_id, state
        return self.create(session_id)

    # ── 메타데이터 ─────────────────────────────────────────────────
    def meta(self, session_id: str) -> Optional[dict]:
        entry = self._store.get(session_id)
        if not entry:
            return None
        from langchain_core.messages import HumanMessage
        messages    = entry["state"].get("messages", [])
        turn_count  = sum(1 for m in messages if isinstance(m, HumanMessage))
        servers     = [
            s.get("hostname", "") for s in
            entry["state"].get("current_servers", [])
        ]
        return {
            "session_id":      session_id,
            "created":         entry["created"].isoformat(),
            "last_active":     entry["last_active"].isoformat(),
            "turn_count":      turn_count,
            "current_servers": servers,
            "last_intent":     entry["state"].get("last_intent", "unknown"),
            "expires_at":      (
                entry["last_active"] + timedelta(hours=SESSION_TTL_HOURS)
            ).isoformat(),
        }

    def all_meta(self) -> list[dict]:
        self._cleanup()
        return [self.meta(sid) for sid in self._store if self.meta(sid)]

    # ── 정리 ──────────────────────────────────────────────────────
    def _is_expired(self, entry: dict) -> bool:
        return datetime.now() - entry["last_active"] > timedelta(hours=SESSION_TTL_HOURS)

    def _cleanup(self):
        """만료 세션 제거 + MAX_SESSIONS 초과 시 오래된 순 정리."""
        expired = [
            sid for sid, entry in self._store.items()
            if self._is_expired(entry)
        ]
        for sid in expired:
            del self._store[sid]

        if len(self._store) > MAX_SESSIONS:
            by_age = sorted(
                self._store.items(),
                key=lambda x: x[1]["last_active"],
            )
            for sid, _ in by_age[:len(self._store) - MAX_SESSIONS]:
                del self._store[sid]

    @property
    def active_count(self) -> int:
        self._cleanup()
        return len(self._store)


# 전역 싱글톤
_store: Optional[SessionStore] = None

def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
