"""
rag/pgvector_store.py — asyncpg 기반 pgvector 스토어
──────────────────────────────────────────────────────
테이블 스키마:
    rag_documents (id, content, embedding, metadata, doc_type, source, created_at)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

import asyncpg

log = logging.getLogger("monitoring_llm.rag")

EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))  # nomic-embed-text 기본값


class PGVectorStore:

    TABLE = "rag_documents"

    def __init__(self, dsn: str, embed_dim: int = EMBED_DIM):
        self.dsn = dsn
        self.embed_dim = embed_dim
        self._pool: Optional[asyncpg.Pool] = None

    # ── 초기화 ──────────────────────────────────────────────────────
    async def init(self):
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        await self._setup()

    async def _setup(self):
        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    content     TEXT NOT NULL,
                    embedding   vector({self.embed_dim}),
                    metadata    JSONB    DEFAULT '{{}}',
                    doc_type    VARCHAR(50),
                    source      VARCHAR(255),
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # 코사인 유사도 인덱스 (데이터가 있을 때만 생성)
            count = await conn.fetchval(f"SELECT COUNT(*) FROM {self.TABLE}")
            if count and count > 100:
                await conn.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_rag_emb
                    ON {self.TABLE} USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = {max(4, int(count ** 0.5))})
                """)

    # ── 쓰기 ────────────────────────────────────────────────────────
    async def add_documents(self, docs: list[dict[str, Any]]) -> int:
        """
        docs: [{"content": str, "embedding": list[float],
                "doc_type": str, "source": str, "metadata": dict}]
        """
        if not docs or not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            await conn.executemany(
                f"""
                INSERT INTO {self.TABLE}
                    (content, embedding, metadata, doc_type, source)
                VALUES ($1, $2::vector, $3::jsonb, $4, $5)
                """,
                [
                    (
                        d["content"],
                        self._vec_str(d["embedding"]),
                        json.dumps(d.get("metadata", {})),
                        d.get("doc_type", "unknown"),
                        d.get("source", ""),
                    )
                    for d in docs
                ],
            )
        return len(docs)

    # ── 읽기 ────────────────────────────────────────────────────────
    async def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        doc_type: Optional[str] = None,
        since_days: Optional[int] = None,
    ) -> list[dict]:
        """코사인 유사도 기반 유사 문서 검색."""
        if not self._pool:
            return []

        vec = self._vec_str(query_embedding)
        conditions: list[str] = []
        params: list[Any] = [vec, top_k]

        if doc_type:
            params.append(doc_type)
            conditions.append(f"doc_type = ${len(params)}")
        if since_days:
            conditions.append(f"created_at >= NOW() - INTERVAL '{since_days} days'")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        sql = f"""
            SELECT content, metadata, doc_type, source, created_at,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM {self.TABLE}
            {where}
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]

    # ── 삭제 ────────────────────────────────────────────────────────
    async def delete_old(self, doc_type: str, before_days: int = 7):
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            deleted = await conn.execute(
                f"""
                DELETE FROM {self.TABLE}
                WHERE doc_type = $1
                  AND created_at < NOW() - INTERVAL '{before_days} days'
                """,
                doc_type,
            )
        log.info("[RAG] 오래된 %s 문서 삭제: %s", doc_type, deleted)

    async def count(self, doc_type: Optional[str] = None) -> int:
        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            if doc_type:
                return await conn.fetchval(
                    f"SELECT COUNT(*) FROM {self.TABLE} WHERE doc_type = $1", doc_type
                )
            return await conn.fetchval(f"SELECT COUNT(*) FROM {self.TABLE}")

    async def close(self):
        if self._pool:
            await self._pool.close()

    # ── 헬퍼 ────────────────────────────────────────────────────────
    @staticmethod
    def _vec_str(vec: list[float]) -> str:
        return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
