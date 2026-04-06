"""
Reaction Engine — контекстные реакции Псины на сообщения.

Псина ставит реакции когда есть причина:
- 👍 — согласился, подтвердил, сказал "ок"
- ❤️ — похвалил бота, сказал что-то тёплое
- 😂 — шутка, смешное
- 👀 — что-то интересное/неожиданное
- 🔥 — что-то крутое/восхищение

Не спамит: cooldown, max 1 реакция, только когда реально есть причина.
"""

import re
from datetime import datetime, timedelta, timezone

import structlog

from src.message_processor.processor import NormalizedMessage

logger = structlog.get_logger()


class ReactionEngine:
    """
    Решает — поставить реакцию на сообщение или нет.
    """

    def __init__(self) -> None:
        self._last_reaction: dict[int, datetime] = {}
        self._cooldown_seconds = 60

        self._reaction_patterns: dict[str, list[str]] = {
            "❤️": [
                r"(?:псина|п[её]с|п[её]сик)\s*(?:ты|классн|крут|лучш|молодец|хорош|умн|класс|огонь|супер|годнот|топ)",
                r"(?:люблю тебя|ты лучш|ты классн|ты крут|ты молодец|ты огонь|ты топ|ты годный)",
                r"(?:спасибо|благодарю|ценю)\s*(?:тебе|псина|п[её]с|п[её]сик|брат|друг|чувак)",
                r"(?:ты хорош|ты классн|ты крут|ты лучш)\s*(?:п[её]с|псина|бот|друг|брат)",
                r"(?:обнимаю|целую|люблю|скучаю)\s*(?:по тебе|за тебя|тебя)",
            ],
            "😂": [
                r"(?:лол|ржу|ахах|хаха|хах|орал|угар|бляха|пиздец)\s*(?:ну|а|да|вот)?$",
                r"(?:это смешно|это угар|я ору|я ржу|умер со смеху|ахах|лол|ржу не могу)",
                r"(?:шутка|анекдот|прикол|байка|история смешная)",
            ],
            "🔥": [
                r"(?:огонь|топ|пушка|бомба|красавчик|красотка|лучшее|крутое|шикарно)",
                r"(?:вау|офигеть|охуеть|ебать|нифига себе|ничего себе)\s*(?:красота|круто|мощь)?",
            ],
            "👀": [
                r"(?:опа|ого|ничего себе|вот это да|вот это поворот|сюрприз|неожиданно)",
                r"(?:слушайте|смотрите|представляете|представьте)\s*(?:что|как|какой)",
            ],
            "👍": [
                r"(?:согласен|точно|да да|правильно|верно|базара нет|конечно|естественно)",
                r"(?:ок|окей|договорились|по рукам|ладно|понял|ясно|принято)\s*(?:!|\.|)$",
                r"(?:норм|нормально|хорошо|отлично|супер|класс)",
            ],
        }

        # Эмодзи которые НЕ считаются поводом для реакции (чтобы не спамить на эмодзи-сообщения)
        self._emoji_only_pattern = re.compile(
            r'^[\s\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U00002702-\U000027B0\U000024C2-\U0001F251]+$'
        )

    def should_react(self, msg: NormalizedMessage) -> str | None:
        """
        Решить — поставить реакцию или нет.
        Возвращает эмодзи реакции или None.
        """
        if not self._is_cooldown_ok(msg.chat_id):
            return None

        if msg.is_command:
            return None

        text = msg.text.strip()
        if len(text) < 3:
            return None

        # Не реагируем на сообщения состоящие только из эмодзи
        if self._emoji_only_pattern.match(text):
            return None

        for emoji, patterns in self._reaction_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    self._last_reaction[msg.chat_id] = datetime.now(timezone.utc)
                    logger.debug("Reaction chosen", emoji=emoji, chat_id=msg.chat_id)
                    return emoji

        return None

    def _is_cooldown_ok(self, chat_id: int) -> bool:
        last = self._last_reaction.get(chat_id)
        if last is None:
            return True
        return datetime.now(timezone.utc) - last > timedelta(seconds=self._cooldown_seconds)


reaction_engine = ReactionEngine()
