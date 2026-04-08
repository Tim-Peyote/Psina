"""
Context tracker — отслеживает живой контекст разговора.

Понимает:
- Кто кому отвечает (reply chains)
- О ком говорят (mentions, имена в тексте)
- Текущую тему разговора
- Активных участников
- Временные окна разговоров
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re

import structlog

from src.message_processor.processor import NormalizedMessage
from src.database.session import get_session
from src.database.models import Message, User
from sqlalchemy import select

logger = structlog.get_logger()

# Паттерн для поиска упоминаний имён в тексте
# Ищет слова с заглавной буквы и @username
NAME_PATTERN = re.compile(r'(?:@(\w+)|\b([А-ЯA-Z][а-яa-z]{2,15})\b)')


@dataclass
class ConversationThread:
    """Одна ветка разговора — кто, кому, о ком."""

    chat_id: int
    topic: str = ""
    participants: set[int] = field(default_factory=set)
    mentioned_users: set[int] = field(default_factory=set)
    messages: list[dict] = field(default_factory=list)
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reply_chain: list[int] = field(default_factory=list)  # telegram message IDs

    @property
    def is_active(self) -> bool:
        """Разговор активен, если последняя активность < 15 мин назад."""
        return (datetime.now(timezone.utc) - self.last_activity) < timedelta(minutes=15)

    def add_message(self, msg: NormalizedMessage) -> None:
        self.participants.add(msg.user_id)
        self.messages.append({
            "user_id": msg.user_id,
            "username": msg.username,
            "first_name": msg.first_name,
            "text": msg.text,
            "timestamp": msg.created_at.isoformat(),
            "telegram_id": msg.telegram_id,
        })
        self.last_activity = msg.created_at
        self.reply_chain.append(msg.telegram_id)

    def add_mentioned_user(self, user_id: int) -> None:
        if user_id:
            self.mentioned_users.add(user_id)


class ContextTracker:
    """
    Живой контекст чата.

    Отслеживает:
    - Активные разговоры
    - Кто о ком говорит
    - Цепочки ответов
    - Темы разговоров
    """

    def __init__(self) -> None:
        # chat_id -> ConversationThread
        self._active_threads: dict[int, ConversationThread] = {}
        # user_id -> set of known names/aliases
        self._user_aliases: dict[int, set[str]] = {}
        # name/username -> user_id
        self._name_to_user: dict[str, int] = {}

    def track_message(self, msg: NormalizedMessage) -> ConversationThread:
        """
        Обработать новое сообщение и обновить контекст.
        Возвращает актуальный ConversationThread.
        """
        chat_id = msg.chat_id

        # Получаем или создаём тред
        if chat_id not in self._active_threads or not self._active_threads[chat_id].is_active:
            self._active_threads[chat_id] = ConversationThread(chat_id=chat_id)

        thread = self._active_threads[chat_id]
        thread.add_message(msg)

        # Если это ответ на сообщение — связываем
        if msg.reply_to_message_id:
            self._link_reply(thread, msg.reply_to_message_id, msg.user_id)

        # Ищем упоминания других пользователей в тексте
        mentioned = self._extract_mentions(msg)
        for user_id in mentioned:
            thread.add_mentioned_user(user_id)

        # Обновляем мапу имён
        if msg.username:
            if msg.username not in self._name_to_user:
                self._name_to_user[msg.username] = msg.user_id
            if msg.user_id not in self._user_aliases:
                self._user_aliases[msg.user_id] = set()
            self._user_aliases[msg.user_id].add(msg.username)

        if msg.first_name:
            if msg.user_id not in self._user_aliases:
                self._user_aliases[msg.user_id] = set()
            self._user_aliases[msg.user_id].add(msg.first_name)
            if msg.first_name not in self._name_to_user:
                self._name_to_user[msg.first_name] = msg.user_id

        logger.debug(
            "Context tracked",
            chat_id=chat_id,
            participants=len(thread.participants),
            mentioned=len(thread.mentioned_users),
        )

        return thread

    def get_context_for_message(self, msg: NormalizedMessage) -> dict:
        """
        Собрать полный контекст для сообщения.
        Возвращает структуру для LLM.
        """
        thread = self.track_message(msg)

        context = {
            "current_user": {
                "user_id": msg.user_id,
                "username": msg.username,
                "first_name": msg.first_name,
            },
            "participants": [],
            "mentioned": [],
            "recent_messages": [],
            "reply_context": None,
            "topic": thread.topic,
        }

        # Информация об участниках
        for uid in thread.participants:
            info = self._get_user_info(uid)
            context["participants"].append(info)

        # О ком говорят
        for uid in thread.mentioned_users:
            info = self._get_user_info(uid)
            context["mentioned"].append(info)

        # Последние сообщения
        for m in thread.messages[-10:]:
            context["recent_messages"].append(m)

        # Контекст reply
        if msg.reply_to_message_id:
            context["reply_context"] = self._find_replied_message(thread, msg.reply_to_message_id)

        return context

    def _link_reply(self, thread: ConversationThread, reply_to_id: int, replier_id: int) -> None:
        """Связать ответ с оригинальным сообщением."""
        original = self._find_message_by_telegram_id(thread, reply_to_id)
        if original:
            original_user = original.get("user_id")
            if original_user and original_user != replier_id:
                logger.debug(
                    "Reply linked",
                    replier=replier_id,
                    original_author=original_user,
                )

    def _find_message_by_telegram_id(self, thread: ConversationThread, telegram_id: int) -> dict | None:
        """Найти сообщение по telegram_id."""
        for m in thread.messages:
            if m.get("telegram_id") == telegram_id:
                return m
        return None

    def _find_replied_message(self, thread: ConversationThread, reply_to_id: int) -> dict | None:
        """Найти сообщение, на которое был дан ответ."""
        return self._find_message_by_telegram_id(thread, reply_to_id)

    def _extract_mentions(self, msg: NormalizedMessage) -> set[int]:
        """
        Извлечь упоминания пользователей из текста.
        Возвращает set user_id.
        """
        mentioned_ids: set[int] = set()
        text = msg.text

        # Ищем @username
        at_mentions = re.findall(r'@(\w+)', text)
        for username in at_mentions:
            if username in self._name_to_user:
                mentioned_ids.add(self._name_to_user[username])

        # Ищем имена с заглавной буквы
        name_matches = NAME_PATTERN.findall(text)
        for _, name in name_matches:
            if name and name in self._name_to_user:
                uid = self._name_to_user[name]
                if uid != msg.user_id:  # Не упоминать самого себя
                    mentioned_ids.add(uid)

        return mentioned_ids

    def _get_user_info(self, user_id: int) -> dict:
        """Получить информацию о пользователе."""
        aliases = self._user_aliases.get(user_id, set())
        username = None
        first_name = None

        # Пытаемся найти в известных алиасах
        for alias in aliases:
            if alias.startswith('@') or alias.islower():
                username = alias
            else:
                first_name = first_name or alias

        return {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "known_aliases": list(aliases),
        }

    def resolve_name(self, name: str) -> int | None:
        """Найти user_id по имени или username."""
        name_clean = name.strip().lstrip('@')
        return self._name_to_user.get(name_clean) or self._name_to_user.get(name)

    def get_participant_names(self, chat_id: int) -> dict[int, str]:
        """Вернуть имена всех участников чата."""
        thread = self._active_threads.get(chat_id)
        if not thread:
            return {}

        result = {}
        for uid in thread.participants:
            aliases = self._user_aliases.get(uid, set())
            display_name = next(iter(aliases)) if aliases else f"user_{uid}"
            result[uid] = display_name

        return result

    def get_active_thread(self, chat_id: int) -> ConversationThread | None:
        """Получить активный тред разговора."""
        thread = self._active_threads.get(chat_id)
        if thread and thread.is_active:
            return thread
        return None


# Singleton
context_tracker = ContextTracker()
