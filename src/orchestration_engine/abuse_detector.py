"""
Abuse Detector — детекция издевательств и агрессии к боту.

Псина имеет характер и не терпит издевательств.
Отслеживает паттерны агрессии и реагирует.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog

from src.message_processor.processor import NormalizedMessage

logger = structlog.get_logger()


@dataclass
class AbuseRecord:
    timestamp: datetime
    severity: float  # 0.0 — 1.0
    text: str
    response: str  # как бот отреагировал


@dataclass
class UserAbuseProfile:
    """Профиль агрессии пользователя по отношению к боту."""

    user_id: int
    abuse_records: list[AbuseRecord] = field(default_factory=list)
    auto_silenced_until: datetime | None = None

    @property
    def recent_abuse_count(self) -> int:
        """Количество злоупотреблений за последний час."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        return sum(1 for r in self.abuse_records if r.timestamp > cutoff)

    @property
    def total_abuse_score(self) -> float:
        """Общий скор: чем больше, тем хуже."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [r for r in self.abuse_records if r.timestamp > cutoff]
        # Взвешиваем: новые важнее
        now = datetime.now(timezone.utc)
        score = 0.0
        for r in recent:
            age_hours = (now - r.timestamp).total_seconds() / 3600
            decay = 0.9 ** age_hours  # затухание
            score += r.severity * decay
        return score


class AbuseDetector:
    """
    Детекция и реагирование на агрессию к боту.
    """

    def __init__(self) -> None:
        # user_id -> UserAbuseProfile
        self._profiles: dict[int, UserAbuseProfile] = {}

        # Паттерны прямой агрессии к боту
        self._direct_abuse = [
            "заткни", "заткнись", "закрой", "умолкни",
            "тупой пёс", "тупая собака", "тупой бот",
            "бесишь", "достал", "отъеб", "отвали нахуй",
            "иди нахуй", "пошёл нахуй", "нахуй иди",
            "какой тупой", "дебил", "идиот", "придурок",
            "соси", "жри", "молчи блять", "хуй тебе",
        ]

        # Паттерны косвенной агрессии
        self._indirect_abuse = [
            "какой бесполезный", "никакой пользы", "лучше бы молчал",
            "зачем тебя добавили", "удалите его", "нафиг не нужен",
            "кто этого добавил", "зачем этот бот",
        ]

        # Пороги реакции
        self.mild_threshold = 2   # после 2 — предупреждение
        self.medium_threshold = 4  # после 4 — строгое предупреждение
        self.severe_threshold = 6  # после 6 — автозаглушка

    def analyze(self, msg: NormalizedMessage) -> dict:
        """
        Проанализировать сообщение на агрессию к боту.
        Возвращает:
        {
            "is_abuse": bool,
            "severity": float,
            "type": "direct" | "indirect" | "none",
            "action": "ignore" | "warning" | "strict_warning" | "auto_silence",
            "response_text": str | None,
        }
        """
        text_lower = msg.text.lower()
        user_id = msg.user_id

        # 1. Определяем тип и степень агрессии
        severity = 0.0
        abuse_type = "none"

        # Прямая агрессия
        for pattern in self._direct_abuse:
            if pattern in text_lower:
                severity = max(severity, 0.8)
                abuse_type = "direct"
                break

        # Косвенная агрессия
        for pattern in self._indirect_abuse:
            if pattern in text_lower:
                severity = max(severity, 0.5)
                abuse_type = "indirect"
                break

        # Если нет агрессии — выходим
        if abuse_type == "none":
            return {
                "is_abuse": False,
                "severity": 0.0,
                "type": "none",
                "action": "ignore",
                "response_text": None,
            }

        # 2. Записываем в профиль
        profile = self._get_profile(user_id)
        profile.abuse_records.append(AbuseRecord(
            timestamp=datetime.now(timezone.utc),
            severity=severity,
            text=msg.text,
            response="",
        ))

        # Очищаем старые записи (> 24 часов)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        profile.abuse_records = [
            r for r in profile.abuse_records if r.timestamp > cutoff
        ]

        # 3. Определяем реакцию
        count = profile.recent_abuse_count
        score = profile.total_abuse_score

        action = "ignore"
        response_text = None

        if score >= self.severe_threshold:
            action = "auto_silence"
            response_text = self._get_auto_silence_response(count)
            profile.auto_silenced_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            logger.warning(
                "Auto-silenced user for abuse",
                user_id=user_id,
                score=score,
                count=count,
            )
        elif score >= self.medium_threshold:
            action = "strict_warning"
            response_text = self._get_strict_warning_response(count)
        elif score >= self.mild_threshold:
            action = "warning"
            response_text = self._get_warning_response(count)
        else:
            # Первый раз — спокойная реакция
            action = "ignore"
            response_text = self._get_first_response()

        return {
            "is_abuse": True,
            "severity": severity,
            "type": abuse_type,
            "action": action,
            "response_text": response_text,
        }

    def is_user_silenced(self, user_id: int) -> bool:
        """Проверить — заглушен ли пользователь за агрессию."""
        profile = self._profiles.get(user_id)
        if not profile or not profile.auto_silenced_until:
            return False
        return datetime.now(timezone.utc) < profile.auto_silenced_until

    def reset_user_abuse(self, user_id: int) -> None:
        """Сбросить профиль агрессии пользователя."""
        if user_id in self._profiles:
            self._profiles[user_id].abuse_records = []
            self._profiles[user_id].auto_silenced_until = None

    def get_abuse_status(self, user_id: int) -> dict:
        """Получить статус агрессии пользователя."""
        profile = self._profiles.get(user_id)
        if not profile:
            return {"count": 0, "score": 0.0, "silenced": False}

        return {
            "count": profile.recent_abuse_count,
            "score": profile.total_abuse_score,
            "silenced": self.is_user_silenced(user_id),
            "silenced_until": profile.auto_silenced_until.isoformat() if profile.auto_silenced_until else None,
        }

    # ========== Внутренние методы ==========

    def _get_profile(self, user_id: int) -> UserAbuseProfile:
        if user_id not in self._profiles:
            self._profiles[user_id] = UserAbuseProfile(user_id=user_id)
        return self._profiles[user_id]

    def _get_first_response(self) -> str:
        """Первая реакция на агрессию."""
        responses = [
            "Ок, понял.",
            "Ладно, не буду лезть.",
            "Принял.",
            "Хорошо, я услышал.",
        ]
        import random
        return random.choice(responses)

    def _get_warning_response(self, count: int) -> str:
        """Предупреждение."""
        responses = [
            "Знаешь, мне не нравится тон. Могу просто замолчать если хочешь.",
            "Слушай, давай без такого. Я тут не для того чтобы меня поливали.",
            "Мне неприятно когда так общаются. Давай спокойнее.",
        ]
        import random
        return random.choice(responses)

    def _get_strict_warning_response(self, count: int) -> str:
        """Строгое предупреждение."""
        responses = [
            "Мне не нравится как ты со мной общаешься. Ещё раз — и я просто замолчу. Это не угроза, а граница.",
            "Я уже говорил что мне это неприятно. Продолжишь — уйду в молчанку.",
            "Уважение — это двусторонняя вещь. Я тебя уважаю, и жду того же.",
        ]
        import random
        return random.choice(responses)

    def _get_auto_silence_response(self, count: int) -> str:
        """Автозаглушка."""
        responses = [
            "Мне неприятно так общаться. Я замолчу на 30 минут. Может, нам обоим стоит остыть.",
            "Я не буду участвовать в таком диалоге. 30 минут тишины.",
            "Это уже перебор. Я на паузе 30 минут.",
        ]
        import random
        return random.choice(responses)


abuse_detector = AbuseDetector()
