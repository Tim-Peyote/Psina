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
import asyncio
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

    Name/alias maps are scoped by chat_id to prevent cross-chat collisions
    (e.g. two different "Ivan"s in different chats).
    """

    def __init__(self) -> None:
        # chat_id -> ConversationThread
        self._active_threads: dict[int, ConversationThread] = {}
        # chat_id -> user_id -> set of known names/aliases
        self._user_aliases: dict[int, dict[int, set[str]]] = {}
        # chat_id -> name/username -> user_id
        self._name_to_user: dict[int, dict[str, int]] = {}
        # Lock protecting _name_to_user and _user_aliases writes
        self._lock = asyncio.Lock()

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
        mentioned = self._extract_mentions(msg, chat_id)
        for user_id in mentioned:
            thread.add_mentioned_user(user_id)

        # Обновляем мапу имён — ВСЕГДА обновляем (username мог измениться)
        self._register_user(chat_id, msg.user_id, msg.username, msg.first_name)

        logger.debug(
            "Context tracked",
            chat_id=chat_id,
            participants=len(thread.participants),
            mentioned=len(thread.mentioned_users),
        )

        return thread

    def _register_user(self, chat_id: int, user_id: int, username: str | None, first_name: str | None) -> None:
        """Register or update user name mappings (scoped to chat_id)."""
        if chat_id not in self._user_aliases:
            self._user_aliases[chat_id] = {}
        if chat_id not in self._name_to_user:
            self._name_to_user[chat_id] = {}

        chat_aliases = self._user_aliases[chat_id]
        chat_names = self._name_to_user[chat_id]

        if user_id not in chat_aliases:
            chat_aliases[user_id] = set()

        if username:
            # Always update — username can change
            chat_names[username] = user_id
            chat_names[username.lower()] = user_id
            chat_aliases[user_id].add(username)

        if first_name:
            chat_aliases[user_id].add(first_name)
            chat_names[first_name] = user_id
            chat_names[first_name.lower()] = user_id

    def get_context_for_message(self, msg: NormalizedMessage) -> dict:
        """
        Собрать полный контекст для сообщения.
        Возвращает структуру для LLM.
        """
        thread = self.track_message(msg)
        chat_id = msg.chat_id

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
            info = self._get_user_info(chat_id, uid)
            context["participants"].append(info)

        # О ком говорят
        for uid in thread.mentioned_users:
            info = self._get_user_info(chat_id, uid)
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

    def _extract_mentions(self, msg: NormalizedMessage, chat_id: int) -> set[int]:
        """
        Извлечь упоминания пользователей из текста.
        Возвращает set user_id (только для данного чата).
        """
        mentioned_ids: set[int] = set()
        text = msg.text
        chat_names = self._name_to_user.get(chat_id, {})

        # Ищем @username
        at_mentions = re.findall(r'@(\w+)', text)
        for username in at_mentions:
            uid = chat_names.get(username) or chat_names.get(username.lower())
            if uid:
                mentioned_ids.add(uid)

        # Ищем имена с заглавной буквы
        name_matches = NAME_PATTERN.findall(text)
        for _, name in name_matches:
            if name:
                uid = chat_names.get(name) or chat_names.get(name.lower())
                if uid and uid != msg.user_id:  # Не упоминать самого себя
                    mentioned_ids.add(uid)

        return mentioned_ids

    def _get_user_info(self, chat_id: int, user_id: int) -> dict:
        """Получить информацию о пользователе (в контексте чата)."""
        chat_aliases = self._user_aliases.get(chat_id, {})
        aliases = chat_aliases.get(user_id, set())
        username = None
        first_name = None

        for alias in aliases:
            # Usernames are latin alphanumeric, first_names usually start uppercase or Cyrillic
            if not username and alias.isascii() and alias[0:1].islower():
                username = alias
            elif not first_name:
                first_name = alias

        # If we still don't have username, try reverse lookup in _name_to_user
        if not username:
            chat_names = self._name_to_user.get(chat_id, {})
            for name, uid in chat_names.items():
                if uid == user_id and name.isascii() and name[0:1].islower():
                    username = name
                    break

        return {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "known_aliases": list(aliases),
        }

    def resolve_name(self, name: str, chat_id: int | None = None) -> int | None:
        """Найти user_id по имени или username.

        If chat_id is provided, searches only within that chat (preferred).
        Falls back to DB lookup if not found in memory.
        """
        name_clean = name.strip().lstrip("@").lower()
        name_original = name.strip().lstrip("@")

        # 1. In-memory lookup scoped to chat_id
        if chat_id is not None:
            chat_names = self._name_to_user.get(chat_id, {})
            uid = chat_names.get(name_clean) or chat_names.get(name_original)
            if uid:
                return uid

        # 2. Cross-chat fallback (for backward compat, picks first match)
        for cid, chat_names in self._name_to_user.items():
            uid = chat_names.get(name_clean) or chat_names.get(name_original)
            if uid:
                return uid

        # 3. DB fallback — query User table
        return self._resolve_from_db(name_clean, chat_id)

    def _resolve_from_db(self, name: str, chat_id: int | None = None) -> int | None:
        """Sync DB lookup for user by username or first_name."""
        try:
            from src.database.session import sync_session_factory
            with sync_session_factory() as session:
                # Try by username first (exact)
                stmt = select(User).where(User.username == name)
                result = session.execute(stmt)
                user = result.scalar_one_or_none()

                if not user:
                    # Try by first_name (case-insensitive)
                    from sqlalchemy import func
                    stmt = select(User).where(func.lower(User.first_name) == name.lower())
                    result = session.execute(stmt)
                    user = result.scalar_one_or_none()

                if user:
                    # Cache in the appropriate chat scope
                    scope = chat_id or 0
                    self._register_user(scope, user.id, user.username, user.first_name)
                    logger.debug("Resolved user from DB", name=name, user_id=user.id)
                    return user.id
        except Exception:
            logger.debug("DB resolve failed", name=name, exc_info=True)

        return None

    def get_participant_names(self, chat_id: int) -> dict[int, str]:
        """Вернуть имена всех участников чата."""
        thread = self._active_threads.get(chat_id)
        if not thread:
            return {}

        result = {}
        chat_aliases = self._user_aliases.get(chat_id, {})
        for uid in thread.participants:
            aliases = chat_aliases.get(uid, set())
            display_name = next(iter(aliases)) if aliases else f"user_{uid}"
            result[uid] = display_name

        return result

    def get_active_thread(self, chat_id: int) -> ConversationThread | None:
        """Получить активный тред разговора."""
        thread = self._active_threads.get(chat_id)
        if thread and thread.is_active:
            return thread
        return None

    async def load_chat_users_from_db(self, chat_id: int) -> int:
        """Load all known users for a chat from DB into in-memory cache.

        Called when the tracker has no data for a chat (e.g. after restart).
        Returns number of users loaded.
        """
        try:
            from src.database.models import UserProfile
            count = 0
            async for session in get_session():
                # Get all users who have profiles in this chat
                stmt = (
                    select(User)
                    .join(UserProfile, User.id == UserProfile.user_id)
                    .where(UserProfile.chat_id == chat_id)
                )
                result = await session.execute(stmt)
                users = list(result.scalars().all())

                for user in users:
                    self._register_user(chat_id, user.id, user.username, user.first_name)
                    count += 1

                # Also load from recent messages if profiles are sparse
                from src.database.models import Message
                stmt = (
                    select(User)
                    .join(Message, User.id == Message.user_id)
                    .where(Message.chat_id == chat_id)
                    .distinct()
                )
                result = await session.execute(stmt)
                msg_users = list(result.scalars().all())

                for user in msg_users:
                    chat_aliases = self._user_aliases.get(chat_id, {})
                    if user.id not in chat_aliases:
                        self._register_user(chat_id, user.id, user.username, user.first_name)
                        count += 1

            if count:
                logger.info("Loaded chat users from DB", chat_id=chat_id, count=count)
            return count
        except Exception:
            logger.debug("Failed to load chat users from DB", chat_id=chat_id, exc_info=True)
            return 0


# Singleton
context_tracker = ContextTracker()
