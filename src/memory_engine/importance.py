"""Importance scoring for memory items."""

from src.message_processor.processor import NormalizedMessage


def calculate_importance(msg: NormalizedMessage) -> float:
    """
    Calculate importance score for a message.
    Returns a value between 0.0 and 1.0.
    """
    score = 0.0

    # Commands are important
    if msg.is_command:
        score += 0.3

    # Mentions of the bot are important
    if msg.is_mention_bot:
        score += 0.3

    # Longer messages tend to be more informative
    text_len = len(msg.text)
    if text_len > 100:
        score += 0.2
    elif text_len > 50:
        score += 0.1

    # Messages in private chats are more important
    if msg.is_private:
        score += 0.2

    # Replies add some importance
    if msg.reply_to_message_id:
        score += 0.1

    return min(score, 1.0)


def calculate_relevance(memory_age_hours: float, initial_relevance: float = 1.0) -> float:
    """
    Calculate relevance decay based on age.
    Uses exponential decay with half-life of 72 hours.
    """
    half_life = 72.0
    decay = 0.5 ** (memory_age_hours / half_life)
    return max(initial_relevance * decay, 0.05)
