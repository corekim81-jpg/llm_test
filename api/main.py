"""
api/main.py — FastAPI 앱 진입점
────────────────────────────────
기존 debate/RCA 서버에 모니터링 LLM 라우터를 통합하는 방법:

    방법 A: 이 파일을 단독 서버로 실행
        uvicorn monitoring_llm.api.main:app --host 0.0.0.0 --port 8000 --reload

    방법 B: 기존 main.py 에 3줄 추가
        from monitoring_llm.api.routes import router as monitoring_router
        app.include_router(monitoring_router, prefix="/monitoring")
        # 끝. 기존 라우터(/debate, /rca 등)는 그대로 유지됨.

환경변수:
    PROMETHEUS_URL   http://prometheus:9090
    LOKI_URL         http://loki:3100
    JAEGER_URL       http://jaeger:16686
    OLLAMA_BASE_URL  http://dev-ubuntu:11434
    OLLAMA_MODEL     qwen3:8b
    CMDB_DB_PATH     /path/to/cmdb.db
    MOCK_MODE        true  (개발/테스트 시)
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# .env 파일 자동 로드 (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 없어도 동작 — 환경변수를 직접 export 하면 됨

import pathlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from monitoring_llm.api.routes import router as monitoring_router

_STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("monitoring_llm")


def create_app() -> FastAPI:
    app = FastAPI(
        title       = "Monitoring LLM API",
        description = "BankSystem_16 통합 모니터링 LLM — 자연어 쿼리 기반 AIOps",
        version     = "1.0.0",
        docs_url    = "/docs",
        redoc_url   = "/redoc",
    )

    # CORS (프론트엔드 개발 시 필요)
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = ["*"],
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # 모니터링 LLM 라우터 등록
    app.include_router(monitoring_router, prefix="/monitoring")

    # 정적 파일 서빙 (index.html 등)
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/ui", tags=["ui"])
    async def ui():
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/", tags=["root"])
    async def root():
        return {
            "service": "Monitoring LLM API",
            "ui":      "/ui",
            "docs":    "/docs",
            "health":  "/monitoring/health",
            "chat":    "/monitoring/chat",
        }

    @app.on_event("startup")
    async def startup():
        log.info("Monitoring LLM API 시작")
        log.info(f"  OLLAMA_MODEL    = {os.getenv('OLLAMA_MODEL',    'qwen3:8b')}")
        log.info(f"  PROMETHEUS_URL  = {os.getenv('PROMETHEUS_URL',  'http://localhost:9090')}")
        log.info(f"  LOKI_URL        = {os.getenv('LOKI_URL',        'http://localhost:3101')}")
        log.info(f"  MOCK_MODE       = {os.getenv('MOCK_MODE',       'false')}")

        # CMDB 초기 데이터 확인
        cmdb_path = os.getenv("CMDB_DB_PATH", "cmdb.db")
        log.info(f"  CMDB_DB_PATH    = {os.getenv('CMDB_DB_PATH', 'cmdb.db')}")
        if not os.path.exists(cmdb_path):
            log.warning(f"CMDB 파일 없음: {cmdb_path} — seed 데이터 자동 생성")
            from monitoring_llm.cmdb.database import CMDB, seed_banksystem_16
            cmdb = CMDB(cmdb_path)
            seed_banksystem_16(cmdb)

        # RAG 초기화 + 스케줄러 시작 (PGVECTOR_URL 설정 시)
        from monitoring_llm.rag import init_rag, get_rag_store
        await init_rag()
        rag_store = get_rag_store()
        if rag_store:
            from monitoring_llm.rag.scheduler import RAGIndexScheduler
            app.state.rag_scheduler = RAGIndexScheduler(rag_store)
            await app.state.rag_scheduler.start()
            log.info("  RAG 스케줄러    = 시작됨")
        else:
            log.info("  RAG             = 비활성화 (PGVECTOR_URL 미설정)")

    @app.on_event("shutdown")
    async def shutdown():
        if hasattr(app.state, "rag_scheduler"):
            await app.state.rag_scheduler.stop()
        from monitoring_llm.rag import close_rag
        await close_rag()

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "monitoring_llm.api.main:app",
        host    = "0.0.0.0",
        port    = int(os.getenv("PORT", "8000")),
        reload  = os.getenv("RELOAD", "false").lower() == "true",
        workers = 1,   # LangGraph 싱글톤 보호를 위해 1 고정
    )
