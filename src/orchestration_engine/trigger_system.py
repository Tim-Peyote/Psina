"""
Trigger System — confidence scoring для детекции обращения к боту.

Имя бота: Псина
Алиасы: пес, пёс, псинa

Confidence levels:
  HIGH (≥0.7)   → отвечать
  MEDIUM (0.3-0.7) → осторожно, обычно молчать
  LOW (<0.3)    → игнорировать
"""

import re
from dataclasses import dataclass
from enum import Enum

import structlog

from src.config import settings

logger = structlog.get_logger()


class ConfidenceLevel(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class TriggerResult:
    confidence: float
    level: ConfidenceLevel
    reason: str
    is_explicit_call: bool = False
    is_reply_to_bot: bool = False
    is_session_continuation: bool = False


# Имена бота + варианты с опечатками
BOT_NAMES = {"псина", "псина", "псінa", "psina"}
BOT_ALIASES = {"пес", "пёс", "песик", "пёсик", "псинуля", "псин"}
ALL_BOT_NAMES = BOT_NAMES | BOT_ALIASES

# Паттерны для разных типов сигналов

# Сильные сигналы
STRONG_NAME_START = re.compile(
    r'^(?:псина|пес|пёс|песик|пёсик|псин)\b[,\s!:.\?]',
    re.IGNORECASE
)
STRONG_QUESTION_TO_BOT = re.compile(
    r'(?:псина|пес|пёс|песик|пёсик|псин).*\?',
    re.IGNORECASE
)

# Средние сигналы
NAME_IN_MIDDLE = re.compile(
    r'(?:^|\s)(псина|пес|пёс|песик|пёсик|псин)\b',
    re.IGNORECASE
)

# Слабые сигналы — слово "пёс/пес" может быть не про бота
GENERIC_DOG_WORDS = re.compile(
    r'(?:п[её]с|собак|пс[иы]|хвост|лап)',
    re.IGNORECASE
)

# Явные обращения
EXPLICIT_CALL_PATTERNS = [
    r'^(псина|пес|пёс)[,\s]+(.+)',          # "Псина, ..."
    r'^([А-ЯA-Zа-яa-z]+),?\s+(псина|пес|пёс)\b',  # "Вася, псина ..."
    r'@(псина|\w+)',                          # @mention
]

# Команды заткнуться
SILENCE_PATTERNS = [
    r'(?:псина|пес|пёс)[,\s]+(заткнись|замолчи|помолчи|тише|хватит|стоп|не\s+лезь|отвали|уйди)',
    r'(заткнись|замолчи|помолчи|тише|хватит|стоп|не\s+лезь|отвали)[,\s]+(псина|пес|пёс)',
    r'не\s+лезь\s+пока',
    r'не\s+сейчас',
    r'помолчи\s+немного',
]

# Команды стать активнее
MORE_ACTIVE_PATTERNS = [
    r'(?:псина|пес|пёс)[,\s]+(будь\s+поактивнее|включайся\s+чаще|активнее|оживи|разговорись)',
    r'(будь\s+поактивнее|включайся\s+чаще|активнее|оживи|разговорись)',
]

# Команды стать пассивнее
LESS_ACTIVE_PATTERNS = [
    r'(?:псина|пес|пёс)[,\s]+(сбавь|поубавь|тише|меньше\s+пиши|реже\s+отвечай)',
    r'(сбавь|поубавь|тише|меньше\s+пиши|реже\s+отвечай)',
]

# Command to mention-only
MENTION_ONLY_PATTERNS = [
    r'(?:псина|пес|пёс)[,\s]+(отвечай\s+только\s+если\s+позвали|только\s+по\s+имени|mention\s*[-\s]?only)',
    r'отвечай\s+только\s+если\s+позвали',
    r'только\s+когда\s+зовут',
]


class TriggerSystem:
    """
    Система детекции обращения к боту.
    """

    def __init__(self) -> None:
        self.bot_user_id: int | None = None
        self.high_threshold: float = settings.trigger_high_threshold
        self.medium_threshold: float = settings.trigger_medium_threshold

    def evaluate(
        self,
        text: str,
        is_reply: bool = False,
        reply_to_bot: bool = False,
        in_active_session: bool = False,
    ) -> TriggerResult:
        """
        Оценить обращение к боту.
        Возвращает TriggerResult с confidence и reason.
        """
        text_stripped = text.strip()

        # 0. Reply на сообщение бота — максимальный сигнал
        if reply_to_bot:
            return TriggerResult(
                confidence=0.95,
                level=ConfidenceLevel.HIGH,
                reason="reply_to_bot",
                is_explicit_call=True,
                is_reply_to_bot=True,
            )

        # 1. Continuation активной сессии
        if in_active_session:
            # Проверяем — это всё ещё обращение к боту или уже новый разговор
            session_score = self._score_session_continuation(text_stripped)
            if session_score >= 0.5:
                return TriggerResult(
                    confidence=session_score,
                    level=ConfidenceLevel.HIGH if session_score >= 0.7 else ConfidenceLevel.MEDIUM,
                    reason="session_continuation",
                    is_session_continuation=True,
                )

        # 2. Проверяем silence команды — это не обращение, а команда
        if self._matches_patterns(text_stripped, SILENCE_PATTERNS):
            return TriggerResult(
                confidence=0.9,
                level=ConfidenceLevel.HIGH,
                reason="silence_command",
                is_explicit_call=True,
            )

        # 3. Score обращения
        score = 0.0
        reasons: list[str] = []
        is_explicit = False

        # 3a. Имя в начале — сильнейший сигнал
        if STRONG_NAME_START.match(text_stripped):
            score += 0.5
            reasons.append("name_at_start")
            is_explicit = True

            # Плюс если это вопрос
            if text_stripped.endswith("?"):
                score += 0.15
                reasons.append("question_to_bot")

        # 3b. @mention в тексте
        if "@" in text_stripped:
            mention_score = self._score_mention(text_stripped)
            score += mention_score
            if mention_score > 0:
                reasons.append("mention_detected")
                is_explicit = True

        # 3c. Вопрос к боту (без имени в начале но с именем где-то)
        if STRONG_QUESTION_TO_BOT.search(text_stripped) and text_stripped.endswith("?"):
            score += 0.35
            reasons.append("explicit_question")
            is_explicit = True

        # 3d. Имя в середине — средний сигнал
        name_mid = NAME_IN_MIDDLE.search(text_stripped)
        if name_mid and not STRONG_NAME_START.match(text_stripped):
            matched_name = name_mid.group(1).lower()
            if matched_name in BOT_NAMES:
                score += 0.25
                reasons.append("name_in_middle")
            elif matched_name in BOT_ALIASES:
                score += 0.15
                reasons.append("alias_in_middle")

        # 3e. Если есть имя бота НО в контексте общего разговора
        # Пример: "какой хороший пёс у соседа" — это НЕ про бота
        has_bot_name = any(name in text_stripped.lower() for name in ALL_BOT_NAMES)
        if has_bot_name:
            # Проверяем — это про бота или про собаку вообще
            if self._is_about_bot(text_stripped):
                score += 0.1
                reasons.append("context_about_bot")
            else:
                # Слово "пёс" но не про бота
                score -= 0.15
                reasons.append("dog_word_not_about_bot")

        # 3f. Общие вопросы в чат — слабый сигнал
        if self._is_general_question(text_stripped):
            score -= 0.1
            reasons.append("general_question")

        # Clamp
        score = max(0.0, min(1.0, score))

        # Определяем уровень
        if score >= self.high_threshold:
            level = ConfidenceLevel.HIGH
        elif score >= self.medium_threshold:
            level = ConfidenceLevel.MEDIUM
        else:
            level = ConfidenceLevel.LOW

        reason_str = "; ".join(reasons) if reasons else "no_signals"

        logger.debug(
            "Trigger evaluated",
            text=text_stripped[:80],
            confidence=score,
            level=level.value,
            reason=reason_str,
        )

        return TriggerResult(
            confidence=score,
            level=level,
            reason=reason_str,
            is_explicit_call=is_explicit,
        )

    def classify_intent(self, text: str) -> str:
        """
        Классифицировать интенцию сообщения.
        Возвращает: silence, more_active, less_active, mention_only, normal
        """
        text_lower = text.lower()

        if self._matches_patterns(text_lower, SILENCE_PATTERNS):
            return "silence"

        if self._matches_patterns(text_lower, MENTION_ONLY_PATTERNS):
            return "mention_only"

        if self._matches_patterns(text_lower, MORE_ACTIVE_PATTERNS):
            return "more_active"

        if self._matches_patterns(text_lower, LESS_ACTIVE_PATTERNS):
            return "less_active"

        return "normal"

    def is_behavior_control(self, text: str) -> bool:
        """Это управление поведением бота?"""
        intent = self.classify_intent(text)
        return intent != "normal"

    def get_behavior_action(self, text: str) -> str | None:
        """Какое действие нужно выполнить."""
        return self.classify_intent(text)

    # ========== Внутренние методы ==========

    def _score_mention(self, text: str) -> float:
        """Оценить @mention."""
        mentions = re.findall(r'@(\w+)', text)
        for m in mentions:
            if m.lower() in ALL_BOT_NAMES or m.lower() in {"psina_bot", "psina"}:
                return 0.5
        return 0.0

    def _is_about_bot(self, text: str) -> bool:
        """
        Проверить — текст про бота или про собак/псов вообще.
        """
        text_lower = text.lower()

        # Если есть контекстные маркеры обращения — это про бота
        if any(p in text_lower for p in [", псина", ", пес", ", пёс", "псина,", "пес,", "пёс,"]):
            return True

        # Если это команда/вопрос к боту
        if text_lower.startswith(("псина", "пес", "пёс")):
            return True

        # Если просто "пёс/пес" без контекста обращения — скорее всего не про бота
        if GENERIC_DOG_WORDS.search(text_lower):
            # Проверяем — есть ли рядом слова обращения
            if any(w in text_lower for w in ["скажи", "расскажи", "помоги", "знаешь", "что думаешь"]):
                return True
            return False

        return False

    def _is_general_question(self, text: str) -> bool:
        """Это общий вопрос в чат (не к боту)?"""
        text_lower = text.lower()
        # Общие вопросы без адреса
        if text_lower.startswith(("кто-нибудь", "кто нибудь", "кто-то", "есть кто")):
            return True
        if any(w in text_lower for w in ["кто-нибудь", "кто нибудь", "народ", "ребят"]):
            return True
        return False

    def _score_session_continuation(self, text: str) -> float:
        """
        Оценить — это продолжение диалога с ботом?
        """
        text_lower = text.lower()

        # Короткие ответы — высокая вероятность продолжения
        short_answers = [
            "да", "нет", "ага", "неа", "конечно", "точно", "не знаю",
            "может", "спасибо", "ок", "окей", "понял", "ясно",
            "ну такое", "согласен", "не согласен",
        ]
        if text_lower.strip() in short_answers:
            return 0.8

        # Ответы с местоимением "ты" — обращаются к боту
        if re.search(r'\b(ты|тебе|тобой|твой|твоя|твоё)\b', text_lower):
            return 0.7

        # Логическая связка с предыдущим ответом
        if text_lower.startswith(("а ", "но ", "и ", "так ", "ладно ", "ну ")):
            return 0.5

        # Длинное сообщение без адреса — скорее всего не продолжение
        if len(text) > 100:
            return 0.2

        return 0.3

    def _matches_patterns(self, text: str, patterns: list[str]) -> bool:
        """Проверить совпадение текста с паттернами."""
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False


trigger_system = TriggerSystem()
