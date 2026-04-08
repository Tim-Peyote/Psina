"""
Vibe Adapter — подстройка под стиль общения чата.

Псина анализирует как общаются в конкретном чате и адаптирует свой стиль:
- Формальный / неформальный
- Есть мат / нет
- Длина сообщений
- Частота эмодзи
- Общее настроение

Профили сохраняются в БД (ChatVibeProfile) и восстанавливаются при старте.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from src.config import settings
from src.database.session import get_session
from src.database.models import ChatVibeProfile

logger = structlog.get_logger()


@dataclass
class VibeProfile:
    """Профиль вайба конкретного чата."""

    chat_id: int

    # Формальность: 0.0 = очень неформальный, 1.0 = официальный
    formality: float = 0.3

    # Есть мат: 0.0 = чисто, 1.0 = много мата
    mate_level: float = 0.0

    # Средняя длина сообщения (символы)
    avg_length: float = 50.0

    # Частота эмодзи: 0.0 = нет, 1.0 = много
    emoji_frequency: float = 0.2

    # Общее настроение: negative, neutral, positive
    mood: str = "neutral"

    # Количество проанализированных сообщений
    messages_analyzed: int = 0

    # Последнее обновление
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_formal(self) -> bool:
        return self.formality > 0.7

    @property
    def has_mate(self) -> bool:
        return self.mate_level > 0.3

    @property
    def is_emoji_heavy(self) -> bool:
        return self.emoji_frequency > 0.5

    @property
    def style_hint(self) -> str:
        """Подсказка стиля для LLM."""
        hints = []

        if self.is_formal:
            hints.append("Общайся формально и вежливо.")
        else:
            hints.append("Можешь общаться неформально, как друг.")

        if self.has_mate:
            hints.append("В чате есть мат — можешь не фильтровать себя.")
        else:
            hints.append("В чате не матюкаются — придержи приличный язык.")

        if self.is_emoji_heavy:
            hints.append("В чате любят эмодзи — можешь использовать их.")
        else:
            hints.append("Эмодзи тут не особо используют — не переборщи.")

        if self.mood == "positive":
            hints.append("Настроение в чате хорошее — будь позитивным.")
        elif self.mood == "negative":
            hints.append("Настроение в чате напряжённое — будь аккуратнее.")

        return " ".join(hints)


class VibeAdapter:
    """
    Анализирует и адаптирует стиль общения под чат.
    Профили сохраняются в БД и восстанавливаются при старте.
    """

    def __init__(self) -> None:
        self._profiles: dict[int, VibeProfile] = {}
        # Слова для детекции мата (базовые)
        self._mate_words = {
            "бля", "блять", "сука", "нахуй", "пизд", "ебан", "хуй", "пизд",
            "ёб", "мудак", "жопа", "дерьмо", "залуп", "ёпта", "ёкарн",
        }
        # Слова для формальности
        self._formal_markers = {
            "пожалуйста", "будьте добры", "уважаемый", "прошу",
            "благодарю", "позвольте", "имею честь", "господин",
            "здравствуйте", "добрый день", "добрый вечер",
        }
        self._informal_markers = {
            "привет", "здарова", "хай", "чё", "ща", "короче",
            "блин", "типа", "капец", "жесть", "лол", "аха",
            "давай", "норм", "ок", "го",
        }

    async def _load_profile_from_db(self, chat_id: int) -> VibeProfile | None:
        """Load vibe profile from DB."""
        async for session in get_session():
            stmt = select(ChatVibeProfile).where(ChatVibeProfile.chat_id == chat_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                profile = VibeProfile(
                    chat_id=row.chat_id,
                    formality=row.formality,
                    mate_level=row.mate_level,
                    avg_length=row.avg_length,
                    emoji_frequency=row.emoji_frequency,
                    mood=row.mood,
                    messages_analyzed=row.messages_analyzed,
                    last_updated=row.updated_at or datetime.now(timezone.utc),
                )
                self._profiles[chat_id] = profile
                return profile
        return None

    async def _save_profile_to_db(self, profile: VibeProfile) -> None:
        """Persist vibe profile to DB."""
        async for session in get_session():
            stmt = select(ChatVibeProfile).where(ChatVibeProfile.chat_id == profile.chat_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                row.formality = profile.formality
                row.mate_level = profile.mate_level
                row.avg_length = profile.avg_length
                row.emoji_frequency = profile.emoji_frequency
                row.mood = profile.mood
                row.messages_analyzed = profile.messages_analyzed
            else:
                session.add(
                    ChatVibeProfile(
                        chat_id=profile.chat_id,
                        formality=profile.formality,
                        mate_level=profile.mate_level,
                        avg_length=profile.avg_length,
                        emoji_frequency=profile.emoji_frequency,
                        mood=profile.mood,
                        messages_analyzed=profile.messages_analyzed,
                    )
                )
            await session.commit()

    async def load_all_profiles(self) -> None:
        """Load all vibe profiles from DB at startup."""
        async for session in get_session():
            stmt = select(ChatVibeProfile)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            for row in rows:
                profile = VibeProfile(
                    chat_id=row.chat_id,
                    formality=row.formality,
                    mate_level=row.mate_level,
                    avg_length=row.avg_length,
                    emoji_frequency=row.emoji_frequency,
                    mood=row.mood,
                    messages_analyzed=row.messages_analyzed,
                    last_updated=row.updated_at or datetime.now(timezone.utc),
                )
                self._profiles[profile.chat_id] = profile
        logger.info("Vibe profiles loaded from DB", count=len(self._profiles))

    def get_profile(self, chat_id: int) -> VibeProfile:
        """Получить или создать вайб-профиль чата."""
        if chat_id not in self._profiles:
            self._profiles[chat_id] = VibeProfile(chat_id=chat_id)
        return self._profiles[chat_id]

    def analyze_message(self, chat_id: int, text: str) -> VibeProfile:
        """
        Проанализировать сообщение и обновить вайб-профиль чата.
        """
        profile = self.get_profile(chat_id)
        profile.messages_analyzed += 1
        profile.last_updated = datetime.now(timezone.utc)

        # 1. Мат
        text_lower = text.lower()
        mate_count = sum(1 for w in self._mate_words if w in text_lower)
        alpha = 0.1
        if mate_count > 0:
            # Плавное обновление вверх
            profile.mate_level = profile.mate_level * (1 - alpha) + 1.0 * alpha
        else:
            # Медленный decay вниз если мата нет
            profile.mate_level = max(0.0, profile.mate_level * (1 - alpha * 0.3))

        # 2. Формальность
        formal_count = sum(1 for w in self._formal_markers if w in text_lower)
        informal_count = sum(1 for w in self._informal_markers if w in text_lower)

        if formal_count > 0 or informal_count > 0:
            alpha = 0.05
            if formal_count > informal_count:
                target = 1.0
            else:
                target = 0.0
            profile.formality = profile.formality * (1 - alpha) + target * alpha

        # 3. Длина
        alpha = 0.05
        profile.avg_length = profile.avg_length * (1 - alpha) + len(text) * alpha

        # 4. Эмодзи
        emoji_count = len(re.findall(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U00002702-\U000027B0\U000024C2-\U0001F251]', text))
        if emoji_count > 0:
            alpha = 0.1
            profile.emoji_frequency = profile.emoji_frequency * (1 - alpha) + 1.0 * alpha
        else:
            # Небольшое уменьшение если эмодзи нет
            alpha = 0.01
            profile.emoji_frequency = max(0.0, profile.emoji_frequency * (1 - alpha))

        # 5. Настроение
        profile.mood = self._detect_mood(text_lower)

        # Ограничиваем после достаточного количества данных
        if profile.messages_analyzed > 10:
            profile.mate_level = max(0.0, min(1.0, profile.mate_level))
            profile.formality = max(0.0, min(1.0, profile.formality))
            profile.emoji_frequency = max(0.0, min(1.0, profile.emoji_frequency))

        # Persist to DB every 10 messages to avoid excessive writes
        if profile.messages_analyzed % 10 == 0:
            # Fire-and-forget: schedule save without blocking
            import asyncio
            asyncio.ensure_future(self._save_profile_to_db(profile))

        return profile

    def get_style_instruction(self, chat_id: int) -> str:
        """
        Получить инструкцию стиля для LLM.
        """
        profile = self.get_profile(chat_id)

        if profile.messages_analyzed < 5:
            return "Общайся нейтрально и дружелюбно. Пока мало данных о стиле чата."

        instructions = []

        # Формальность
        if profile.is_formal:
            instructions.append("В чате формальный стиль — обращайся на 'вы', будь вежлив и корреектен.")
        else:
            instructions.append(f"В чате неформальный стиль (formality={profile.formality:.1f}) — обращайся на 'ты', как с другом, можешь использовать сленг.")

        # Мат
        if profile.has_mate:
            instructions.append(f"В чате высокий уровень мата (mate={profile.mate_level:.1f}) — можешь использовать ненормативную лексику уместно, не перебарщивая.")
        else:
            instructions.append("В чате приличный язык — не используй мат.")

        # Эмодзи
        if profile.is_emoji_heavy:
            instructions.append(f"В чате любят эмодзи (emoji={profile.emoji_frequency:.1f}) — ставь их уместно.")
        else:
            instructions.append("Эмодзи используют редко — не ставь без необходимости.")

        # Настроение
        if profile.mood == "positive":
            instructions.append("Настроение в чате позитивное — будь лёгким и весёлым.")
        elif profile.mood == "negative":
            instructions.append("Настроение в чате напряжённое — будь аккуратнее, не провоцируй.")
        else:
            instructions.append("Настроение нейтральное — отвечай по делу.")

        # Длина сообщений
        if profile.avg_length < 30:
            instructions.append(f"В чате пишут коротко (avg {profile.avg_length:.0f} символов) — не отвечай длинно без необходимости.")
        elif profile.avg_length > 200:
            instructions.append(f"В чате пишут развёрнуто (avg {profile.avg_length:.0f} символов) — можешь отвечать подробно.")

        return "\n".join(instructions)

    def _detect_mood(self, text: str) -> str:
        """Определить настроение текста."""
        positive_words = {
            "класс", "круто", "супер", "отлично", "здорово", "рад", "радостно",
            "люблю", "нравится", "кайф", "огонь", "топ", "красавчик",
            "хаха", "лол", "😂", "🤣", "😊", "🔥", "👍",
        }
        negative_words = {
            "плохо", "ужас", "отстой", "беси", "злюсь", "ненавижу",
            "грустно", "печаль", "расстро", "дерьмо", "кошмар", "жесть",
            "😢", "😭", "😡", "👎", "💩",
        }

        pos = sum(1 for w in positive_words if w in text)
        neg = sum(1 for w in negative_words if w in text)

        if pos > neg:
            return "positive"
        elif neg > pos:
            return "negative"
        return "neutral"


vibe_adapter = VibeAdapter()
