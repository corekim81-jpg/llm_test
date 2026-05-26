"""
rag/runbook_rag.py — Markdown Runbook 1회/변경 시 인덱싱
──────────────────────────────────────────────────────────
RUNBOOK_DIR 디렉토리의 .md 파일을 청크 단위로 임베딩해 저장.
파일 MD5 해시로 변경 감지 → 변경된 파일만 재인덱싱.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

from monitoring_llm.rag.embedder import embed_batch
from monitoring_llm.rag.pgvector_store import PGVectorStore

log = logging.getLogger("monitoring_llm.rag")

RUNBOOK_DIR  = os.getenv("RUNBOOK_DIR",  "./runbooks")
HASH_FILE    = os.getenv("RUNBOOK_HASH", "./runbooks/.hash_cache.json")
CHUNK_SIZE   = int(os.getenv("RUNBOOK_CHUNK_SIZE", "500"))   # 글자 수
CHUNK_OVERLAP = int(os.getenv("RUNBOOK_CHUNK_OVERLAP", "50"))


class RunbookRAG:
    def __init__(self, store: PGVectorStore):
        self.store = store
        self._hashes: dict[str, str] = self._load_hashes()

    async def sync(self) -> int:
        """변경된 Runbook 파일만 재인덱싱. 변경 없으면 0 반환."""
        runbook_dir = Path(RUNBOOK_DIR)
        if not runbook_dir.exists():
            log.debug("[RunbookRAG] 디렉토리 없음: %s", RUNBOOK_DIR)
            return 0

        changed: list[Path] = []
        for fpath in sorted(runbook_dir.glob("**/*.md")):
            h = self._md5(fpath)
            if self._hashes.get(str(fpath)) != h:
                changed.append(fpath)
                self._hashes[str(fpath)] = h

        if not changed:
            log.debug("[RunbookRAG] 변경된 Runbook 없음")
            return 0

        log.info("[RunbookRAG] 변경된 파일 %d개 재인덱싱", len(changed))
        total = 0
        for fpath in changed:
            total += await self._index_file(fpath)

        self._save_hashes()
        return total

    async def _index_file(self, fpath: Path) -> int:
        text   = fpath.read_text(encoding="utf-8", errors="ignore")
        chunks = self._split_chunks(text, str(fpath.stem))
        if not chunks:
            return 0

        embeddings = await embed_batch([c["content"] for c in chunks])
        for chunk, emb in zip(chunks, embeddings):
            chunk["embedding"] = emb

        n = await self.store.add_documents(chunks)
        log.info("[RunbookRAG] %s → %d 청크 인덱싱", fpath.name, n)
        return n

    # ── 텍스트 청킹 ────────────────────────────────────────────────
    def _split_chunks(self, text: str, filename: str) -> list[dict]:
        # 마크다운 헤더 기반 1차 분할
        sections = re.split(r"\n(?=#{1,3} )", text)
        chunks = []
        for section in sections:
            section = section.strip()
            if not section:
                continue
            # 긴 섹션은 추가 분할
            for chunk in self._sliding_window(section):
                chunks.append({
                    "content":  chunk,
                    "doc_type": "runbook",
                    "source":   filename,
                    "metadata": {"filename": filename, "section_preview": chunk[:80]},
                })
        return chunks

    @staticmethod
    def _sliding_window(text: str) -> list[str]:
        if len(text) <= CHUNK_SIZE:
            return [text]
        chunks, start = [], 0
        while start < len(text):
            end = start + CHUNK_SIZE
            chunks.append(text[start:end])
            start += CHUNK_SIZE - CHUNK_OVERLAP
        return chunks

    # ── 해시 관리 ──────────────────────────────────────────────────
    @staticmethod
    def _md5(path: Path) -> str:
        return hashlib.md5(path.read_bytes()).hexdigest()

    def _load_hashes(self) -> dict:
        try:
            return json.loads(Path(HASH_FILE).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_hashes(self):
        try:
            Path(HASH_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(HASH_FILE).write_text(
                json.dumps(self._hashes, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("[RunbookRAG] 해시 저장 실패: %s", e)
