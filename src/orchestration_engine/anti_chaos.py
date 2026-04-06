"""
Anti-chaos protection — защитные механизмы от хаоса в групповых чатах.

- Rate limit ответов
- Cooldown между репликами
- Max параллельных сессий
- Блокировка при низкой уверенности
- Защита от разгона
"""

from datetime import datetime, timedelta, timezone

import structlog

from src.config import settings

logger = structlog.get_logger()


class AntiChaosProtection:
    """
    Защитные механизмы бота.
    """

    def __init__(self) -> None:
        # chat_id -> last bot response time
        self._last_response: dict[int, datetime] = {}
        # chat_id -> count of responses in last hour
        self._hourly_counts: dict[int, list[datetime]] = {}
        # chat_id -> cooldown until
        self._cooldown_until: dict[int, datetime] = {}
        # chat_id -> consecutive response count (anti-spam)
        self._consecutive: dict[int, int] = {}

        # Настройки из config
        self.min_cooldown_seconds: int = settings.anti_chaos_cooldown
        self.max_per_hour: int = settings.anti_chaos_max_per_hour
        self.max_consecutive: int = settings.anti_chaos_max_consecutive

    def can_respond(self, chat_id: int, is_urgent: bool = False) -> tuple[bool, str]:
        """
        Проверить — может ли бот ответить сейчас.
        Возвращает (can_respond, reason).
        """
        now = datetime.now(timezone.utc)

        # 1. Срочные (reply на бота) — пропускаем почти всегда
        if is_urgent:
            return True, "urgent"

        # 2. Cooldown
        cooldown_end = self._cooldown_until.get(chat_id)
        if cooldown_end and now < cooldown_end:
            remaining = (cooldown_end - now).total_seconds()
            return False, f"cooldown_active ({remaining:.0f}s)"

        # 3. Rate limit per hour
        hourly = self._hourly_counts.get(chat_id, [])
        cutoff = now - timedelta(hours=1)
        hourly = [t for t in hourly if t > cutoff]
        self._hourly_counts[chat_id] = hourly

        if len(hourly) >= self.max_per_hour:
            return False, f"hourly_limit_reached ({len(hourly)}/{self.max_per_hour})"

        # 4. Consecutive responses (anti-spam)
        consecutive = self._consecutive.get(chat_id, 0)
        if consecutive >= self.max_consecutive:
            return False, f"consecutive_limit ({consecutive}/{self.max_consecutive})"

        # 5. Min cooldown between responses
        last = self._last_response.get(chat_id)
        if last:
            elapsed = (now - last).total_seconds()
            if elapsed < self.min_cooldown_seconds:
                return False, f"min_cooldown ({elapsed:.0f}s < {self.min_cooldown_seconds}s)"

        return True, "ok"

    def record_response(self, chat_id: int) -> None:
        """Записать ответ бота."""
        now = datetime.now(timezone.utc)

        self._last_response[chat_id] = now
        self._cooldown_until[chat_id] = now + timedelta(seconds=self.min_cooldown_seconds)

        if chat_id not in self._hourly_counts:
            self._hourly_counts[chat_id] = []
        self._hourly_counts[chat_id].append(now)

        self._consecutive[chat_id] = self._consecutive.get(chat_id, 0) + 1

    def record_user_message(self, chat_id: int) -> None:
        """Сообщение от пользователя — сбрасывает consecutive счётчик."""
        self._consecutive[chat_id] = 0

    def set_cooldown(self, chat_id: int, seconds: int) -> None:
        """Принудительный cooldown."""
        self._cooldown_until[chat_id] = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def is_conversation_escalating(self, chat_id: int) -> bool:
        """
        Проверить — разговор "разгоняется"?
        Если бот отвечает слишком часто — пора притормозить.
        """
        hourly = self._hourly_counts.get(chat_id, [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = [t for t in hourly if t > cutoff]

        # Если больше половины лимита за последние 15 минут — разгон
        fifteen_min_ago = datetime.now(timezone.utc) - timedelta(minutes=15)
        very_recent = [t for t in recent if t > fifteen_min_ago]

        if len(very_recent) >= self.max_per_hour / 4:  # 25% лимита за 15 мин
            return True

        return False

    def get_status(self, chat_id: int) -> dict:
        """Получить статус защиты для чата."""
        now = datetime.now(timezone.utc)
        hourly = self._hourly_counts.get(chat_id, [])
        cutoff = now - timedelta(hours=1)
        hourly = len([t for t in hourly if t > cutoff])

        return {
            "hourly_count": hourly,
            "hourly_limit": self.max_per_hour,
            "consecutive": self._consecutive.get(chat_id, 0),
            "consecutive_limit": self.max_consecutive,
            "cooldown_active": self._cooldown_until.get(chat_id, datetime.min) > now,
            "escalating": self.is_conversation_escalating(chat_id),
        }


anti_chaos = AntiChaosProtection()
