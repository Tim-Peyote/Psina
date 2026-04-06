"""Rate limiting for bot proactivity."""

from datetime import datetime, timedelta, timezone

from src.config import settings


class RateLimiter:
    """Simple in-memory rate limiter for proactive messages."""

    def __init__(self) -> None:
        self._last_proactive: dict[int, datetime] = {}
        self._hourly_counts: dict[int, list[datetime]] = {}

    def can_send_proactive(self, chat_id: int) -> bool:
        """Check if we can send a proactive message to this chat."""
        now = datetime.now(timezone.utc)

        # Check cooldown
        last = self._last_proactive.get(chat_id)
        if last and (now - last).total_seconds() < settings.proactive_cooldown_seconds:
            return False

        # Check hourly limit
        hourly = self._hourly_counts.get(chat_id, [])
        # Remove entries older than 1 hour
        cutoff = now - timedelta(hours=1)
        hourly = [t for t in hourly if t > cutoff]
        self._hourly_counts[chat_id] = hourly

        if len(hourly) >= settings.proactive_max_per_hour:
            return False

        return True

    def record_proactive(self, chat_id: int) -> None:
        """Record a proactive message."""
        now = datetime.now(timezone.utc)
        self._last_proactive[chat_id] = now
        if chat_id not in self._hourly_counts:
            self._hourly_counts[chat_id] = []
        self._hourly_counts[chat_id].append(now)

    def is_quiet_hours(self) -> bool:
        """Check if we're currently in quiet hours."""
        now = datetime.now(timezone.utc)
        start = settings.quiet_hours_start
        end = settings.quiet_hours_end

        if start > end:
            # Quiet hours span midnight (e.g., 23:00 - 07:00)
            return now.hour >= start or now.hour < end
        else:
            return start <= now.hour < end


rate_limiter = RateLimiter()
