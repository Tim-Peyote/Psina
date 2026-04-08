"""
Session Manager — микросессии параллельных диалогов.

Бот может вести несколько разговоров одновременно в одном чате.
Каждая сессия привязана к конкретному пользователю и контексту.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

import structlog

from src.config import settings

logger = structlog.get_logger()


class SessionState(Enum):
    ACTIVE = "active"
    CLOSING = "closing"  # таймаут скоро, последний шанс
    CLOSED = "closed"


@dataclass
class ConversationalSession:
    """Одна микросессия диалога."""

    chat_id: int
    user_id: int
    session_id: str
    state: SessionState = SessionState.ACTIVE
    participants: set[int] = field(default_factory=set)
    messages: list[dict] = field(default_factory=list)
    topic: str = ""
    thread_anchor: int | None = None  # reply_to_message_id
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = 0
    max_messages: int = 30  # увеличено с 8 для более длинных диалогов
    timeout_seconds: int = 600  # 10 минут, увеличено с 3 для живого общения

    @property
    def is_alive(self) -> bool:
        if self.state == SessionState.CLOSED:
            return False
        if self.message_count >= self.max_messages:
            return False
        elapsed = (datetime.now(timezone.utc) - self.last_activity).total_seconds()
        if elapsed > self.timeout_seconds:
            return False
        return True

    @property
    def is_ending_soon(self) -> bool:
        """Сессия скоро закроется — последнее сообщение бота."""
        if not self.is_alive:
            return False
        elapsed = (datetime.now(timezone.utc) - self.last_activity).total_seconds()
        return elapsed > (self.timeout_seconds * 0.7)

    def add_message(self, user_id: int, text: str) -> None:
        self.participants.add(user_id)
        self.messages.append({
            "user_id": user_id,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self.last_activity = datetime.now(timezone.utc)
        self.message_count += 1

    def get_context_summary(self) -> str:
        """Краткое содержание сессии для контекста LLM."""
        if not self.messages:
            return ""
        lines = [f"  [{m['user_id']}]: {m['text'][:100]}" for m in self.messages[-5:]]
        return "Контекст диалога:\n" + "\n".join(lines)


class SessionManager:
    """
    Управляет микросессиями диалогов.

    В одном чате может быть несколько активных сессий.
    Бот не смешивает контекст между ними.
    """

    def __init__(self) -> None:
        # key: "{chat_id}:{user_id}" -> ConversationalSession
        self._sessions: dict[str, ConversationalSession] = {}
        # key: chat_id -> list of active session keys
        self._chat_sessions: dict[int, list[str]] = {}
        self._session_counter = 0
        self.max_sessions_per_chat = settings.max_sessions_per_chat
        self.session_timeout = settings.session_timeout_seconds
        self.session_max_messages = settings.session_max_messages

    def create_session(
        self,
        chat_id: int,
        user_id: int,
        thread_anchor: int | None = None,
    ) -> ConversationalSession:
        """Создать новую сессию диалога."""
        self._session_counter += 1
        session = ConversationalSession(
            chat_id=chat_id,
            user_id=user_id,
            session_id=f"sess_{self._session_counter}",
            thread_anchor=thread_anchor,
            participants={user_id},
            timeout_seconds=self.session_timeout,
            max_messages=self.session_max_messages,
        )

        key = self._make_key(chat_id, user_id)
        self._cleanup_dead_sessions(chat_id)

        # Проверяем лимит
        chat_keys = self._chat_sessions.get(chat_id, [])
        alive_count = sum(1 for k in chat_keys if self._sessions.get(k, ConversationalSession(0, 0, "")).is_alive)
        if alive_count >= self.max_sessions_per_chat:
            # Закрываем oldest
            self._close_oldest_session(chat_id)

        self._sessions[key] = session
        if chat_id not in self._chat_sessions:
            self._chat_sessions[chat_id] = []
        self._chat_sessions[chat_id].append(key)

        logger.info("Session created", session_id=session.session_id, chat_id=chat_id, user_id=user_id)
        return session

    def get_session(self, chat_id: int, user_id: int) -> ConversationalSession | None:
        """Получить сессию для пользователя в чате."""
        key = self._make_key(chat_id, user_id)
        session = self._sessions.get(key)
        if session and session.is_alive:
            return session
        return None

    def get_active_sessions(self, chat_id: int) -> list[ConversationalSession]:
        """Получить все активные сессии в чате."""
        keys = self._chat_sessions.get(chat_id, [])
        result = []
        for key in keys:
            session = self._sessions.get(key)
            if session and session.is_alive:
                result.append(session)
        return result

    def is_user_in_session(self, chat_id: int, user_id: int) -> bool:
        """Проверить — пользователь в активной сессии с ботом?"""
        return self.get_session(chat_id, user_id) is not None

    def is_session_about_topic(self, chat_id: int, user_id: int, text: str) -> bool:
        """Проверить — сообщение относится к теме сессии?"""
        session = self.get_session(chat_id, user_id)
        if not session:
            return False

        # Короткие ответы — всегда по теме
        if len(text) < 50:
            return True

        # Проверяем пересечение слов с контекстом
        text_lower = text.lower()
        for msg in session.messages[-3:]:
            msg_lower = msg["text"].lower()
            words = set(msg_lower.split())
            text_words = set(text_lower.split())
            overlap = len(words & text_words)
            if overlap >= 2:
                return True

        return False

    def close_session(self, chat_id: int, user_id: int) -> None:
        """Закрыть сессию."""
        key = self._make_key(chat_id, user_id)
        session = self._sessions.get(key)
        if session:
            session.state = SessionState.CLOSED
            logger.info("Session closed", session_id=session.session_id)

    def update_activity(self, chat_id: int, user_id: int, text: str) -> None:
        """Обновить активность в сессии."""
        session = self.get_session(chat_id, user_id)
        if session:
            session.add_message(user_id, text)

    def get_session_context_for_llm(self, chat_id: int, user_id: int) -> str:
        """Получить контекст сессии для LLM."""
        session = self.get_session(chat_id, user_id)
        if not session:
            return ""
        return session.get_context_summary()

    # ========== Внутренние методы ==========

    def _make_key(self, chat_id: int, user_id: int) -> str:
        return f"{chat_id}:{user_id}"

    def _cleanup_dead_sessions(self, chat_id: int) -> None:
        """Удалить мёртвые сессии."""
        keys = self._chat_sessions.get(chat_id, [])
        alive = []
        for key in keys:
            session = self._sessions.get(key)
            if session and session.is_alive:
                alive.append(key)
            elif session:
                session.state = SessionState.CLOSED
        self._chat_sessions[chat_id] = alive

    def _close_oldest_session(self, chat_id: int) -> None:
        """Закрыть самую старую сессию."""
        keys = self._chat_sessions.get(chat_id, [])
        oldest_key = None
        oldest_time = datetime.min.replace(tzinfo=timezone.utc)

        for key in keys:
            session = self._sessions.get(key)
            if session and session.is_alive and session.last_activity < oldest_time:
                oldest_key = key
                oldest_time = session.last_activity

        if oldest_key:
            session = self._sessions[oldest_key]
            session.state = SessionState.CLOSED
            logger.info("Oldest session closed", session_id=session.session_id)


session_manager = SessionManager()
