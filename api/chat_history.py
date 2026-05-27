"""
api/chat_history.py — PostgreSQL 기반 대화 이력 저장소
────────────────────────────────────────────────────────
pgvector 와 같은 PostgreSQL DB에 대화 이력을 저장.
PGVECTOR_URL 미설정 시 graceful disable.

테이블:
    chat_sessions  — 세션 메타데이터 (제목, 마지막 활성, 인텐트, 서버)
    chat_messages  — 개별 메시지 (role: user|ai, content)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("monitoring_llm.api")

HISTORY_RETAIN_DAYS = int(os.getenv("HISTORY_RETAIN_DAYS", "30"))
HISTORY_PER_DAY     = int(os.getenv("HISTORY_PER_DAY",     "20"))

_store: Optional["ChatHistoryStore"] = None


def get_history_store() -> Optional["ChatHistoryStore"]:
    return _store


async def init_history() -> None:
    global _store
    from monitoring_llm.rag import PGVECTOR_URL
    if not PGVECTOR_URL:
        log.info("[ChatHistory] PGVECTOR_URL 미설정 → 대화 이력 비활성화")
        return
    try:
        import asyncpg
        pool = await asyncpg.create_pool(PGVECTOR_URL, min_size=1, max_size=3)
        _store = ChatHistoryStore(pool)
        await _store.init()
        log.info("[ChatHistory] 초기화 완료")
    except Exception as e:
        log.warning("[ChatHistory] 초기화 실패 (비활성화): %s", e)
        _store = None


async def close_history() -> None:
    global _store
    if _store:
        await _store.close()
        _store = None


class ChatHistoryStore:
    def __init__(self, pool):
        self._pool = pool

    # ── 초기화 ──────────────────────────────────────────────────────
    async def init(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id   TEXT        PRIMARY KEY,
                    title        TEXT        NOT NULL,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_active  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_intent  TEXT        DEFAULT '',
                    servers      TEXT[]      DEFAULT '{}',
                    turn_count   INT         DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id          BIGSERIAL   PRIMARY KEY,
                    session_id  TEXT        NOT NULL
                                REFERENCES chat_sessions(session_id)
                                ON DELETE CASCADE,
                    role        TEXT        NOT NULL,
                    content     TEXT        NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_msg_session
                ON chat_messages(session_id, created_at)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_sess_active
                ON chat_sessions(last_active DESC)
            """)

    # ── 저장 ────────────────────────────────────────────────────────
    async def save_turn(
        self,
        session_id: str,
        user_msg:   str,
        ai_msg:     str,
        intent:     str       = "",
        servers:    list[str] = None,
    ) -> None:
        now     = datetime.now(timezone.utc)
        servers = servers or []
        title   = user_msg[:60]

        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO chat_sessions
                    (session_id, title, created_at, last_active, last_intent, servers, turn_count)
                VALUES ($1, $2, $3, $3, $4, $5, 1)
                ON CONFLICT (session_id) DO UPDATE SET
                    last_active = $3,
                    last_intent = $4,
                    servers     = $5,
                    turn_count  = chat_sessions.turn_count + 1
            """, session_id, title, now, intent, servers)

            await conn.execute("""
                INSERT INTO chat_messages (session_id, role, content, created_at)
                VALUES ($1, 'user', $2, $3), ($1, 'ai', $4, $3)
            """, session_id, user_msg, now, ai_msg)

    # ── 조회 ────────────────────────────────────────────────────────
    async def list_sessions(
        self,
        days:    int = None,
        per_day: int = None,
    ) -> list[dict]:
        days    = days    or HISTORY_RETAIN_DAYS
        per_day = per_day or HISTORY_PER_DAY
        since   = datetime.now(timezone.utc) - timedelta(days=days)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT session_id, title, created_at, last_active,
                       last_intent, servers, turn_count
                FROM   chat_sessions
                WHERE  last_active >= $1
                ORDER  BY last_active DESC
                LIMIT  $2
            """, since, days * per_day)

        return [_row_to_dict(r) for r in rows]

    async def get_messages(self, session_id: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT role, content, created_at
                FROM   chat_messages
                WHERE  session_id = $1
                ORDER  BY created_at ASC
            """, session_id)
        return [_row_to_dict(r) for r in rows]

    # ── 정리 ────────────────────────────────────────────────────────
    async def cleanup_old(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETAIN_DAYS)
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM chat_sessions WHERE last_active < $1
            """, cutoff)
        log.info("[ChatHistory] 오래된 이력 정리: %s", result)

    async def close(self) -> None:
        await self._pool.close()


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d
