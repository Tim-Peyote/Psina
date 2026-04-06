"""Deduplication for memory items."""

import hashlib


def content_hash(text: str) -> str:
    """Generate a hash for content deduplication."""
    normalized = text.strip().lower()
    return hashlib.md5(normalized.encode()).hexdigest()


def is_duplicate(existing_hashes: set[str], text: str, threshold: float = 0.9) -> bool:
    """
    Check if content is a duplicate based on exact hash match.
    For fuzzy matching, use embedding similarity in production.
    """
    return content_hash(text) in existing_hashes
