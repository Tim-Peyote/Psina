"""Real embedding service for vector search.

Uses the configured LLM API (OpenAI-compatible) to generate embeddings.
Falls back to mock embeddings when API is unavailable.
Caches embeddings in memory to avoid redundant API calls.
"""

from __future__ import annotations

import hashlib
import structlog
from typing import Any

from openai import AsyncOpenAI

from src.config import settings

logger = structlog.get_logger()

# In-memory cache: text_hash -> embedding
_embedding_cache: dict[str, list[float]] = {}
_MAX_CACHE_SIZE = 10000


class EmbeddingService:
    """Generate and manage embeddings for memory items."""

    def __init__(self) -> None:
        self.dimension = settings.embedding_dimension
        self._client: AsyncOpenAI | None = None
        self._use_real_embeddings = bool(settings.llm_api_key) and settings.llm_provider != "mock"

    def _get_client(self) -> AsyncOpenAI | None:
        if self._client is not None:
            return self._client

        if not self._use_real_embeddings:
            return None

        try:
            self._client = AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
            )
            return self._client
        except Exception:
            logger.warning("Failed to create embedding client, falling back to mock")
            self._use_real_embeddings = False
            return None

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a text.

        Uses real embedding API if available, otherwise falls back to mock.
        Caches results to avoid redundant calls.
        """
        if not text or not text.strip():
            return [0.0] * self.dimension

        text_hash = hashlib.sha256(text.encode()).hexdigest()

        # Check cache
        if text_hash in _embedding_cache:
            return _embedding_cache[text_hash]

        client = self._get_client()

        if client is not None:
            embedding = await self._get_real_embedding(client, text)
        else:
            embedding = self._get_mock_embedding(text)

        # Cache result
        if len(_embedding_cache) < _MAX_CACHE_SIZE:
            _embedding_cache[text_hash] = embedding

        return embedding

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Batch embed multiple texts efficiently."""
        return [await self.embed_text(t) for t in texts]

    async def _get_real_embedding(self, client: AsyncOpenAI, text: str) -> list[float]:
        """Get embedding from real API."""
        try:
            # Try common embedding model endpoints
            # For OpenAI-compatible APIs, use text-embedding-3-small or similar
            response = await client.embeddings.create(
                model="text-embedding-3-small",
                input=text,
                dimensions=self.dimension if self.dimension <= 1536 else None,
            )

            embedding = response.data[0].embedding

            # If API returns different dimension, pad or truncate
            if len(embedding) != self.dimension:
                embedding = self._adjust_dimension(embedding)

            logger.debug("Real embedding generated", dim=len(embedding))
            return embedding

        except Exception:
            logger.exception("Failed to get real embedding, using mock")
            self._use_real_embeddings = False
            return self._get_mock_embedding(text)

    def _get_mock_embedding(self, text: str) -> list[float]:
        """Generate a deterministic mock embedding based on text hash.

        This creates a pseudo-random but reproducible vector from the text,
        useful for development without real API.
        """
        # Use text hash to create deterministic "random" values
        text_bytes = hashlib.sha256(text.encode()).digest()

        # Generate embedding from hash bytes (seeded)
        embedding = []
        for i in range(self.dimension):
            # Create deterministic value from text hash and position
            h = hashlib.md5(text_bytes + str(i).encode()).hexdigest()
            # Convert to float in [-1, 1]
            val = (int(h[:8], 16) / 0xFFFFFFFF) * 2 - 1
            embedding.append(val)

        # Normalize to unit vector
        magnitude = sum(x * x for x in embedding) ** 0.5
        if magnitude > 0:
            embedding = [x / magnitude for x in embedding]

        logger.debug("Mock embedding generated", dim=len(embedding))
        return embedding

    def _adjust_dimension(self, embedding: list[float]) -> list[float]:
        """Adjust embedding to match configured dimension."""
        current_dim = len(embedding)
        target_dim = self.dimension

        if current_dim == target_dim:
            return embedding

        if current_dim > target_dim:
            # Truncate
            return embedding[:target_dim]

        # Pad with zeros
        return embedding + [0.0] * (target_dim - current_dim)

    def serialize_embedding(self, embedding: list[float]) -> bytes:
        """Serialize embedding to bytes for storage."""
        return struct.pack(f"{len(embedding)}f", *embedding)

    def deserialize_embedding(self, data: bytes) -> list[float]:
        """Deserialize embedding from bytes."""
        count = len(data) // 4
        return list(struct.unpack(f"{count}f", data))

    def clear_cache(self) -> None:
        """Clear the embedding cache."""
        _embedding_cache.clear()

    @property
    def uses_real_embeddings(self) -> bool:
        return self._use_real_embeddings


# Singleton
embedding_service = EmbeddingService()
