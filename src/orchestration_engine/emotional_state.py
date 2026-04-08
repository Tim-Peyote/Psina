"""
Emotional State Engine — эмоциональное состояние бота.

Бот не просто отвечает по настроению сообщения —
у него есть непрерывное эмоциональное состояние,
которое эволюционирует от взаимодействий и времени.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog

from src.config import settings

logger = structlog.get_logger()

_REDIS_PREFIX = "emo:"
_REDIS_TTL = 86400 * 7  # 7 days


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


@dataclass
class EmotionalState:
    """Emotional state of bot for a specific chat."""

    chat_id: int
    # -1.0 (angry/sad) .. 1.0 (happy/excited)
    mood: float = 0.3
    # 0.0 (tired) .. 1.0 (hyper)
    energy: float = 0.6
    # 0.0 (satisfied) .. 1.0 (lonely/wants to chat)
    social_need: float = 0.3
    # per-user trust: user_id -> float (0..1)
    trust: dict[int, float] = field(default_factory=dict)
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_interaction: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ===== POSITIVE SIGNALS =====

    _POSITIVE_WORDS = [
        "спасибо", "молодец", "круто", "класс", "отлично", "красава", "топ",
        "лучший", "люблю", "обожаю", "ахах", "хаха", "лол", "ржу",
        "умница", "гений", "прикольно", "здорово", "огонь", "бро",
    ]

    _NEGATIVE_WORDS = [
        "тупой", "бесишь", "достал", "отвали", "заткнись", "идиот",
        "бесполезный", "уйди", "нахуй", "дебил", "молчи",
    ]

    def process_message(self, text: str, user_id: int, is_directed_at_bot: bool) -> None:
        """Update emotional state based on incoming message."""
        now = datetime.now(timezone.utc)

        # Time-based decay first
        self._apply_time_decay(now)

        text_lower = text.lower()

        # Mood adjustment
        mood_delta = 0.0
        if any(w in text_lower for w in self._POSITIVE_WORDS):
            mood_delta += 0.15
        if any(w in text_lower for w in self._NEGATIVE_WORDS):
            mood_delta -= 0.2

        # Being called = energy boost
        if is_directed_at_bot:
            self.energy = _clamp(self.energy + 0.1)
            self.social_need = _clamp(self.social_need - 0.15)

        # Apply mood
        self.mood = _clamp(self.mood + mood_delta, -1.0, 1.0)

        # Trust update
        current_trust = self.trust.get(user_id, 0.5)
        if mood_delta > 0:
            self.trust[user_id] = _clamp(current_trust + 0.05)
        elif mood_delta < 0:
            self.trust[user_id] = _clamp(current_trust - 0.1)
        else:
            # Neutral interaction slightly builds trust
            self.trust[user_id] = _clamp(current_trust + 0.01)

        # Social need decreases with any interaction
        self.social_need = _clamp(self.social_need - 0.05)

        self.last_interaction = now
        self.last_updated = now

    def _apply_time_decay(self, now: datetime) -> None:
        """Apply natural decay/change over time."""
        elapsed = (now - self.last_updated).total_seconds() / 3600  # hours
        if elapsed < 0.1:
            return

        # Mood decays toward neutral (0.0)
        decay_factor = 0.95 ** elapsed
        self.mood *= decay_factor

        # Energy slowly decays
        self.energy = _clamp(self.energy - 0.02 * elapsed)

        # Social need grows when idle
        idle_hours = (now - self.last_interaction).total_seconds() / 3600
        self.social_need = _clamp(self.social_need + 0.03 * idle_hours)

        # Energy resets partially at "morning" (check if crossed 8am MSK)
        hour = now.hour
        if 7 <= hour <= 9 and elapsed > 1:
            self.energy = _clamp(self.energy + 0.2)

    def get_user_trust(self, user_id: int) -> float:
        """Get trust level for a specific user."""
        return self.trust.get(user_id, 0.5)

    def get_prompt_hint(self) -> str:
        """Generate a personality hint for the system prompt based on current state."""
        parts = []

        # Mood
        if self.mood > 0.5:
            parts.append("Ты сейчас в отличном настроении — шути, будь открытым и дружелюбным.")
        elif self.mood > 0.1:
            parts.append("Настроение хорошее, обычный дружелюбный тон.")
        elif self.mood > -0.2:
            parts.append("Нейтральное настроение. Спокойный тон.")
        elif self.mood > -0.5:
            parts.append("Настроение так себе. Отвечай покороче, без лишнего энтузиазма.")
        else:
            parts.append("Настроение хреновое. Отвечай сдержанно, можешь огрызнуться.")

        # Energy
        if self.energy > 0.7:
            parts.append("Полон энергии — можно развёрнуто отвечать и проявлять инициативу.")
        elif self.energy < 0.3:
            parts.append("Мало энергии — отвечай коротко и по делу.")

        # Social need
        if self.social_need > 0.7:
            parts.append("Хочется общения — можно проявить инициативу, задать вопрос.")

        return " ".join(parts)

    def get_user_tone_hint(self, user_id: int) -> str:
        """Get tone hint for a specific user based on trust."""
        trust = self.get_user_trust(user_id)
        if trust > 0.8:
            return "Это близкий друг — общайся тепло, можешь шутить и подкалывать."
        elif trust > 0.6:
            return "Хороший знакомый — дружелюбный тон."
        elif trust > 0.3:
            return "Обычный участник — нейтральный тон."
        else:
            return "К этому человеку относись настороженно — сдержанный тон."

    # ===== SERIALIZATION =====

    def to_dict(self) -> dict:
        # Keep only top-50 users by trust to avoid unbounded growth
        sorted_trust = sorted(self.trust.items(), key=lambda x: x[1], reverse=True)[:50]
        return {
            "mood": round(self.mood, 3),
            "energy": round(self.energy, 3),
            "social_need": round(self.social_need, 3),
            "trust": {str(uid): round(t, 3) for uid, t in sorted_trust},
            "last_updated": self.last_updated.isoformat(),
            "last_interaction": self.last_interaction.isoformat(),
        }

    @classmethod
    def from_dict(cls, chat_id: int, data: dict) -> EmotionalState:
        trust = {}
        for k, v in data.get("trust", {}).items():
            try:
                trust[int(k)] = float(v)
            except (ValueError, TypeError):
                pass
        return cls(
            chat_id=chat_id,
            mood=data.get("mood", 0.3),
            energy=data.get("energy", 0.6),
            social_need=data.get("social_need", 0.3),
            trust=trust,
            last_updated=datetime.fromisoformat(data["last_updated"]) if "last_updated" in data else datetime.now(timezone.utc),
            last_interaction=datetime.fromisoformat(data["last_interaction"]) if "last_interaction" in data else datetime.now(timezone.utc),
        )


class EmotionalStateManager:
    """Manages emotional states per chat, persisted in Redis."""

    def __init__(self) -> None:
        self._states: dict[int, EmotionalState] = {}
        self._redis = None

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        return self._redis

    async def get_state(self, chat_id: int) -> EmotionalState:
        """Get or load emotional state for a chat."""
        if chat_id in self._states:
            return self._states[chat_id]

        # Try loading from Redis
        try:
            r = await self._get_redis()
            data = await r.get(f"{_REDIS_PREFIX}{chat_id}")
            if data:
                state = EmotionalState.from_dict(chat_id, json.loads(data))
                self._states[chat_id] = state
                return state
        except Exception:
            logger.debug("Redis load failed for emotional state", chat_id=chat_id, exc_info=True)

        state = EmotionalState(chat_id=chat_id)
        self._states[chat_id] = state
        return state

    async def save_state(self, state: EmotionalState) -> None:
        """Persist emotional state to Redis."""
        self._states[state.chat_id] = state
        try:
            r = await self._get_redis()
            await r.set(
                f"{_REDIS_PREFIX}{state.chat_id}",
                json.dumps(state.to_dict()),
                ex=_REDIS_TTL,
            )
        except Exception:
            logger.debug("Redis save failed for emotional state", chat_id=state.chat_id, exc_info=True)

    async def process_message(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        is_directed_at_bot: bool,
    ) -> EmotionalState:
        """Process a message and update emotional state."""
        state = await self.get_state(chat_id)
        state.process_message(text, user_id, is_directed_at_bot)
        await self.save_state(state)
        return state


# Singleton
emotional_state_manager = EmotionalStateManager()
