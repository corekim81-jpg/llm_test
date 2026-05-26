"""
rag/ — pgvector 기반 RAG 파이프라인
──────────────────────────────────
PGVECTOR_URL 미설정 시 전체 기능 비활성화 (graceful disable).
"""

import os
import logging

log = logging.getLogger("monitoring_llm.rag")

def _normalize_dsn(url: str) -> str:
    """SQLAlchemy 형식(postgresql+psycopg2://) → asyncpg 형식(postgresql://) 자동 변환."""
    for prefix in ("postgresql+psycopg2://", "postgresql+asyncpg://", "postgres+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url

PGVECTOR_URL = _normalize_dsn(os.getenv("PGVECTOR_URL", ""))

_store = None


def get_rag_store():
    """싱글턴 PGVectorStore 반환. PGVECTOR_URL 미설정 시 None."""
    return _store


async def init_rag():
    """FastAPI lifespan 에서 호출 — 스토어 초기화."""
    global _store
    if not PGVECTOR_URL:
        log.info("[RAG] PGVECTOR_URL 미설정 → RAG 비활성화")
        return
    try:
        from monitoring_llm.rag.pgvector_store import PGVectorStore
        _store = PGVectorStore(PGVECTOR_URL)
        await _store.init()
        log.info("[RAG] pgvector 초기화 완료")
    except Exception as e:
        log.warning("[RAG] 초기화 실패 (RAG 비활성화): %s", e)
        _store = None


async def close_rag():
    global _store
    if _store:
        await _store.close()
        _store = None
