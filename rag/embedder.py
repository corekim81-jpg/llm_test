"""
rag/embedder.py — Ollama Embedding API 래퍼
───────────────────────────────────────────
EMBED_MODEL 환경변수로 모델 선택:
  nomic-embed-text (기본, 768차원)
  bge-m3           (1024차원, EMBED_DIM=1024 함께 설정)
"""

from __future__ import annotations

import logging
import os
from typing import Union

import httpx

log = logging.getLogger("monitoring_llm.rag")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL     = os.getenv("EMBED_MODEL", "nomic-embed-text")
EMBED_DIM       = int(os.getenv("EMBED_DIM", "768"))
_TIMEOUT        = 60.0


async def embed_text(text: str) -> list[float]:
    """단일 텍스트 임베딩."""
    return (await embed_batch([text]))[0]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """복수 텍스트 일괄 임베딩. Ollama /api/embed 사용."""
    if not texts:
        return []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            # Ollama 0.3+ 신형 API
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": texts},
            )
            resp.raise_for_status()
            return resp.json()["embeddings"]
        except (KeyError, httpx.HTTPStatusError):
            pass
        # fallback: 구형 API (단건씩)
        embeddings = []
        for text in texts:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
            )
            resp.raise_for_status()
            embeddings.append(resp.json()["embedding"])
        return embeddings
