"""
Message Router — классификация каждого входящего сообщения.

Типы маршрутизации:
1. background     — только запомнить
2. direct_call    — ответить (явное обращение)
3. session_continuation — продолжить диалог
4. ignore         — чужой разговор
5. behavior_control — смена режима бота
6. game_interaction — взаимодействие с игрой
"""

from dataclasses import dataclass
from enum import Enum

import structlog

from src.message_processor.processor import NormalizedMessage
from src.orchestration_engine.trigger_system import trigger_system, TriggerResult, ConfidenceLevel
from src.orchestration_engine.session_manager import session_manager
from src.orchestration_engine.anti_chaos import anti_chaos

logger = structlog.get_logger()


class MessageRoute(Enum):
    BACKGROUND = "background"
    DIRECT_CALL = "direct_call"
    SESSION_CONTINUATION = "session_continuation"
    IGNORE = "ignore"
    BEHAVIOR_CONTROL = "behavior_control"
    GAME_INTERACTION = "game_interaction"


@dataclass
class RoutingDecision:
    route: MessageRoute
    confidence: float
    trigger: TriggerResult
    reason: str
    should_respond: bool = False
    behavior_action: str | None = None


class MessageRouter:
    """
    Маршрутизатор сообщений.
    Классифицирует каждое входящее сообщение.
    """

    def __init__(self) -> None:
        # Храним telegram_id сообщений бота для детекции reply
        self._bot_message_ids: set[int] = set()
        # Ограничиваем размер — храним последние 5000
        self._max_tracked = 5000

    def register_bot_message(self, telegram_id: int) -> None:
        """Записать ID сообщения, отправленного ботом."""
        self._bot_message_ids.add(telegram_id)
        if len(self._bot_message_ids) > self._max_tracked:
            # Удаляем oldest — просто берём половину
            ids_list = list(self._bot_message_ids)
            self._bot_message_ids = set(ids_list[-self._max_tracked // 2:])

    def route(self, msg: NormalizedMessage) -> RoutingDecision:
        """
        Принять решение по маршрутизации сообщения.
        """
        # 1. Проверяем — это behavior control?
        if trigger_system.is_behavior_control(msg.text):
            action = trigger_system.get_behavior_action(msg.text)
            return RoutingDecision(
                route=MessageRoute.BEHAVIOR_CONTROL,
                confidence=0.9,
                trigger=TriggerResult(
                    confidence=0.9,
                    level=ConfidenceLevel.HIGH,
                    reason="behavior_control",
                    is_explicit_call=True,
                ),
                reason=f"behavior_action: {action}",
                should_respond=True,
                behavior_action=action,
            )

        # 2. Проверяем — это game interaction?
        if msg.is_command and msg.command == "game":
            return RoutingDecision(
                route=MessageRoute.GAME_INTERACTION,
                confidence=1.0,
                trigger=TriggerResult(
                    confidence=1.0,
                    level=ConfidenceLevel.HIGH,
                    reason="game_command",
                    is_explicit_call=True,
                ),
                reason="game_command",
                should_respond=True,
            )

        # 3. Eval trigger для обычного сообщения
        in_session = session_manager.is_user_in_session(msg.chat_id, msg.user_id)
        is_reply_to_bot = self._is_reply_to_bot_message(msg)

        trigger = trigger_system.evaluate(
            text=msg.text,
            is_reply=msg.reply_to_message_id is not None,
            reply_to_bot=is_reply_to_bot,
            in_active_session=in_session,
            chat_id=msg.chat_id,
        )

        # 4. Session continuation check
        if trigger.is_session_continuation:
            return RoutingDecision(
                route=MessageRoute.SESSION_CONTINUATION,
                confidence=trigger.confidence,
                trigger=trigger,
                reason="session_continuation",
                should_respond=trigger.confidence >= 0.5,
            )

        # 5. Direct call — явное обращение
        if trigger.level == ConfidenceLevel.HIGH and trigger.is_explicit_call:
            # Проверяем anti-chaos
            can_respond, reason = anti_chaos.can_respond(
                msg.chat_id,
                is_urgent=trigger.is_reply_to_bot,
            )
            return RoutingDecision(
                route=MessageRoute.DIRECT_CALL,
                confidence=trigger.confidence,
                trigger=trigger,
                reason=f"direct_call; anti_chaos: {reason}",
                should_respond=can_respond,
            )

        # 6. Background — просто запоминаем
        if trigger.level == ConfidenceLevel.LOW:
            return RoutingDecision(
                route=MessageRoute.BACKGROUND,
                confidence=trigger.confidence,
                trigger=trigger,
                reason="low_confidence — background only",
                should_respond=False,
            )

        # 7. Medium — осторожно, обычно молчим
        if trigger.level == ConfidenceLevel.MEDIUM:
            # Если в сессии — можно ответить
            if in_session:
                can_respond, reason = anti_chaos.can_respond(msg.chat_id, is_urgent=False)
                return RoutingDecision(
                    route=MessageRoute.SESSION_CONTINUATION,
                    confidence=trigger.confidence,
                    trigger=trigger,
                    reason=f"medium_confidence + session; anti_chaos: {reason}",
                    should_respond=can_respond,  # Убрали порог 0.5 — в сессии отвечаем всегда
                )
            # Не в сессии — молчим
            return RoutingDecision(
                route=MessageRoute.IGNORE,
                confidence=trigger.confidence,
                trigger=trigger,
                reason="medium_confidence — ignoring (not in session)",
                should_respond=False,
            )

        # По умолчанию — игнор
        return RoutingDecision(
            route=MessageRoute.IGNORE,
            confidence=trigger.confidence,
            trigger=trigger,
            reason="no_match — ignoring",
            should_respond=False,
        )

    def _is_reply_to_bot_message(self, msg: NormalizedMessage) -> bool:
        """
        Проверить — reply на сообщение бота?
        """
        if msg.reply_to_message_id is None:
            return False
        return msg.reply_to_message_id in self._bot_message_ids


message_router = MessageRouter()
