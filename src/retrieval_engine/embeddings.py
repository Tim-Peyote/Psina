"""Embedding generation for vector search.

Uses pgvector for storage. Embeddings can be generated via
a local model or an API. This module provides the interface.
"""

import structlog

from src.config import settings

logger = structlog.get_logger()


class EmbeddingService:
    """Generate and manage embeddings for memory items."""

    def __init__(self) -> None:
        self.dimension = settings.embedding_dimension

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a text.
        
        In production, replace with actual embedding API call.
        Returns a zero vector as placeholder.
        """
        logger.debug("Generating embedding", text_length=len(text))
        return [0.0] * self.dimension

    def serialize_embedding(self, embedding: list[float]) -> bytes:
        """Serialize embedding to bytes for storage."""
        import struct
        return struct.pack(f"{len(embedding)}f", *embedding)

    def deserialize_embedding(self, data: bytes) -> list[float]:
        """Deserialize embedding from bytes."""
        import struct
        count = len(data) // 4
        return list(struct.unpack(f"{count}f", data))
