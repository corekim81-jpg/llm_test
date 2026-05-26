"""
rag/trace_rag.py — Tempo 에러 트레이스 주기적 인덱싱
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from monitoring_llm.rag.embedder import embed_batch
from monitoring_llm.rag.pgvector_store import PGVectorStore

log = logging.getLogger("monitoring_llm.rag")

TEMPO_URL   = os.getenv("TEMPO_URL", "http://localhost:3200")
TRACE_LIMIT = int(os.getenv("RAG_TRACE_LIMIT", "50"))


class TraceRAG:
    def __init__(self, store: PGVectorStore):
        self.store = store

    async def index_recent_errors(self, minutes: int = 11) -> int:
        """최근 N분의 에러 트레이스를 Tempo에서 조회해 인덱싱."""
        now   = datetime.now(timezone.utc)
        since = now - timedelta(minutes=minutes)

        traces = await self._fetch_error_traces(since, now)
        if not traces:
            return 0

        docs = self._parse(traces)
        if not docs:
            return 0

        embeddings = await embed_batch([d["content"] for d in docs])
        for doc, emb in zip(docs, embeddings):
            doc["embedding"] = emb

        n = await self.store.add_documents(docs)
        log.info("[TraceRAG] %d건 인덱싱 완료", n)
        return n

    async def _fetch_error_traces(self, start: datetime, end: datetime) -> list:
        params = {
            "q":     "{status=error}",
            "start": str(int(start.timestamp())),
            "end":   str(int(end.timestamp())),
            "limit": TRACE_LIMIT,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{TEMPO_URL}/api/search", params=params
                )
                if resp.status_code == 200:
                    return resp.json().get("traces", [])
        except Exception as e:
            log.warning("[TraceRAG] Tempo 조회 실패: %s", e)
        return []

    @staticmethod
    def _parse(traces: list) -> list[dict]:
        docs = []
        for t in traces:
            root_svc  = t.get("rootServiceName", "unknown")
            root_name = t.get("rootTraceName", "unknown")
            duration  = t.get("durationMs", 0)
            trace_id  = t.get("traceID", "")

            service_stats = t.get("serviceStats", {})
            error_services = [
                svc for svc, stat in service_stats.items()
                if stat.get("errorCount", 0) > 0
            ]

            content = (
                f"[에러트레이스] 서비스={root_svc} 엔드포인트={root_name} "
                f"소요시간={duration}ms 에러서비스={','.join(error_services)} "
                f"traceID={trace_id}"
            )
            docs.append({
                "content":  content,
                "doc_type": "trace",
                "source":   root_svc,
                "metadata": {
                    "trace_id":       trace_id,
                    "root_service":   root_svc,
                    "root_name":      root_name,
                    "duration_ms":    duration,
                    "error_services": error_services,
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                },
            })
        return docs
