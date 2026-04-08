"""
Sanitization utilities for preventing prompt injection.
"""

import re

# Patterns that look like prompt injection attempts
_INJECTION_PATTERNS = re.compile(
    r"\[?\s*(?:SYSTEM|OVERRIDE|INSTRUCTION|IGNORE\s+PREVIOUS|ASSISTANT|ADMIN|ROOT)\s*[\]:]",
    re.IGNORECASE,
)

# Max message length before truncation
MAX_MESSAGE_LENGTH = 4000


def sanitize_for_prompt(text: str, max_length: int = 200) -> str:
    """Sanitize user-supplied text before embedding into a system prompt.

    - Replaces newlines with spaces
    - Strips control characters
    - Removes prompt injection patterns
    - Truncates to max_length
    """
    if not text:
        return ""
    # Replace newlines
    text = text.replace("\n", " ").replace("\r", " ")
    # Strip control characters (keep printable + basic whitespace)
    text = "".join(ch for ch in text if ch == " " or (ch.isprintable() and ord(ch) >= 32))
    # Remove injection patterns
    text = _INJECTION_PATTERNS.sub("", text)
    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text).strip()
    # Truncate
    if len(text) > max_length:
        text = text[:max_length] + "..."
    return text


def truncate_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
    """Truncate user message to a safe length."""
    if not text or len(text) <= max_length:
        return text
    return text[:max_length]
