"""
rag/scheduler.py — APScheduler 기반 주기적 인덱싱
───────────────────────────────────────────────────
FastAPI lifespan 에서 시작/종료:
    scheduler = RAGIndexScheduler(store)
    await scheduler.start()
    ...
    await scheduler.stop()
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from monitoring_llm.rag.pgvector_store import PGVectorStore

log = logging.getLogger("monitoring_llm.rag")

LOG_INTERVAL_MIN   = int(os.getenv("RAG_LOG_INTERVAL_MIN",   "5"))
TRACE_INTERVAL_MIN = int(os.getenv("RAG_TRACE_INTERVAL_MIN", "10"))
CLEANUP_HOUR       = int(os.getenv("RAG_CLEANUP_HOUR",       "3"))
LOG_RETAIN_DAYS    = int(os.getenv("RAG_LOG_RETAIN_DAYS",    "7"))


class RAGIndexScheduler:

    def __init__(self, store: PGVectorStore):
        self.store = store
        self._sched = AsyncIOScheduler(timezone="Asia/Seoul")

    async def start(self):
        """스케줄러 시작 + Runbook 최초 인덱싱."""
        self._register_jobs()
        self._sched.start()
        log.info("[RAGScheduler] 스케줄러 시작")

        # 서버 기동 시 Runbook 즉시 동기화
        await self._sync_runbooks()

    async def stop(self):
        self._sched.shutdown(wait=False)
        log.info("[RAGScheduler] 스케줄러 종료")

    # ── 잡 등록 ─────────────────────────────────────────────────────
    def _register_jobs(self):
        # 로그 인덱싱 (5분마다)
        self._sched.add_job(
            self._index_logs,
            trigger="interval",
            minutes=LOG_INTERVAL_MIN,
            id="rag_log_indexing",
            replace_existing=True,
        )
        # 트레이스 인덱싱 (10분마다)
        self._sched.add_job(
            self._index_traces,
            trigger="interval",
            minutes=TRACE_INTERVAL_MIN,
            id="rag_trace_indexing",
            replace_existing=True,
        )
        # 오래된 벡터 정리 (매일 새벽 3시)
        self._sched.add_job(
            self._cleanup,
            trigger="cron",
            hour=CLEANUP_HOUR,
            id="rag_cleanup",
            replace_existing=True,
        )

    # ── 잡 구현 ─────────────────────────────────────────────────────
    async def _index_logs(self):
        try:
            from monitoring_llm.rag.log_rag import LogRAG
            n = await LogRAG(self.store).index_recent(minutes=LOG_INTERVAL_MIN + 1)
            if n:
                log.info("[RAGScheduler] 로그 %d건 인덱싱", n)
            else:
                log.info("[RAGScheduler] 로그 %d건 인덱싱2", n)

                
        except Exception as e:
            log.warning("[RAGScheduler] 로그 인덱싱 실패: %s", e)

    async def _index_traces(self):
        try:
            from monitoring_llm.rag.trace_rag import TraceRAG
            n = await TraceRAG(self.store).index_recent_errors(
                minutes=TRACE_INTERVAL_MIN + 1
            )
            if n:
                log.info("[RAGScheduler] 트레이스 %d건 인덱싱", n)
            else:
                log.info("[RAGScheduler] 트레이스 %d건 인덱싱2", n)
                
        except Exception as e:
            log.warning("[RAGScheduler] 트레이스 인덱싱 실패: %s", e)

    async def _sync_runbooks(self):
        try:
            from monitoring_llm.rag.runbook_rag import RunbookRAG
            n = await RunbookRAG(self.store).sync()
            log.info("[RAGScheduler] Runbook %d 청크 동기화", n)
        except Exception as e:
            log.warning("[RAGScheduler] Runbook 동기화 실패: %s", e)

    async def _cleanup(self):
        try:
            await self.store.delete_old("log",   LOG_RETAIN_DAYS)
            await self.store.delete_old("trace", LOG_RETAIN_DAYS)
            log.info("[RAGScheduler] 오래된 벡터 정리 완료")
        except Exception as e:
            log.warning("[RAGScheduler] 정리 실패: %s", e)

        try:
            from monitoring_llm.api.chat_history import get_history_store
            hist = get_history_store()
            if hist:
                await hist.cleanup_old()
        except Exception as e:
            log.warning("[RAGScheduler] 대화 이력 정리 실패: %s", e)
