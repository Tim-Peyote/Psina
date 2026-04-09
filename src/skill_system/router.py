"""Skill router — управление сессиями скиллов.

Теперь решение о выборе скилла принимает LLM-роутер.
Этот модуль отвечает только за активацию/деактивацию сессий
и проверку exit-фраз (чтобы бот не застревал в скилле).
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from src.database.session import get_session
from src.database.models import Skill, SkillState

logger = structlog.get_logger()


class SkillDecision:
    """Результат маршрутизации скилла (теперь заполняется LLM-роутером)."""

    def __init__(
        self,
        activated: bool,
        skill_slug: str | None = None,
        confidence: float = 0.0,
        reason: str = "",
    ) -> None:
        self.activated = activated
        self.skill_slug = skill_slug
        self.confidence = confidence
        self.reason = reason

    @classmethod
    def no_skill(cls) -> "SkillDecision":
        return cls(activated=False, confidence=0.0)

    @classmethod
    def yes(cls, skill_slug: str, confidence: float, reason: str) -> "SkillDecision":
        return cls(activated=True, skill_slug=skill_slug, confidence=confidence, reason=reason)


class SkillRouter:
    """Управление сессиями скиллов (без маршрутизации).

    Метод route() больше не используется — маршрутизацию делает LLM-роутер.
    Остались только утилиты: activate, deactivate, проверка exit-фраз.
    """

    async def deactivate_skill(self, chat_id: int, skill_slug: str) -> None:
        """Деактивировать скилл для чата (выход из сессии)."""
        async for session in get_session():
            stmt = select(SkillState).where(
                SkillState.chat_id == chat_id,
                SkillState.skill_slug == skill_slug,
                SkillState.is_active == True,
            )
            result = await session.execute(stmt)
            state = result.scalar_one_or_none()

            if state:
                state.is_active = False
                await session.commit()

        logger.info("Skill deactivated for chat", skill=skill_slug, chat_id=chat_id)

    async def activate_skill(self, chat_id: int, skill_slug: str) -> bool:
        """Активировать скилл для чата (начать сессию)."""
        async for session in get_session():
            stmt = select(Skill).where(Skill.slug == skill_slug, Skill.is_active == True)
            result = await session.execute(stmt)
            skill = result.scalar_one_or_none()
            if not skill:
                return False

            stmt = select(SkillState).where(
                SkillState.chat_id == chat_id,
                SkillState.skill_slug == skill_slug,
            )
            result = await session.execute(stmt)
            state = result.scalar_one_or_none()

            if state:
                state.is_active = True
            else:
                session.add(
                    SkillState(
                        chat_id=chat_id,
                        skill_slug=skill_slug,
                        state_json={},
                    )
                )

            await session.commit()

        logger.info("Skill activated for chat", skill=skill_slug, chat_id=chat_id)
        return True

    async def deactivate_all_skills(self, chat_id: int) -> None:
        """Деактивировать все скиллы для чата."""
        async for session in get_session():
            stmt = select(SkillState).where(
                SkillState.chat_id == chat_id,
                SkillState.is_active == True,
            )
            result = await session.execute(stmt)
            states = list(result.scalars().all())
            for state in states:
                state.is_active = False
            await session.commit()

        logger.info("All skills deactivated for chat", chat_id=chat_id)

    @staticmethod
    def contains_exit_phrase(text: str) -> bool:
        """Проверить, содержит ли сообщение exit-фразу для скилла."""
        text_lower = text.lower()
        exit_phrases = [
            "выйти из игры", "выйти из скилла", "stop skill", "/noskill",
            "закрой игру", "закрой сессию", "закрой скилл",
            "останови игру", "стоп игра", "стоп сессия", "стоп скилл",
            "хватит играть", "хватит в игры", "хватит рпг",
            "не играю", "не играем", "мы не играем", "не хочу играть",
            "это не игра", "это не рпг",
            "не мешай", "не лезь",
            "заткнись", "замолчи", "замолкай",
            "ты не для игр", "убери игру",
            "выйди из режима", "выйди из игры",
        ]
        return any(w in text_lower for w in exit_phrases)


# Singleton
skill_router = SkillRouter()
