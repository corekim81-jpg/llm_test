"""
rag/rca_rag.py — RCA/이상탐지 결과 이벤트 트리거 인덱싱
─────────────────────────────────────────────────────────
장애 분석 후 `index_rca_result()` 를 호출하면
나중에 "비슷한 장애 있었어?" 쿼리 시 검색 가능해짐.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from monitoring_llm.rag.embedder import embed_text
from monitoring_llm.rag.pgvector_store import PGVectorStore

log = logging.getLogger("monitoring_llm.rag")


class RcaRAG:
    def __init__(self, store: PGVectorStore):
        self.store = store

    async def index_rca_result(
        self,
        query: str,
        response: str,
        intent: str,
        servers: list[str],
        tool_results: dict | None = None,
    ) -> bool:
        """
        LLM이 생성한 장애 분석 결과를 벡터 DB에 저장.

        호출 시점: node_respond() 완료 후 (error_analysis 인텐트일 때).

        저장 내용: 질문 + 답변 요약 → 나중에 유사 질문 시 컨텍스트로 활용.
        """
        content = self._build_content(query, response, servers)
        try:
            embedding = await embed_text(content)
        except Exception as e:
            log.warning("[RcaRAG] 임베딩 실패: %s", e)
            return False

        await self.store.add_documents([{
            "content":  content,
            "embedding": embedding,
            "doc_type": "rca",
            "source":   ",".join(servers) if servers else "unknown",
            "metadata": {
                "query":     query[:200],
                "intent":    intent,
                "servers":   servers,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }])
        log.info("[RcaRAG] RCA 결과 인덱싱 완료: %s", query[:60])
        return True

    @staticmethod
    def _build_content(query: str, response: str, servers: list[str]) -> str:
        server_str = ", ".join(servers) if servers else "unknown"
        # 응답에서 핵심 문장만 추출 (최대 600자)
        summary = response.strip()[:600]
        return (
            f"[장애분석] 서버: {server_str}\n"
            f"질문: {query}\n"
            f"분석결과: {summary}"
        )
