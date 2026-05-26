"""
rag/log_rag.py — Loki 에러/경고 로그 주기적 인덱싱
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from monitoring_llm.rag.embedder import embed_batch
from monitoring_llm.rag.pgvector_store import PGVectorStore

log = logging.getLogger("monitoring_llm.rag")

LOKI_URL     = os.getenv("LOKI_URL", "http://localhost:3100")
LOG_LEVELS   = os.getenv("RAG_LOG_LEVELS", "error|warn|critical")  # 인덱싱할 레벨
LOG_LIMIT    = int(os.getenv("RAG_LOG_LIMIT", "500"))               # 회차당 최대 건수
LOG_MAX_LEN  = 400                                                   # 로그 줄 최대 길이


class LogRAG:
    def __init__(self, store: PGVectorStore):
        self.store = store

    async def index_recent(self, minutes: int = 5) -> int:
        """최근 N분 에러/경고 로그를 가져와 pgvector에 저장."""
        now   = datetime.now(timezone.utc)
        since = now - timedelta(minutes=minutes)

        entries = await self._fetch_loki(since, now)
        if not entries:
            log.debug("[LogRAG] 새 로그 없음")
            return 0

        docs = self._parse(entries)
        if not docs:
            return 0

        embeddings = await embed_batch([d["content"] for d in docs])
        for doc, emb in zip(docs, embeddings):
            doc["embedding"] = emb

        n = await self.store.add_documents(docs)
        log.info("[LogRAG] %d건 인덱싱 완료", n)
        return n

    async def cleanup(self, before_days: int = 7):
        await self.store.delete_old("log", before_days)

    # ── 내부 ────────────────────────────────────────────────────────
    async def _fetch_loki(self, start: datetime, end: datetime) -> list:
        params = {
            "query": '{level=~"' + LOG_LEVELS + '"}',
            "start": str(int(start.timestamp() * 1e9)),
            "end":   str(int(end.timestamp()   * 1e9)),
            "limit": LOG_LIMIT,
            "direction": "backward",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{LOKI_URL}/loki/api/v1/query_range", params=params
                )
                if resp.status_code == 200:
                    return resp.json().get("data", {}).get("result", [])
                log.warning("[LogRAG] Loki 응답 %d", resp.status_code)
        except Exception as e:
            log.warning("[LogRAG] Loki 조회 실패: %s", e)
        return []

    @staticmethod
    def _parse(loki_result: list) -> list[dict]:
        docs = []
        for stream in loki_result:
            labels  = stream.get("stream", {})
            service = labels.get("service_name") or labels.get("job") or "unknown"
            level   = labels.get("level", "unknown")

            for ts_ns, line in stream.get("values", []):
                line = line.strip()
                if len(line) < 10:
                    continue
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
                content = f"[{service}][{level}] {line[:LOG_MAX_LEN]}"
                docs.append({
                    "content":  content,
                    "doc_type": "log",
                    "source":   service,
                    "metadata": {
                        "service":   service,
                        "level":     level,
                        "timestamp": ts.isoformat(),
                        "labels":    labels,
                    },
                })
        return docs
