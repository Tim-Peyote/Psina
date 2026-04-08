"""
Trigger System — confidence scoring для детекции обращения к боту.

Бот динамически запоминает новые прозвища которые ему дают.

Confidence levels:
  HIGH (≥0.7)   → отвечать
  MEDIUM (0.3-0.7) → отвечать если в сессии
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


# Базовые имена (до того как бот выучит новые)
BOT_NAMES = {"псина", "псінa", "psina"}
BOT_ALIASES = {"пес", "пёс", "песик", "пёсик", "псинуля", "псин"}
ALL_BOT_NAMES = BOT_NAMES | BOT_ALIASES


def _build_name_pattern(names: set[str]) -> str:
    """Build regex pattern for a set of names."""
    escaped = [re.escape(n) for n in names]
    return "(?:" + "|".join(escaped) + ")"


class TriggerSystem:
    """
    Система детекции обращения к боту.
    Динамически запоминает новые прозвища.
    """

    def __init__(self) -> None:
        self.bot_user_id: int | None = None
        self.high_threshold: float = settings.trigger_high_threshold
        self.medium_threshold: float = settings.trigger_medium_threshold

        # Per-chat learned nicknames: {chat_id: set("бобик", "шарик", ...)}
        self._learned_nicknames: dict[int, set[str]] = {}

    def learn_nickname(self, chat_id: int, name: str) -> None:
        """Запомнить что в этом чате бота называют этим именем."""
        name_lower = name.lower().strip().rstrip(".,!?")
        if not name_lower or len(name_lower) < 2:
            return
        if chat_id not in self._learned_nicknames:
            self._learned_nicknames[chat_id] = set()
        self._learned_nicknames[chat_id].add(name_lower)

    def get_all_names_for_chat(self, chat_id: int) -> set[str]:
        """Все известные имена бота для конкретного чата."""
        base = ALL_BOT_NAMES.copy()
        if chat_id in self._learned_nicknames:
            base |= self._learned_nicknames[chat_id]
        return base

    def evaluate(
        self,
        text: str,
        is_reply: bool = False,
        reply_to_bot: bool = False,
        in_active_session: bool = False,
        chat_id: int = 0,
    ) -> TriggerResult:
        """
        Оценить обращение к боту.
        Возвращает TriggerResult с confidence и reason.
        """
        text_stripped = text.strip()
        text_lower = text_stripped.lower()

        # Get all known names for this chat
        all_names = self.get_all_names_for_chat(chat_id)
        names_pattern = _build_name_pattern(all_names)

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
            session_score = self._score_session_continuation(text_stripped)
            if session_score >= 0.5:
                return TriggerResult(
                    confidence=session_score,
                    level=ConfidenceLevel.HIGH if session_score >= 0.7 else ConfidenceLevel.MEDIUM,
                    reason="session_continuation",
                    is_session_continuation=True,
                )

        # 2. Score обращения
        score = 0.0
        reasons: list[str] = []
        is_explicit = False

        # 2a. Имя в начале — сильнейший сигнал
        name_start_pattern = re.compile(
            rf'^{names_pattern}\b[,\s!:.\?]|^{names_pattern}$',
            re.IGNORECASE
        )
        if name_start_pattern.match(text_stripped):
            score += 0.75
            reasons.append("name_at_start")
            is_explicit = True
            if text_stripped.endswith("?"):
                score += 0.15
                reasons.append("question_to_bot")

        # 2b. @mention
        if "@" in text_stripped:
            mention_score = self._score_mention(text_stripped)
            score += mention_score
            if mention_score > 0:
                reasons.append("mention_detected")
                is_explicit = True

        # 2c. Имя в середине
        name_mid_pattern = re.compile(
            rf'(?:^|\s)({names_pattern})\b',
            re.IGNORECASE
        )
        name_mid = name_mid_pattern.search(text_stripped)
        if name_mid and not name_start_pattern.match(text_stripped):
            matched_name = name_mid.group(1).lower()
            if matched_name in BOT_NAMES:
                score += 0.25
                reasons.append("name_in_middle")
            elif matched_name in BOT_ALIASES:
                score += 0.15
                reasons.append("alias_in_middle")
            else:
                # Learned nickname in middle
                score += 0.20
                reasons.append(f"learned_nick_in_middle:{matched_name}")

        # 2d. Если есть имя бота НО в контексте общего разговора
        has_bot_name = any(name in text_lower for name in ALL_BOT_NAMES)
        if has_bot_name:
            if self._is_about_bot(text_lower):
                score += 0.1
                reasons.append("context_about_bot")
            else:
                score -= 0.15
                reasons.append("dog_word_not_about_bot")

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

        silence_patterns = [
            r'(заткнись|замолчи|помолчи|тише|хватит|стоп|не\s+лезь|отвали|уйди)',
        ]
        for p in silence_patterns:
            if re.search(p, text_lower):
                return "silence"

        if re.search(r'отвечай\s+только\s+если\s+позвали', text_lower):
            return "mention_only"

        if re.search(r'(будь\s+поактивнее|включайся\s+чаще|активнее|оживи|разговорись)', text_lower):
            return "more_active"

        if re.search(r'(сбавь|поубавь|тише|меньше\s+пиши|реже\s+отвечай)', text_lower):
            return "less_active"

        return "normal"

    def is_behavior_control(self, text: str) -> bool:
        """Это управление поведением бота?"""
        return self.classify_intent(text) != "normal"

    def get_behavior_action(self, text: str) -> str | None:
        """Какое действие нужно выполнить."""
        return self.classify_intent(text)

    # ========== Внутренние методы ==========

    def _score_mention(self, text: str) -> float:
        """Оценить @mention."""
        mentions = re.findall(r'@(\w+)', text)
        all_names = self.get_all_names_for_chat(0)  # Check all known names
        for m in mentions:
            if m.lower() in all_names or m.lower() in {"psina_bot", "psina"}:
                return 0.5
        return 0.0

    def _is_about_bot(self, text_lower: str) -> bool:
        """Текст про бота или про собак/псов вообще."""
        if any(p in text_lower for p in [", псина", ", пес", ", пёс", "псина,", "пес,", "пёс,"]):
            return True
        if text_lower.startswith(("псина", "пес", "пёс")):
            return True
        if any(w in text_lower for w in ["скажи", "расскажи", "помоги", "знаешь", "что думаешь"]):
            return True
        return False

    def _is_general_question(self, text: str) -> bool:
        """Общий вопрос в чат (не к боту)?"""
        text_lower = text.lower()
        if text_lower.startswith(("кто-нибудь", "кто нибудь", "кто-то", "есть кто")):
            return True
        if any(w in text_lower for w in ["кто-нибудь", "кто нибудь", "народ", "ребят"]):
            return True
        return False

    def _score_session_continuation(self, text: str) -> float:
        """Оценить — это продолжение диалога с ботом?"""
        text_lower = text.lower().strip()

        short_answers = [
            "да", "нет", "ага", "неа", "конечно", "точно", "не знаю",
            "может", "спасибо", "ок", "окей", "понял", "ясно",
            "ну такое", "согласен", "не согласен",
        ]
        if text_lower in short_answers:
            return 0.85

        if re.search(r'\b(ты|тебе|тобой|твой|твоя|твоё)\b', text_lower):
            return 0.8

        session_signals = [
            r'(почему|хули|чё|что|где)\s*(ты\s+)?(молчи|молчишь|затих|замолк)',
            r'(ау|алло|хелло|эй|hey)\s*$',
            r'(ответь|отвечай|реакци|эмоци|живой|ты\s+там)',
            r'(лохмат|четвероног|твар|мраз|сук|псина|пёс|пес|собак)',
            r'(хули?|чё|что)\s+(ты\s+)?(не\s+)?(отвечаешь|реагируешь|пишешь)',
            r'(скажи|напомни|покажи|дай|расскажи|объясни)',
            r'(как\s+ты|что\s+думаешь|ты\s+видишь|ты\s+понял|ты\s+слыш)',
            r'(туп|глуп|бесполез|ужасн|отстой)',
        ]
        for pattern in session_signals:
            if re.search(pattern, text_lower):
                return 0.75

        if text_lower.startswith(("а ", "но ", "и ", "так ", "ладно ", "ну ")):
            return 0.6

        if len(text) < 50:
            return 0.55

        if len(text) > 100:
            return 0.35

        return 0.5

    def _matches_patterns(self, text: str, patterns: list[str]) -> bool:
        """Проверить совпадение текста с паттернами."""
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False


trigger_system = TriggerSystem()
