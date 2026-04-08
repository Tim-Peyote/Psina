"""Skill router — decides which skill to activate based on message context.

The router uses two strategies:
1. Explicit triggers (commands, keywords matching skill.triggers)
2. LLM-based intent classification (when no explicit trigger matches)
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from src.database.session import get_session
from src.database.models import Skill, SkillState
from src.message_processor.processor import NormalizedMessage
from src.skill_system.registry import skill_registry
from src.skill_system.state_manager import skill_state_manager
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()


# Commands that always activate a specific skill
SKILL_COMMANDS = {
    "rpg": "agent_rpg",
    "game": "agent_rpg",
}


class SkillDecision:
    """Result of skill routing."""

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
    """Decide which skill should handle a message."""

    def __init__(self) -> None:
        self.llm_provider = LLMProvider.get_provider()

    async def route(self, msg: NormalizedMessage) -> SkillDecision:
        """Route a message to the appropriate skill.

        Priority:
        1. Explicit skill commands (/rpg, /game, etc.)
        2. Trigger keyword matching
        3. LLM-based intent classification
        """
        # 1. Check explicit commands
        if msg.is_command and msg.command:
            cmd = msg.command.lower()
            if cmd in SKILL_COMMANDS:
                slug = SKILL_COMMANDS[cmd]
                return SkillDecision.yes(
                    skill_slug=slug,
                    confidence=1.0,
                    reason=f"command /{cmd} → skill {slug}",
                )

            # /skills command is handled by orchestrator, not a skill
            if cmd == "skills":
                return SkillDecision.no_skill()

        # 2. Check if a skill is already active for this chat
        active_skills = await skill_state_manager.get_all_active_skills(msg.chat_id)
        if active_skills:
            # If user is already in a skill session, keep them there
            # unless they explicitly exit or the message is clearly not about the skill
            text_lower = msg.text.lower()

            # Wide exit conditions — user wants to leave skill session
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
                # Without bot name — standalone
                "закрой", "хватит", "стоп", "хватит играть",
            ]
            if any(w in text_lower for w in exit_phrases):
                # Deactivate ALL active skills for this chat
                for slug in active_skills:
                    await self.deactivate_skill(msg.chat_id, slug)
                logger.info("Skill session exited by user", skills=active_skills, chat_id=msg.chat_id)
                return SkillDecision.no_skill()

            # Check if message is clearly NOT about the active skill
            non_skill_indicators = [
                "погода", "курс ", "кто выиграл", "что случилось", "какая цена",
                "сколько стоит", "новости", "какие новости",
                "как дела", "что делаешь", "помоги", "объясни", "расскажи", "найди",
                "гугл", "поиск",
            ]
            is_clearly_non_skill = any(w in text_lower for w in non_skill_indicators)
            if is_clearly_non_skill:
                for slug in active_skills:
                    await self.deactivate_skill(msg.chat_id, slug)
                logger.info("Dropped stuck skill session", skills=active_skills, text=msg.text[:80], chat_id=msg.chat_id)
                return SkillDecision.no_skill()

            # Continue active skill
            if len(active_skills) == 1:
                slug = active_skills[0]
                return SkillDecision.yes(
                    skill_slug=slug,
                    confidence=0.8,
                    reason=f"continuing active skill session: {slug}",
                )

        # 3. Trigger keyword matching
        async for session in get_session():
            stmt = select(Skill).where(Skill.is_active == True)
            result = await session.execute(stmt)
            skills = list(result.scalars().all())

        for skill in skills:
            if not skill.triggers:
                continue
            text_lower = msg.text.lower()
            for trigger in skill.triggers:
                if trigger.lower() in text_lower:
                    return SkillDecision.yes(
                        skill_slug=skill.slug,
                        confidence=0.6,
                        reason=f"trigger '{trigger}' matched skill {skill.slug}",
                    )

        # 4. LLM-based classification (only for longer messages in group chats)
        if len(msg.text) > 15 and msg.chat_type in ("group", "supergroup"):
            return await self._llm_classify(msg.text, skills)

        return SkillDecision.no_skill()

    async def _llm_classify(
        self,
        text: str,
        skills: list,  # Can be Skill or SkillMetadata
    ) -> SkillDecision:
        """Use LLM to classify message into a skill.

        Uses only descriptions (~50-100 tokens each), not full content.
        This is the Discovery-phase classification.
        """
        if not skills:
            return SkillDecision.no_skill()

        # Build description list from registry (cheap)
        all_desc = skill_registry.get_all_descriptions()
        if not all_desc:
            return SkillDecision.no_skill()

        skill_descriptions = "\n".join(
            f"- {slug}: {desc}" for slug, desc in all_desc.items()
        )

        prompt = f"""Определи, подходит ли сообщение пользователя к одному из этих скиллов.

Доступные скиллы:
{skill_descriptions}

Если сообщение явно относится к одному из скиллов — ответь с его slug.
Если не относится ни к одному — ответь "none".

Сообщение: {text}

Ответ (только slug или none):"""

        try:
            messages = [
                {"role": "user", "content": prompt},
            ]
            response = await self.llm_provider.generate_response(messages=messages)
            response_slug = response.strip().lower().replace("none", "")

            for slug in all_desc:
                if slug.lower() == response_slug:
                    return SkillDecision.yes(
                        skill_slug=slug,
                        confidence=0.5,  # LLM classification is uncertain
                        reason=f"LLM classified as {slug}",
                    )
        except Exception:
            logger.exception("LLM skill classification failed")

        return SkillDecision.no_skill()

    async def deactivate_skill(self, chat_id: int, skill_slug: str) -> None:
        """Deactivate a skill for a chat (exit session)."""
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
        """Activate a skill for a chat (start session)."""
        async for session in get_session():
            # Check skill exists
            stmt = select(Skill).where(Skill.slug == skill_slug, Skill.is_active == True)
            result = await session.execute(stmt)
            skill = result.scalar_one_or_none()
            if not skill:
                return False

            # Create or reactivate state
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


# Singleton
skill_router = SkillRouter()
