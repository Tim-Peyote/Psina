"""
Proactive Engine — Псина сама начинает разговор.

Триггеры:
- Утреннее приветствие
- Follow-up по памяти (вчера упоминали событие — как прошло?)
- Тишина + высокий social_need
- Праздники
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import structlog

from src.config import settings

logger = structlog.get_logger()

_REDIS_PREFIX = "proactive:"

# Moscow timezone offset
_TZ_MSK = timezone(timedelta(hours=3))


class ProactiveEngine:
    """Decides when and what proactive messages to send."""

    def __init__(self) -> None:
        self._redis = None

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        return self._redis

    async def check_chat(self, chat_id: int) -> str | None:
        """Check if a proactive message should be sent to this chat.

        Returns message text or None.
        """
        now = datetime.now(_TZ_MSK)
        hour = now.hour

        # Respect quiet hours
        if settings.quiet_hours_start <= hour or hour < settings.quiet_hours_end:
            return None

        # Check rate limits
        if not await self._can_send(chat_id):
            return None

        # Try triggers in priority order
        msg = await self._try_morning_greeting(chat_id, now)
        if msg:
            await self._record_sent(chat_id, "morning")
            return msg

        msg = await self._try_memory_followup(chat_id, now)
        if msg:
            await self._record_sent(chat_id, "followup")
            return msg

        msg = await self._try_silence_break(chat_id, now)
        if msg:
            await self._record_sent(chat_id, "silence_break")
            return msg

        msg = await self._try_holiday_greeting(chat_id, now)
        if msg:
            await self._record_sent(chat_id, "holiday")
            return msg

        return None

    async def _can_send(self, chat_id: int) -> bool:
        """Check proactive rate limits."""
        try:
            r = await self._get_redis()
            key = f"{_REDIS_PREFIX}count:{chat_id}"
            count = await r.get(key)
            if count and int(count) >= settings.proactive_max_per_hour:
                return False

            # Check cooldown
            last_key = f"{_REDIS_PREFIX}last:{chat_id}"
            last = await r.get(last_key)
            if last:
                last_ts = datetime.fromisoformat(last)
                if (datetime.now(timezone.utc) - last_ts).total_seconds() < settings.proactive_cooldown_seconds:
                    return False
        except Exception:
            logger.debug("Redis check failed in proactive engine", exc_info=True)
            return False
        return True

    async def _record_sent(self, chat_id: int, trigger_type: str) -> None:
        """Record that a proactive message was sent."""
        try:
            r = await self._get_redis()
            now = datetime.now(timezone.utc)

            # Increment hourly counter
            key = f"{_REDIS_PREFIX}count:{chat_id}"
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, 3600)
            await pipe.execute()

            # Set last sent timestamp
            last_key = f"{_REDIS_PREFIX}last:{chat_id}"
            await r.set(last_key, now.isoformat(), ex=settings.proactive_cooldown_seconds)

            # Record type for dedup
            type_key = f"{_REDIS_PREFIX}type:{chat_id}:{trigger_type}"
            await r.set(type_key, "1", ex=86400)

            logger.info("Proactive message sent", chat_id=chat_id, trigger=trigger_type)
        except Exception:
            logger.debug("Redis record failed in proactive engine", exc_info=True)

    async def _already_sent_today(self, chat_id: int, trigger_type: str) -> bool:
        """Check if this trigger type was already sent today."""
        try:
            r = await self._get_redis()
            return bool(await r.get(f"{_REDIS_PREFIX}type:{chat_id}:{trigger_type}"))
        except Exception:
            return True  # Fail safe: assume already sent

    # ===== TRIGGER IMPLEMENTATIONS =====

    async def _try_morning_greeting(self, chat_id: int, now: datetime) -> str | None:
        """Morning greeting between 8-10 if not sent today."""
        if not (8 <= now.hour <= 10):
            return None

        if await self._already_sent_today(chat_id, "morning"):
            return None

        # Check emotional state for tone
        from src.orchestration_engine.emotional_state import emotional_state_manager
        state = await emotional_state_manager.get_state(chat_id)

        greetings_happy = [
            "Доброе утро! Как спалось? *потягивается*",
            "Утро! Новый день, новые приключения",
            "Доброе утречко! Я бодрый и готов к делу",
            "Ку! Утро — самое время для хороших дел",
        ]
        greetings_neutral = [
            "Утро...",
            "Доброе утро. Ну как-то так.",
            "*зевает* Утро...",
        ]
        greetings_low = [
            "*еле проснулся* ...утро",
            "М... утро... нужен кофе...",
        ]

        if state.mood > 0.3:
            return random.choice(greetings_happy)
        elif state.mood > -0.2:
            return random.choice(greetings_neutral)
        else:
            return random.choice(greetings_low)

    async def _try_memory_followup(self, chat_id: int, now: datetime) -> str | None:
        """Follow up on events mentioned yesterday.

        Queries memory for items about future events/plans that should have
        happened by now.
        """
        if await self._already_sent_today(chat_id, "followup"):
            return None

        # Only try once in the afternoon (14-17)
        if not (14 <= now.hour <= 17):
            return None

        try:
            from src.memory_services.retrieval_service import retrieval_service

            # Search for recent memories about plans/events
            results = await retrieval_service.search(
                query="собеседование встреча экзамен дедлайн",
                chat_id=chat_id,
                top_k=5,
            )

            # Filter for items from 1-2 days ago with high importance
            cutoff = now - timedelta(days=2)
            relevant = []
            for r in results:
                if r.created_at and r.created_at > cutoff and r.score > 0.5:
                    # Check if content mentions future event
                    keywords = ["собеседование", "встреча", "экзамен", "дедлайн", "презентация", "интервью"]
                    if any(kw in r.content.lower() for kw in keywords):
                        relevant.append(r)

            if not relevant:
                return None

            # Pick the most relevant one
            best = max(relevant, key=lambda x: x.score)
            content = best.content[:100]

            followups = [
                f"Кстати, вчера упоминали: {content}. Как прошло?",
                f"Помню, говорили про: {content}. Ну что, как?",
                f"Эй, а что там с «{content}»? Рассказывайте!",
            ]
            return random.choice(followups)

        except Exception:
            logger.debug("Memory followup failed", exc_info=True)
            return None

    async def _try_silence_break(self, chat_id: int, now: datetime) -> str | None:
        """Break silence if chat has been quiet during active hours and social need is high."""
        # Only during active hours (10-22)
        if not (10 <= now.hour <= 22):
            return None

        from src.orchestration_engine.emotional_state import emotional_state_manager
        state = await emotional_state_manager.get_state(chat_id)

        # Only if social need is high
        if state.social_need < 0.7:
            return None

        # Check if chat has been silent for a while
        idle_hours = (now.astimezone(timezone.utc) - state.last_interaction).total_seconds() / 3600
        if idle_hours < 3:
            return None

        starters = [
            "Тихо тут... Чем занимаетесь?",
            "Что-то все молчат... Живы?",
            "*оглядывается* Эй, ку-ку! Тут есть кто?",
            "Скучно... Расскажите что-нибудь интересное!",
            "Давно никто не писал. Всё ок?",
        ]
        return random.choice(starters)

    async def _try_holiday_greeting(self, chat_id: int, now: datetime) -> str | None:
        """Congratulate on holidays."""
        from src.orchestration_engine.personality import PsinaPersonality

        holiday = PsinaPersonality._HOLIDAYS.get((now.month, now.day))
        if not holiday:
            return None

        if await self._already_sent_today(chat_id, "holiday"):
            return None

        # Only send in the morning (9-12)
        if not (9 <= now.hour <= 12):
            return None

        greetings = [
            f"С {holiday}! Хорошего дня!",
            f"Поздравляю с {holiday}!",
            f"Сегодня {holiday}. Празднуем?",
        ]
        return random.choice(greetings)


# Singleton
proactive_engine = ProactiveEngine()
