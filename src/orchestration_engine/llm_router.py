"""
LLM Router — единый центр принятия решений через LLM.

Заменяет:
- SearchIntentDetector (regex-паттерны поиска)
- SkillRouter LLM-классификацию
- Частично MessageRouter (решение "отвечать или нет")

Возвращает структурированное решение в формате dataclass.
"""

import json
import re

import structlog

from src.config import settings

logger = structlog.get_logger()


class RouteAction:
    SEARCH = "search"
    ANSWER_DIRECTLY = "answer_directly"
    USE_SKILL = "use_skill"
    STAY_SILENT = "stay_silent"


class LLMRouteDecision:
    """Результат маршрутизации через LLM."""

    def __init__(
        self,
        action: str,
        confidence: float = 0.0,
        search_query: str = "",
        skill_slug: str = "",
        reasoning: str = "",
    ):
        self.action = action
        self.confidence = confidence
        self.search_query = search_query
        self.skill_slug = skill_slug
        self.reasoning = reasoning

    @property
    def should_search(self) -> bool:
        return self.action == RouteAction.SEARCH

    @property
    def should_use_skill(self) -> bool:
        return self.action == RouteAction.USE_SKILL

    @property
    def should_answer(self) -> bool:
        return self.action == RouteAction.ANSWER_DIRECTLY

    @property
    def should_be_silent(self) -> bool:
        return self.action == RouteAction.STAY_SILENT


class LLMRouter:
    """
    Маршрутизатор на базе LLM.

    Принимает решение: поиск / ответ / навык / молчание.
    Для поиска — извлекает чистый поисковый запрос.
    Для скилла — выбирает slug из доступных.
    """

    def __init__(self, llm_provider) -> None:
        self._llm = llm_provider

    async def route(
        self,
        message_text: str,
        is_private_chat: bool = False,
        has_active_session: bool = False,
        available_skills: list[dict] | None = None,
    ) -> LLMRouteDecision:
        """
        Определить действие для сообщения.

        Args:
            message_text: текст сообщения
            is_private_chat: личная переписка или группа
            has_active_session: есть ли активная сессия диалога
            available_skills: список доступных скиллов [{"slug": "...", "description": "..."}]
        """
        if not settings.web_search_enabled and not available_skills:
            # Если поиск выключен и скиллов нет — всегда отвечаем напрямую
            return LLMRouteDecision(
                action=RouteAction.ANSWER_DIRECTLY,
                confidence=0.5,
                reasoning="no search, no skills — answer directly",
            )

        system_prompt = self._build_system_prompt(available_skills)
        user_message = self._build_user_message(message_text, is_private_chat, has_active_session)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            response = await self._llm.generate_response(
                messages=messages,
                chat_id=0,
                user_id=0,
            )

            decision = self._parse_response(response, available_skills or [])

            logger.debug(
                "LLM route decision made",
                text=message_text[:80],
                action=decision.action,
                confidence=decision.confidence,
                reasoning=decision.reasoning[:100],
            )

            return decision

        except Exception as e:
            logger.error(
                "LLM routing failed, falling back to answer_directly",
                error=type(e).__name__,
                details=str(e),
            )
            # Fallback: отвечаем напрямую, LLM разберётся
            return LLMRouteDecision(
                action=RouteAction.ANSWER_DIRECTLY,
                confidence=0.3,
                reasoning=f"llm_routing_failed: {type(e).__name__}",
            )

    def _build_system_prompt(self, available_skills: list[dict] | None) -> str:
        """Системный промпт для маршрутизации."""
        skills_section = ""
        if available_skills:
            skills_list = "\n".join(
                f"  - {s['slug']}: {s['description']}" + (f" (команды: {', '.join('/' + c for c in s['commands'])})" if 'commands' in s else "")
                for s in available_skills
            )
            skills_section = f"""
ДОСТУПНЫЕ НАВЫКИ:
{skills_list}

Если сообщение относится к команде или описанию навыка — выбери action=use_skill и укажи skill_slug.
"""
        else:
            skills_section = """
ДОСТУПНЫЕ НАВЫКИ: нет
"""

        prompt = f"""Ты — маршрутизатор сообщений в Telegram-боте. Твоя задача — решить, какое действие выполнить для каждого входящего сообщения.

ВОЗМОЖНЫЕ ДЕЙСТВИЯ:
- search: нужен поиск в интернете (факты, цены, погода, новости, "найди/погугли/поищи")
- answer_directly: бот может ответить из контекста/памяти (приветствия, вопросы о боте, болтовня, продолжение диалога)
- use_skill: сообщение относится к одному из навыков (игры, RPG, специальные команды)
- stay_silent: сообщение не требует ответа (короткие междометия, бот не обращается к сообщению)
{skills_section}
ПРАВИЛА:
1. Если пользователь прос найти что-то в интернете ("найди", "поищи", "погугли", "сколько стоит", "какая погода", "кто выиграл") — выбери search и извлеки чистый поисковый запрос.
2. Если сообщение относится к навыку — выбери use_skill с правильным slug.
3. Если это обычный диалог, вопрос к боту, приветствие — выбери answer_directly.
4. Если это короткое сообщение типа "ок", "да", "нет", "ага", "lol" — выбери stay_silent.
5. Опечатки — это нормально. "нади" = "найди", "паищи" = "поищи".
6. Извлекай поисковый запрос без мусора: убери "найди", "в интернете", "бот", обращения.

ОТВЕЧАЙ СТРОГО В ФОРМАТЕ JSON (без markdown, без обёрток):
{{"action": "search|answer_directly|use_skill|stay_silent", "search_query": "только для search", "skill_slug": "только для use_skill", "confidence": 0.95, "reasoning": "короткое объяснение"}}"""
        return prompt

    def _build_user_message(
        self,
        text: str,
        is_private_chat: bool,
        has_active_session: bool,
    ) -> str:
        """Сообщение для LLM с контекстом."""
        context_parts = [f"Сообщение: {text}"]
        if is_private_chat:
            context_parts.append("Контекст: личная переписка")
        if has_active_session:
            context_parts.append("Контекст: есть активная сессия диалога")
        return "\n".join(context_parts)

    def _parse_response(
        self,
        response: str,
        available_skills: list[dict],
    ) -> LLMRouteDecision:
        """Распарсить JSON-ответ от LLM."""
        # Извлекаем JSON из ответа (может быть обёрнут в ```json ... ```)
        response = response.strip()
        json_match = re.search(r'\{[^}]+\}', response, re.DOTALL)
        if not json_match:
            logger.warning("LLM routing: no JSON found in response", response=response[:200])
            return LLMRouteDecision(
                action=RouteAction.ANSWER_DIRECTLY,
                confidence=0.3,
                reasoning="no_json_in_llm_response",
            )

        try:
            data = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            logger.warning("LLM routing: invalid JSON", response=response[:200])
            return LLMRouteDecision(
                action=RouteAction.ANSWER_DIRECTLY,
                confidence=0.3,
                reasoning="invalid_json",
            )

        action = data.get("action", RouteAction.ANSWER_DIRECTLY)
        confidence = float(data.get("confidence", 0.5))
        search_query = (data.get("search_query") or "").strip()
        skill_slug = (data.get("skill_slug") or "").strip()
        reasoning = data.get("reasoning", "")

        # Валидация action
        if action not in (RouteAction.SEARCH, RouteAction.ANSWER_DIRECTLY, RouteAction.USE_SKILL, RouteAction.STAY_SILENT):
            logger.warning("LLM routing: unknown action", action=action)
            action = RouteAction.ANSWER_DIRECTLY

        # Валидация skill_slug
        if action == RouteAction.USE_SKILL:
            valid_slugs = {s["slug"] for s in available_skills}
            if skill_slug not in valid_slugs:
                logger.warning("LLM routing: invalid skill_slug", slug=skill_slug, valid=list(valid_slugs))
                action = RouteAction.ANSWER_DIRECTLY
                skill_slug = ""

        # Валидация search_query
        if action == RouteAction.SEARCH and not search_query:
            logger.warning("LLM routing: search action but no query")
            action = RouteAction.ANSWER_DIRECTLY

        return LLMRouteDecision(
            action=action,
            confidence=confidence,
            search_query=search_query,
            skill_slug=skill_slug,
            reasoning=reasoning,
        )


# Singleton (инициализируется в orchestrator с правильным llm_provider)
llm_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    if llm_router is None:
        raise RuntimeError("LLM router not initialized. Call init_router() first.")
    return llm_router


def init_router(llm_provider) -> None:
    global llm_router
    llm_router = LLMRouter(llm_provider)
