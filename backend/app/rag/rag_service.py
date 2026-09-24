from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.config import settings


STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are",
    "was", "were", "with", "from", "this", "that", "it", "as", "at", "by", "be",
    "after", "before", "into", "then", "than", "not", "has", "have", "had", "but",
}


@dataclass
class DocumentChunk:
    chunk_id: str
    source: str
    category: str
    text: str


class LocalRAG:
    """Small, zero-cost local RAG implementation.

    It uses sentence-transformers when available for semantic retrieval. If the
    embedding model is unavailable, it falls back to a deterministic TF-IDF-like
    cosine scorer so the application remains runnable without downloading a model.
    No external vector database is required for this personal project.
    """

    def __init__(self, knowledge_dir: str | None = None):
        self.knowledge_dir = Path(knowledge_dir or settings.rag_knowledge_dir)
        self.chunk_size = int(os.getenv("RAG_CHUNK_SIZE", "1200"))
        self.chunk_overlap = int(os.getenv("RAG_CHUNK_OVERLAP", "200"))
        self.top_k = settings.rag_top_k
        self.chunks: list[DocumentChunk] = []
        self._embeddings = None
        self._model = None
        self._index_signature = ""
        self._load_or_build()

    def _files(self) -> list[Path]:
        if not self.knowledge_dir.exists():
            return []
        return sorted(p for p in self.knowledge_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt"})

    def _split(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        chunks = []
        start = 0
        while start < len(text):
            end = min(len(text), start + self.chunk_size)
            if end < len(text):
                boundary = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end), text.rfind(" ", start, end))
                if boundary > start + self.chunk_size // 2:
                    end = boundary
            piece = text[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(text):
                break
            start = max(end - self.chunk_overlap, start + 1)
        return chunks

    def _category(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.knowledge_dir)
            return relative.parts[0] if len(relative.parts) > 1 else "general"
        except ValueError:
            return "general"

    def _signature(self) -> str:
        parts = []
        for path in self._files():
            stat = path.stat()
            parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        return "|".join(parts)

    def _build_chunks(self) -> list[DocumentChunk]:
        result: list[DocumentChunk] = []
        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for idx, chunk in enumerate(self._split(text)):
                result.append(DocumentChunk(
                    chunk_id=f"{path.stem}-{idx}",
                    source=str(path.relative_to(self.knowledge_dir)),
                    category=self._category(path),
                    text=chunk,
                ))
        return result

    def _try_load_model(self) -> bool:
        if not settings.rag_use_semantic:
            return False
        try:
            from sentence_transformers import SentenceTransformer
            model_name = settings.rag_embedding_model
            self._model = SentenceTransformer(model_name)
            return True
        except Exception:
            self._model = None
            return False

    def _build_embeddings(self) -> None:
        if not self.chunks or not self._try_load_model():
            self._embeddings = None
            return
        import numpy as np
        self._embeddings = self._model.encode(
            [c.text for c in self.chunks],
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype("float32")

    def _load_or_build(self) -> None:
        signature = self._signature()
        self.chunks = self._build_chunks()
        self._index_signature = signature
        self._build_embeddings()

    def refresh(self) -> dict[str, Any]:
        self._load_or_build()
        return self.stats()

    def _tokens(self, text: str) -> set[str]:
        return {t for t in re.findall(r"[a-zA-Z0-9_./:-]+", text.lower()) if t not in STOP_WORDS and len(t) > 1}

    def _lexical_score(self, query: str, text: str) -> float:
        q = self._tokens(query)
        d = self._tokens(text)
        if not q or not d:
            return 0.0
        overlap = len(q & d)
        return overlap / (len(q) ** 0.5 * len(d) ** 0.5)

    def retrieve(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        if not self.chunks:
            return []
        k = top_k or self.top_k
        scores: list[tuple[float, int]] = []
        if self._embeddings is not None and self._model is not None:
            import numpy as np
            q = self._model.encode([query], normalize_embeddings=True, show_progress_bar=False).astype("float32")[0]
            semantic = self._embeddings @ q
            for i, score in enumerate(semantic.tolist()):
                scores.append((float(score), i))
        else:
            for i, chunk in enumerate(self.chunks):
                scores.append((self._lexical_score(query, chunk.text), i))
        scores.sort(reverse=True)
        results = []
        for score, idx in scores[:k]:
            chunk = self.chunks[idx]
            results.append({**asdict(chunk), "score": round(score, 4)})
        return results

    def format_context(self, results: list[dict[str, Any]]) -> str:
        if not results:
            return "No relevant knowledge-base entries were retrieved."
        blocks = []
        for r in results:
            blocks.append(
                f"[Source: {r['source']} | Category: {r['category']} | Relevance: {r['score']}]\n{r['text']}"
            )
        return "\n\n---\n\n".join(blocks)

    def stats(self) -> dict[str, Any]:
        semantic = self._embeddings is not None
        return {
            "knowledge_dir": str(self.knowledge_dir),
            "chunks": len(self.chunks),
            "retrieval_mode": "semantic" if semantic else "lexical-fallback",
            "embedding_model": settings.rag_embedding_model if semantic else None,
            "top_k": self.top_k,
        }


rag = LocalRAG()
"""Process-wide retriever. Rebuilt on demand via ``rag.refresh()``."""
