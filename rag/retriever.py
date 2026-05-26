"""
rag/retriever.py — 질문 시 통합 컨텍스트 검색
──────────────────────────────────────────────
사용 방법 (nodes.py node_respond에서):
    context = await RAGRetriever(store).retrieve(
        query=state["messages"][-1],
        intent=state["last_intent"],
        servers=[s["hostname"] for s in state["current_servers"]],
    )
    # context를 LLM 프롬프트에 추가
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from monitoring_llm.rag.embedder import embed_text
from monitoring_llm.rag.pgvector_store import PGVectorStore

log = logging.getLogger("monitoring_llm.rag")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
RAG_TOP_K      = int(os.getenv("RAG_TOP_K", "3"))
RAG_SINCE_DAYS = int(os.getenv("RAG_SINCE_DAYS", "7"))


class RAGRetriever:
    def __init__(self, store: PGVectorStore):
        self.store = store

    async def retrieve(
        self,
        query: str,
        intent: str = "",
        servers: Optional[list[str]] = None,
    ) -> str:
        """
        인텐트별 컨텍스트를 조합해 LLM 프롬프트용 문자열 반환.
        RAG 결과가 없으면 빈 문자열 반환.
        """
        parts: list[str] = []

        # ── 벡터 검색 (로그, Runbook, RCA) ──────────────────────────
        try:
            emb = await embed_text(query)
            vector_context = await self._vector_search(query, emb, intent)
            if vector_context:
                parts.append(vector_context)
        except Exception as e:
            log.warning("[RAGRetriever] 벡터 검색 실패: %s", e)

        # ── 메트릭은 즉석 Prometheus 조회 (벡터 DB 불필요) ───────────
        if intent in ("metric_range", "multi_modal", "error_analysis"):
            metric_ctx = await self._realtime_metric_summary(servers or [])
            if metric_ctx:
                parts.append(metric_ctx)

        return "\n\n".join(parts)

    # ── 벡터 검색 ────────────────────────────────────────────────────
    async def _vector_search(
        self, query: str, emb: list[float], intent: str
    ) -> str:
        results: list[dict] = []

        # intent에 따라 검색 타입 결정
        search_types: list[tuple[Optional[str], int]] = []
        if intent == "error_analysis":
            search_types = [("log", 3), ("trace", 2), ("rca", 2), ("runbook", 2)]
        elif intent == "action_recommend":
            search_types = [("runbook", 4), ("rca", 2)]
        elif intent == "incident_history":
            search_types = [("rca", 3), ("log", 2)]
        else:
            search_types = [(None, RAG_TOP_K)]  # None = 전체 타입 검색

        for doc_type, top_k in search_types:
            hits = await self.store.search(
                emb, top_k=top_k,
                doc_type=doc_type,
                since_days=RAG_SINCE_DAYS,
            )
            results.extend(hits)

        if not results:
            return ""

        # 유사도 0.5 이하 제거
        results = [r for r in results if r.get("similarity", 0) >= 0.5]
        if not results:
            return ""

        # 중복 제거 (content 앞 80자 기준)
        seen, unique = set(), []
        for r in results:
            key = r["content"][:80]
            if key not in seen:
                seen.add(key)
                unique.append(r)

        lines = [
            f"[{r['doc_type']}][{r['source']}] {r['content'][:300]}"
            for r in unique[:6]
        ]
        return "## 관련 이력/문서\n" + "\n".join(lines)

    # ── 실시간 메트릭 요약 ───────────────────────────────────────────
    async def _realtime_metric_summary(self, servers: list[str]) -> str:
        if not servers:
            return ""
        summaries = []
        for host in servers[:3]:  # 최대 3대
            s = await self._query_server_metrics(host)
            if s:
                summaries.append(s)
        if not summaries:
            return ""
        return "## 현재 메트릭 요약\n" + "\n".join(summaries)

    async def _query_server_metrics(self, hostname: str) -> str:
        queries = {
            "CPU(%)":   f'100 - avg(rate(node_cpu_seconds_total{{mode="idle",instance=~"{hostname}.*"}}[5m]))*100',
            "MEM(%)":   f'(1 - node_memory_MemAvailable_bytes{{instance=~"{hostname}.*"}} / node_memory_MemTotal_bytes{{instance=~"{hostname}.*"}}) * 100',
            "DISK(%)":  f'100 - (node_filesystem_avail_bytes{{instance=~"{hostname}.*",mountpoint="/"}} / node_filesystem_size_bytes{{instance=~"{hostname}.*",mountpoint="/"}}) * 100',
        }
        results = []
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for label, q in queries.items():
                    resp = await client.get(
                        f"{PROMETHEUS_URL}/api/v1/query",
                        params={"query": q},
                    )
                    if resp.status_code == 200:
                        data = resp.json().get("data", {}).get("result", [])
                        if data:
                            val = float(data[0]["value"][1])
                            results.append(f"{label}={val:.1f}")
        except Exception:
            pass
        if not results:
            return ""
        return f"{hostname}: " + ", ".join(results)
