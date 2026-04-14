"""
Orchestrator — центральный мозг бота.

Новый пайплайн:
  message → track_context → ingest_memory → extract_facts → track_relationships
  → route_message → handle_behavior_control / direct_call / session / ignore
  → anti_chaos_check → build_context → generate → save
"""

import enum
import re
from datetime import datetime, timezone

import structlog

from src.config import settings
from src.message_processor.processor import NormalizedMessage
from src.memory_engine.engine import MemoryEngine
from src.memory_engine.fact_extractor import fact_extractor
from src.memory_engine.relationship_engine import relationship_engine
from src.llm_adapter.base import LLMProvider
from src.retrieval_engine.retriever import Retriever
from src.summarizer.daily import DailySummarizer
from src.game_engine.manager import GameManager
from src.context_tracker.tracker import context_tracker
from src.orchestration_engine.personality import bot_personality
from src.orchestration_engine.llm_router import LLMRouter, LLMRouteDecision, RouteAction, get_router, init_router
from src.orchestration_engine.message_router import message_router, MessageRoute, RoutingDecision
from src.orchestration_engine.session_manager import session_manager
from src.orchestration_engine.anti_chaos import anti_chaos
from src.orchestration_engine.trigger_system import trigger_system, ConfidenceLevel
from src.skill_system.router import SkillDecision as SkillDecisionClass
from src.orchestration_engine.knowledge_analyzer import knowledge_analyzer
from src.orchestration_engine.vibe_adapter import vibe_adapter
from src.orchestration_engine.censorship_manager import censorship_manager
from src.orchestration_engine.abuse_detector import abuse_detector
from src.orchestration_engine.emotional_state import emotional_state_manager
from src.workers.reminders import reminder_manager
from src.web_search_engine.processor import search_processor
# New memory services
from src.memory_services.context_pack import context_pack_builder
from src.memory_services.retrieval_service import retrieval_service
# Skill system
from src.skill_system.router import skill_router
from src.skill_system.registry import skill_registry

logger = structlog.get_logger()


class ActivityLevel(enum.StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class ChatSettings:
    """Настройки конкретного чата."""

    def __init__(self, chat_id: int) -> None:
        from datetime import timedelta
        self.chat_id = chat_id
        self.mode: str = settings.bot_mode
        self.activity: ActivityLevel = ActivityLevel.NORMAL
        self.silence_until: datetime | None = None
        self.mention_only: bool = False

    @property
    def is_silenced(self) -> bool:
        if self.silence_until is None:
            return False
        return datetime.now(timezone.utc) < self.silence_until

    def silence(self, minutes: int) -> None:
        from datetime import timedelta
        self.silence_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)


class Orchestrator:
    """
    Центральный мозг бота.

    Главное правило:
    Бот читает всё → понимает контекст → отвечает только при высокой уверенности.
    Ошибка "промолчал" допустима. Ошибка "влез не туда" — критическая.
    """

    def __init__(self) -> None:
        self.memory_engine = MemoryEngine()
        self.llm_provider = LLMProvider.get_provider()
        self.retriever = Retriever()
        self.summarizer = DailySummarizer()
        self.game_manager = GameManager()

        # Инициализируем LLM-роутер
        init_router(self.llm_provider)

        # Настройки чатов
        self._chat_settings: dict[int, ChatSettings] = {}
        # Tracks which chats have had users loaded from DB
        self._chat_users_loaded: set[int] = set()

        # Last skill error for debug
        self._last_skill_error: dict | None = None

        logger.info("Orchestrator initialized", bot_name=settings.bot_name)

    def _get_settings(self, chat_id: int) -> ChatSettings:
        if chat_id not in self._chat_settings:
            self._chat_settings[chat_id] = ChatSettings(chat_id)
        return self._chat_settings[chat_id]

    async def process_message(self, msg: NormalizedMessage) -> str | None:
        """
        Полный пайплайн обработки сообщения.
        """
        try:
            return await self._process_message_inner(msg)
        except Exception:
            logger.exception(
                "Critical error in process_message",
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                text=msg.text[:100],
            )
            return None  # Silence is better than crash

    async def _process_message_inner(self, msg: NormalizedMessage) -> str | None:
        """Inner message processing pipeline."""
        # Сбрасываем consecutive при сообщении от пользователя
        anti_chaos.record_user_message(msg.chat_id)

        # ===== ФАЗА 1: СЛУШАЕМ И ЗАПОМИНАЕМ (всегда) =====

        # 0. Ensure we know chat participants (loads from DB after restart)
        if msg.chat_id not in self._chat_users_loaded:
            await context_tracker.load_chat_users_from_db(msg.chat_id)
            self._chat_users_loaded.add(msg.chat_id)

        # 1. Трекаем контекст и вайб
        context = context_tracker.get_context_for_message(msg)
        vibe_adapter.analyze_message(msg.chat_id, msg.text)

        # 1.1. Обновляем эмоциональное состояние
        trigger_eval = trigger_system.evaluate(
            msg.text,
            is_reply=msg.reply_to_message_id is not None,
            reply_to_bot=False,
            in_active_session=session_manager.is_user_in_session(msg.chat_id, msg.user_id),
            chat_id=msg.chat_id,
        )
        is_directed = trigger_eval.level == ConfidenceLevel.HIGH
        await emotional_state_manager.process_message(
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            text=msg.text,
            is_directed_at_bot=is_directed,
        )

        # Learn new nicknames: if user calls bot by a name that's not in known list,
        # but it looks like a call (name at start of message), learn it
        text_stripped = msg.text.strip().split()[0].lower().rstrip(".,!?:") if msg.text.strip() else ""
        known_names = trigger_system.get_all_names_for_chat(msg.chat_id)
        if text_stripped and text_stripped not in known_names and len(text_stripped) >= 2:
            # Check if this is actually a call pattern: "Бобик, ..." or "Бобик ..."
            if re.match(rf'^{re.escape(text_stripped)}[,\s:!?]', msg.text, re.IGNORECASE):
                trigger_system.learn_nickname(msg.chat_id, text_stripped)

        # 2. Сохраняем в память
        await self.memory_engine.ingest_message(msg)

        # 3. Извлекаем факты
        facts = await fact_extractor.extract_and_save(msg)

        # 4. Трекаем связи
        await relationship_engine.extract_relationship_from_message(msg)

        # 5. Обновляем профили (per-chat)
        if len(msg.text) > 30:
            await fact_extractor.update_profile_from_facts(msg.user_id, msg.chat_id)
            await relationship_engine.update_profile_relationships(msg.user_id, msg.chat_id)

        # ===== ФАЗА 1.6: ДЕТЕКЦИЯ АГРЕССИИ =====
        abuse_result = await abuse_detector.analyze(msg)
        abuse_context: str | None = None
        if abuse_result["is_abuse"] and abuse_result["action"] != "ignore":
            action = abuse_result["action"]
            count = abuse_result.get("abuse_count", 1)
            if action == "auto_silence":
                self._get_settings(msg.chat_id).silence(30)
                abuse_context = (
                    f"РЕЖИМ ОТВЕТА: АГРЕССИЯ (авто-молчание). Пользователь грубит {count}-й раз подряд. "
                    "Ответь один раз органично — можешь послать, огрызнуться, устало попрощаться. "
                    "После этого ответа бот уходит в тишину на 30 минут. "
                    "НЕ говори «я замолчу» как угрозу — просто сделай это, попрощавшись по-своему."
                )
            elif action == "strict_warning":
                abuse_context = (
                    f"РЕЖИМ ОТВЕТА: АГРЕССИЯ (строгое предупреждение #{count}). Тебя грубо задели. "
                    "Можешь ответить жёстко, с матом если уместно по вайбу чата. "
                    "Дай понять что ещё раз — и тебе станет неинтересно общаться."
                )
            elif action == "warning":
                abuse_context = (
                    f"РЕЖИМ ОТВЕТА: АГРЕССИЯ (предупреждение #{count}). Тебя задели. "
                    "Отреагируй органично — огрызнись, поддень, пошути в ответ. "
                    "Не читай лекции и не угрожай молчанием — просто ответь как живой человек."
                )

        # Команды регулировки характера (обрабатываем но не прерываем пайплайн)
        personality_cmd = vibe_adapter.detect_personality_command(msg.text)
        if personality_cmd:
            import asyncio
            asyncio.ensure_future(vibe_adapter.apply_personality_command(msg.chat_id, personality_cmd))

        # ===== ФАЗА 1.7: LLM-МАРШРУТИЗАЦИЯ =====
        # LLM-роутер решает: поиск / скилл / ответить / молчать
        # Пропускаем роутер если:
        #   - есть контекст агрессии (бот должен ответить)
        #   - прямой вызов по имени (HIGH confidence) — роутер не должен глушить своё же обращение
        trigger_pre = trigger_system.evaluate(
            msg.text,
            is_reply=msg.reply_to_message_id is not None,
            reply_to_bot=False,
            in_active_session=session_manager.is_user_in_session(msg.chat_id, msg.user_id),
            chat_id=msg.chat_id,
        )
        is_direct_call = trigger_pre.level == ConfidenceLevel.HIGH
        if abuse_context is None and not is_direct_call:
            route_decision = await self._llm_route(msg)
            if route_decision is not None:
                return route_decision  # "" = hard silence, non-empty = actual response

        # ===== ФАЗА 2: БЫСТРЫЕ КОМАНДЫ (fast-path) =====

        settings_obj = self._get_settings(msg.chat_id)

        # 6. Проверяем silence
        if settings_obj.is_silenced:
            logger.debug("Chat is silenced", chat_id=msg.chat_id)
            return None

        # 7. Проверяем цензуру — команда смены уровня
        new_censorship = censorship_manager.parse_level_from_text(msg.text)
        if new_censorship:
            trigger = trigger_system.evaluate(
                msg.text,
                is_reply=msg.reply_to_message_id is not None,
                reply_to_bot=False,
                in_active_session=session_manager.is_user_in_session(msg.chat_id, msg.user_id),
                chat_id=msg.chat_id,
            )
            if trigger.level == ConfidenceLevel.HIGH:
                censorship_manager.set_level(msg.chat_id, new_censorship)
                level_texts = {
                    "strict": "Понял, буду аккуратнее с выражениями.",
                    "moderate": "Ок, умеренный режим.",
                    "free": "Понял, без фильтров.",
                }
                return level_texts.get(new_censorship.value, "Принял.")

        # 8. Классифицируем сообщение (fast-path: behavior_control, game)
        decision = message_router.route(msg)

        logger.debug(
            "Message routed",
            route=decision.route.value,
            confidence=decision.confidence,
            reason=decision.reason,
        )

        # ===== ФАЗА 3: ДЕЙСТВУЕМ =====

        # Behavior control — меняем режим
        if decision.route == MessageRoute.BEHAVIOR_CONTROL:
            return await self._handle_behavior_control(msg.chat_id, decision.behavior_action, msg.user_id)

        # Game interaction
        if decision.route == MessageRoute.GAME_INTERACTION:
            return await self._handle_game_command(msg)

        # Прямая агрессия к боту — ответить несмотря на роутинг
        if (abuse_context is not None
                and abuse_result.get("type") == "direct"
                and decision.route in (MessageRoute.BACKGROUND, MessageRoute.IGNORE)):
            return await self._generate_response(msg, context, decision, abuse_context=abuse_context)

        # Background — только запомнить
        if decision.route == MessageRoute.BACKGROUND:
            return None

        # Ignore — чужой разговор
        if decision.route == MessageRoute.IGNORE:
            return None

        # Direct call или session continuation
        if decision.should_respond:
            return await self._generate_response(msg, context, decision, abuse_context=abuse_context)

        # Не должен отвечать
        return None

    async def _llm_route(self, msg: NormalizedMessage) -> str | None:
        """
        Фаза LLM-маршрутизации.

        LLM решает: поиск / скилл / ответить / молчать.
        Если решение требует действия — выполняет и возвращает ответ.
        Если LLM решил "answer_directly" — возвращает None (дальше идёт обычный pipeline).
        """
        router = get_router()

        # Собираем доступные скиллы с командами и описаниями
        all_desc = skill_registry.get_all_descriptions()
        available_skills = []
        for slug, desc in all_desc.items():
            commands = skill_registry.get_skill_command_for_slug(slug)
            skill_info = {"slug": slug, "description": desc}
            if commands:
                skill_info["commands"] = commands
            available_skills.append(skill_info)

        # Проверяем есть ли активная сессия скилла
        from src.skill_system.state_manager import skill_state_manager
        active_skills = await skill_state_manager.get_all_active_skills(msg.chat_id)

        # Вызываем LLM-роутер
        decision = await router.route(
            message_text=msg.text,
            is_private_chat=msg.is_private,
            has_active_session=bool(active_skills),
            available_skills=available_skills,
        )

        logger.debug(
            "LLM route decision",
            text=msg.text[:80],
            action=decision.action,
            confidence=decision.confidence,
            reasoning=decision.reasoning[:100],
        )

        # === ACTION: SEARCH ===
        if decision.should_search:
            logger.info(
                "Web search triggered (LLM)",
                query=decision.search_query,
                confidence=decision.confidence,
                reason=decision.reasoning,
            )
            search_response = await search_processor.search_and_answer(decision.search_query)
            message_router.register_bot_message(msg.telegram_id)
            if search_response:
                return search_response
            logger.warning("Search returned empty response (LLM)", query=decision.search_query)
            # Если поиск пустой — fallback на обычный ответ
            return None

        # === ACTION: EXIT_SKILL — user wants to leave skill session ===
        if decision.should_exit_skill:
            await skill_router.deactivate_all_skills(msg.chat_id)
            logger.info("Skill session exited via LLM route", chat_id=msg.chat_id)
            return "🏁 Сессия остановлена."

        # === ACTION: USE_SKILL ===
        if decision.should_use_skill:

            try:
                skill_slug = decision.skill_slug
                logger.info("skill_debug: before activate_skill", skill=skill_slug, chat_id=msg.chat_id)
                await skill_router.activate_skill(msg.chat_id, skill_slug)
                logger.info("skill_debug: after activate_skill", skill=skill_slug)

                skill_decision = SkillDecisionClass.yes(
                    skill_slug=skill_slug,
                    confidence=decision.confidence,
                    reason=decision.reasoning,
                )
                logger.info("skill_debug: SkillDecision created", skill=skill_slug)
                logger.info(
                    "Skill activated (LLM)",
                    skill=skill_slug,
                    chat_id=msg.chat_id,
                    confidence=decision.confidence,
                    reason=decision.reasoning,
                )
                logger.info("skill_debug: before _handle_skill", skill=skill_slug)
                return await self._handle_skill(msg, skill_decision)
            except Exception as e:
                import traceback
                logger.error(
                    "skill_debug: CRASH in use_skill block",
                    skill=decision.skill_slug if decision else "unknown",
                    error_type=type(e).__name__,
                    error=str(e),
                    traceback=traceback.format_exc(),
                    chat_id=msg.chat_id,
                    user_id=msg.user_id,
                )
                raise

        # === ACTION: STAY_SILENT ===
        if decision.should_be_silent:
            logger.debug("LLM decided to stay silent", text=msg.text[:80])
            # High confidence silence — but if there's an active skill session,
            # don't hard-silence: let the skill handler decide whether to respond.
            # The skill knows its own context (e.g. Session Zero is waiting for an answer).
            if decision.confidence >= 0.8 and not active_skills:
                return ""  # Empty string = hard silence, pipeline stops
            return None  # Let normal pipeline / skill handler decide

        # === ACTION: ANSWER_DIRECTLY ===
        # LLM решил что бот может ответить — идём дальше по обычному pipeline
        # (message_router.route() определит direct_call vs session_continuation)
        return None

    async def _generate_response(
        self,
        msg: NormalizedMessage,
        context: dict,
        decision: RoutingDecision,
        abuse_context: str | None = None,
    ) -> str | None:
        """Сгенерировать ответ с анализом знаний."""
        # Обновляем сессию
        if decision.trigger.is_explicit_call:
            session = session_manager.create_session(
                msg.chat_id, msg.user_id, msg.reply_to_message_id,
            )
        else:
            session_manager.update_activity(msg.chat_id, msg.user_id, msg.text)

        # Anti-chaos: проверяем
        is_urgent = decision.trigger.is_reply_to_bot or decision.trigger.is_explicit_call
        in_session = session_manager.is_user_in_session(msg.chat_id, msg.user_id)
        can_respond, reason = anti_chaos.can_respond(msg.chat_id, is_urgent=is_urgent or in_session)
        if not can_respond:
            logger.debug("Anti-chaos blocked response", reason=reason)
            return None

        # ===== АНАЛИЗ ЗНАНИЙ =====
        knowledge_report = await knowledge_analyzer.analyze(msg, context)
        strategy = knowledge_analyzer.get_response_strategy(knowledge_report)

        logger.debug(
            "Knowledge analysis",
            strategy=strategy,
            has_info=knowledge_report.has_enough_info,
        )

        # Собираем контекст для LLM
        settings_obj = self._get_settings(msg.chat_id)
        activity = settings_obj.activity.value

        # Инструкции от вайба и цензуры
        vibe_instruction = vibe_adapter.get_style_instruction(msg.chat_id)
        vibe_profile = vibe_adapter.get_profile(msg.chat_id)
        censorship_instruction = censorship_manager.get_instruction_for_llm(
            msg.chat_id,
            mate_level=vibe_profile.mate_level,
        )

        # Эмоциональное состояние
        emo_state = await emotional_state_manager.get_state(msg.chat_id)
        emo_hint = emo_state.get_prompt_hint()
        user_tone_hint = emo_state.get_user_tone_hint(msg.user_id)

        system_prompt = bot_personality.get_system_prompt(
            context=context,
            activity_level=activity,
            censorship_instruction=censorship_instruction,
            vibe_instruction=vibe_instruction,
            abuse_context=abuse_context,
        )

        # Добавляем эмоциональное состояние в промпт
        if emo_hint:
            system_prompt += f"\n\nТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ: {emo_hint}"
        if user_tone_hint:
            system_prompt += f"\n{user_tone_hint}"

        # Добавляем knowledge report
        knowledge_context = knowledge_report.summary_text
        if knowledge_context:
            context["knowledge_context"] = knowledge_context

        # Если стратегия — уточнить, добавляем подсказку
        if strategy == "ask_clarification" and knowledge_report.clarification_prompt:
            system_prompt += f"\n\n{knowledge_report.clarification_prompt}"
        elif strategy == "admit_ignorance":
            system_prompt += "\n\nУ тебя нет знаний по этой теме. Честно признайся что не знаешь."

        # Session context
        session_ctx = session_manager.get_session_context_for_llm(msg.chat_id, msg.user_id)
        context["session_context"] = session_ctx

        # ===== НОВАЯ СИСТЕМА ПАМЯТИ: используем context pack builder =====
        # Собираем ограниченный контекст с релевантной памятью
        reply_ctx = context.get("reply_context")
        web_context_str = ""  # Web search returns directly, never reaches here
        context_pack = await context_pack_builder.build_context_pack(
            system_prompt=system_prompt,
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            query=msg.text,  # Use user message as query for memory retrieval
            include_user_profile=True,
            include_web_context=web_context_str,
            knowledge_context=knowledge_context,
            reply_context=reply_ctx,
        )

        # Формируем сообщения из context pack
        llm_messages = context_pack_builder.format_pack_for_llm(context_pack)

        # Current message is ALREADY in recent_messages from DB (saved by ingest_message)
        # Don't duplicate it. The last message in recent_messages IS the current one.
        # Only add emotional/tone/strategy hints on top.

        # Эмоциональная подсказка для LLM (НЕ готовый ответ!)
        emotional = bot_personality.get_emotional_response(msg.text)
        if emotional:
            # Даём LLM понять контекст, но НЕ диктуем ответ
            emotional_hints = {
                "greeting": "Пользователь приветствует. Отреагируй тепло, по-своему.",
                "farewell": "Пользователь прощается. Скажи что-то от себя.",
                "agreement": "Пользователь соглашается. Подхвати настрой.",
                "surprise": "Пользователь удивлён. Можешь отреагировать.",
                "support": "Пользователю непросто. Будь рядом, но не шаблонно.",
            }
            hint = emotional_hints.get(emotional)
            if hint:
                llm_messages.append({
                    "role": "system",
                    "content": hint,
                })

        # Тон
        tone = bot_personality.adjust_tone(msg.text)
        if tone == "supportive":
            llm_messages.append({
                "role": "system",
                "content": "Человеку непросто — отвечай мягко и с поддержкой.",
            })
        elif tone == "playful":
            llm_messages.append({
                "role": "system",
                "content": "Можно ответить с юмором и лёгкостью.",
            })
        elif tone == "calm":
            llm_messages.append({
                "role": "system",
                "content": "Отвечай спокойно и нейтрально. Не спорь, не огрызайся.",
            })

        # Стратегия ответа
        if strategy == "answer_with_uncertainty":
            llm_messages.append({
                "role": "system",
                "content": "Ты не до конца уверен. Скажи что помнишь, но подчеркни что можешь ошибаться.",
            })

        # Генерируем
        response = await self.llm_provider.generate_response(
            messages=llm_messages,
            chat_id=msg.chat_id,
            user_id=msg.user_id,
        )

        # Сохраняем ответ
        await self.memory_engine.save_bot_response(response, msg.chat_id, msg.user_id)

        # Anti-chaos: записываем
        anti_chaos.record_response(msg.chat_id)

        return response

    async def _handle_behavior_control(
        self, chat_id: int, action: str | None, user_id: int,
    ) -> str:
        """Обработка команд управления поведением."""
        settings = self._get_settings(chat_id)

        if action == "silence":
            settings.silence(60)  # молчим 1 час
            response = bot_personality.get_silence_response()
            logger.info("Chat silenced", chat_id=chat_id, user_id=user_id)
            return response

        if action == "more_active":
            settings.activity = ActivityLevel.HIGH
            response = bot_personality.get_more_active_response()
            logger.info("Activity increased", chat_id=chat_id)
            return response

        if action == "less_active":
            settings.activity = ActivityLevel.LOW
            response = bot_personality.get_less_active_response()
            logger.info("Activity decreased", chat_id=chat_id)
            return response

        if action == "mention_only":
            settings.mention_only = True
            settings.activity = ActivityLevel.LOW
            response = bot_personality.get_mention_only_response()
            logger.info("Mention-only mode", chat_id=chat_id)
            return response

        # Unknown action
        return ""

    async def _handle_skill(
        self,
        msg: NormalizedMessage,
        decision,  # SkillDecision type
    ) -> str | None:
        """Delegate message processing to an active skill handler."""
        import traceback as tb_module

        skill_slug = decision.skill_slug
        if not skill_slug:
            return None

        stage = "unknown"
        try:
            # Stage 1: Activate skill (load full SKILL.md)
            stage = "activate"
            logger.info("Skill activation start", skill=skill_slug, chat_id=msg.chat_id, user_id=msg.user_id)
            skill = await skill_registry.activate_skill_by_slug(skill_slug)
            if not skill:
                logger.error("Skill not found", skill=skill_slug, stage=stage)
                await skill_router.deactivate_skill(msg.chat_id, skill_slug)
                return "Не могу запустить навык. Попробуй ещё раз."

            # Stage 2: Get handler
            stage = "get_handler"
            logger.debug("Skill handler resolution", skill=skill_slug, stage=stage)
            handler = skill_registry.get_skill_handler(skill_slug)
            if not handler:
                logger.error("No handler for skill", skill=skill_slug, stage=stage)
                await skill_router.deactivate_skill(msg.chat_id, skill_slug)
                return "Не могу запустить навык. Попробуй ещё раз."

            handler_type = "custom" if hasattr(handler, "process_message") else "default"
            logger.debug("Skill handler ready", skill=skill_slug, handler_type=handler_type, stage=stage)

            # Stage 3: Execute handler
            stage = "execute"
            logger.debug("Skill handler execution start", skill=skill_slug, handler_type=handler_type, chat_id=msg.chat_id)

            # PromptOnlySkill: default_handler(skill, msg, chat_id, user_id)
            # ExecutableSkill: handler.process_message(msg, chat_id, user_id)
            if callable(handler) and not hasattr(handler, "process_message"):
                response = await handler(skill, msg, msg.chat_id, msg.user_id)
            else:
                response = await handler.process_message(msg, msg.chat_id, msg.user_id)

            logger.debug("Skill handler execution done", skill=skill_slug, response_len=len(response) if response else 0)
            return response

        except Exception as e:
            tb = tb_module.format_exc()
            logger.error(
                "Skill handler failed",
                skill=skill_slug,
                stage=stage,
                error_type=type(e).__name__,
                error=str(e),
                traceback=tb,
                chat_id=msg.chat_id,
                user_id=msg.user_id,
            )
            self._last_skill_error = {
                "skill": skill_slug,
                "stage": stage,
                "error": str(e),
                "error_type": type(e).__name__,
                "traceback": tb,
                "chat_id": msg.chat_id,
            }
            try:
                await skill_router.deactivate_skill(msg.chat_id, skill_slug)
            except Exception:
                logger.debug("Failed to deactivate skill after error", skill=skill_slug)

            # Fallback: normal response pipeline instead of silence
            context = context_tracker.get_context_for_message(msg)
            return await self._generate_response(msg, context, self._fallback_decision(msg))

    def _fallback_decision(self, msg):
        """Create a routing decision for fallback after skill error."""
        trigger = trigger_system.evaluate(
            text=msg.text,
            is_reply=msg.reply_to_message_id is not None,
            reply_to_bot=True,  # Treat as direct call since user expects response
            in_active_session=session_manager.is_user_in_session(msg.chat_id, msg.user_id),
            chat_id=msg.chat_id,
        )
        return RoutingDecision(
            route=MessageRoute.DIRECT_CALL,
            confidence=trigger.confidence,
            trigger=trigger,
            reason="fallback_from_skill_error",
            should_respond=True,
        )

    async def _handle_game_command(self, msg: NormalizedMessage) -> str:
        """Обработка игровой команды."""
        args = msg.command_args if msg.command_args else []

        if not args:
            return (
                "🎮 <b>Игровые команды:</b>\n\n"
                "/game start [name] — начать игру\n"
                "/game status — статус игры\n"
                "/game continue — продолжить\n"
                "/game log — журнал событий\n"
                "/game end — завершить игру"
            )

        action = args[0].lower()

        if action == "start":
            name = " ".join(args[1:]) if len(args) > 1 else f"DnD Session {msg.user_id}"
            return await self.game_manager.start_session(msg.chat_id, msg.user_id, name)
        if action == "status":
            return await self.game_manager.get_status(msg.chat_id)
        if action == "continue":
            return await self.game_manager.resume_session(msg.chat_id)
        if action == "log":
            return await self.game_manager.get_event_log(msg.chat_id)
        if action == "end":
            return await self.game_manager.end_session(msg.chat_id)

        return "❌ Неизвестная команда. Используй /game для списка команд."

    # ========== КОМАНДЫ ==========

    async def handle_summary_command(self, chat_id: int) -> str:
        summary = await self.summarizer.get_latest_summary(chat_id)
        if summary:
            return f"📊 <b>Последняя сводка:</b>\n\n{summary.content}"
        return "📊 Сводка пока не создана. Она появится завтра."

    async def handle_memory_command(self, user_id: int, chat_id: int) -> str:
        items = await self.memory_engine.get_recent_memories(user_id, chat_id, limit=10)
        if not items:
            return "🧠 У меня пока нет воспоминаний."

        by_type: dict[str, list] = {}
        for item in items:
            t = item.type.value
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(item.content)

        lines = ["🧠 <b>Что я помню:</b>"]
        for t, contents in by_type.items():
            lines.append(f"\n📁 <b>{t}:</b>")
            for c in contents[:3]:
                lines.append(f"  • {c}")

        return "\n".join(lines)

    async def handle_profile_command(self, user_id: int, chat_id: int) -> str:
        profile = await self.memory_engine.get_user_profile(user_id, chat_id)
        if not profile:
            return "Профиль ещё не создан. Пообщайся со мной!"

        parts = [f"<b>Профиль пользователя</b>"]
        
        if profile.display_name:
            parts.append(f"Имя: {profile.display_name}")
            
        if profile.traits:
            import json
            try:
                traits = json.loads(profile.traits)
                if traits:
                    parts.append(f"\n<b>Инфо:</b>")
                    for t in traits[:5]:
                        parts.append(f"  • {t}")
            except (json.JSONDecodeError, TypeError):
                pass
                
        if profile.interests:
            import json
            try:
                interests = json.loads(profile.interests)
                if interests:
                    parts.append(f"\n<b>Интересы:</b>")
                    for i in interests[:5]:
                        parts.append(f"  • {i}")
            except (json.JSONDecodeError, TypeError):
                pass
                
        if profile.relationships:
            import json
            try:
                rels = json.loads(profile.relationships)
                if rels:
                    parts.append(f"\n<b>Связи:</b>")
                    for r in rels[:5]:
                        parts.append(f"  • {r}")
            except (json.JSONDecodeError, TypeError):
                pass
                
        if profile.summary:
            parts.append(f"\n<b>Краткое резюме:</b> {profile.summary}")

        return "\n".join(parts)

    async def handle_mode_command(self, chat_id: int, new_mode: str) -> str:
        from src.memory_engine.engine import MemoryEngine
        me = MemoryEngine()
        valid_modes = ["observer", "assistant", "social", "game_master"]
        if new_mode not in valid_modes:
            return f"❌ Неизвестный режим. Доступные: {', '.join(valid_modes)}"

        await me.set_chat_mode(chat_id, new_mode)  # type: ignore[arg-type]
        return f"✅ Режим изменён на: <code>{new_mode}</code>"

    async def get_current_mode(self, chat_id: int) -> str:
        from src.memory_engine.engine import MemoryEngine
        me = MemoryEngine()
        mode = await me.get_chat_mode(chat_id)
        settings_obj = self._get_settings(chat_id)
        return (
            f"🤖 Режим: <code>{mode.value if hasattr(mode, 'value') else mode}</code>\n"
            f"Активность: <code>{settings_obj.activity.value}</code>\n"
            f"Только по имени: <code>{settings_obj.mention_only}</code>"
        )

    async def handle_game_command(self, user_id: int, chat_id: int, args: list[str]) -> str:
        return await self._handle_game_command(
            NormalizedMessage(
                telegram_id=0, chat_id=chat_id, chat_type="private",
                user_id=user_id, username=None, first_name=None,
                text="/game " + " ".join(args), reply_to_message_id=None,
                is_mention_bot=False, is_command=True, command="game",
                command_args=args, language_code=None,
                created_at=datetime.now(timezone.utc),
            )
        )

    async def handle_settings_command(self, chat_id: int) -> str:
        s = self._get_settings(chat_id)
        return (
            f"⚙️ <b>Настройки чата {chat_id}:</b>\n\n"
            f"Режим: <code>{s.mode}</code>\n"
            f"Активность: <code>{s.activity.value}</code>\n"
            f"Только по имени: <code>{s.mention_only}</code>\n"
            f"Модель: <code>{settings.llm_model}</code>\n"
            f"Провайдер: <code>{settings.llm_provider}</code>"
        )

    async def handle_clear_command(self, user_id: int, chat_id: int) -> str:
        """Полная очистка контекста чата."""
        from sqlalchemy import select, delete
        from src.database.session import get_session
        from src.database.models import (
            MemoryItem, MemorySummary, MemoryExtractionBatch,
            UserProfile, ChatVibeProfile, Message, SkillState,
            GameSession, GameEvent, SkillEvent, Summary,
            Reminder,
        )

        deleted = {
            "memory_items": 0,
            "memory_summaries": 0,
            "extraction_batches": 0,
            "user_profiles": 0,
            "vibe_profile": 0,
            "messages": 0,
            "skill_states": 0,
            "game_sessions": 0,
            "game_events": 0,
            "skill_events": 0,
            "summaries": 0,
            "reminders": 0,
        }

        try:
            async for session in get_session():
                # 1. Memory items
                stmt = select(MemoryItem).where(MemoryItem.chat_id == chat_id)
                result = await session.execute(stmt)
                items = list(result.scalars().all())
                deleted["memory_items"] = len(items)
                for item in items:
                    await session.delete(item)

                # 2. Memory summaries
                stmt = select(MemorySummary).where(MemorySummary.chat_id == chat_id)
                result = await session.execute(stmt)
                summaries = list(result.scalars().all())
                deleted["memory_summaries"] = len(summaries)
                for s in summaries:
                    await session.delete(s)

                # 3. Extraction batches
                stmt = select(MemoryExtractionBatch).where(MemoryExtractionBatch.chat_id == chat_id)
                result = await session.execute(stmt)
                batches = list(result.scalars().all())
                deleted["extraction_batches"] = len(batches)
                for b in batches:
                    await session.delete(b)

                # 4. User profiles (только для этого чата)
                stmt = select(UserProfile).where(UserProfile.chat_id == chat_id)
                result = await session.execute(stmt)
                profiles = list(result.scalars().all())
                deleted["user_profiles"] = len(profiles)
                for p in profiles:
                    await session.delete(p)

                # 5. Vibe profile
                stmt = select(ChatVibeProfile).where(ChatVibeProfile.chat_id == chat_id)
                result = await session.execute(stmt)
                vibes = list(result.scalars().all())
                deleted["vibe_profile"] = len(vibes)
                for v in vibes:
                    await session.delete(v)

                # 6. Messages
                stmt = select(Message).where(Message.chat_id == chat_id)
                result = await session.execute(stmt)
                messages = list(result.scalars().all())
                deleted["messages"] = len(messages)
                for m in messages:
                    await session.delete(m)

                # 7. Skill states
                stmt = select(SkillState).where(SkillState.chat_id == chat_id)
                result = await session.execute(stmt)
                skills = list(result.scalars().all())
                deleted["skill_states"] = len(skills)
                for s in skills:
                    await session.delete(s)

                # 8. Game sessions (сначала загружаем, потом их события)
                stmt = select(GameSession).where(GameSession.chat_id == chat_id)
                result = await session.execute(stmt)
                game_sessions = list(result.scalars().all())

                # 9. Game events (удаляем через session_id)
                game_session_ids = [gs.id for gs in game_sessions]
                if game_session_ids:
                    stmt = select(GameEvent).where(GameEvent.session_id.in_(game_session_ids))
                    result = await session.execute(stmt)
                    game_events = list(result.scalars().all())
                    deleted["game_events"] = len(game_events)
                    for ge in game_events:
                        await session.delete(ge)

                # 10. Game sessions
                deleted["game_sessions"] = len(game_sessions)
                for gs in game_sessions:
                    await session.delete(gs)

                # 11. Skill events
                stmt = select(SkillEvent).where(SkillEvent.chat_id == chat_id)
                result = await session.execute(stmt)
                skill_events_list = list(result.scalars().all())
                deleted["skill_events"] = len(skill_events_list)
                for se in skill_events_list:
                    await session.delete(se)

                # 12. Usage stats — НЕ чистим, это глобальная статистика без chat_id
                # (если нужна очистка — отдельная админ-команда)

                # 13. Summaries
                stmt = select(Summary).where(Summary.chat_id == chat_id)
                result = await session.execute(stmt)
                summaries = list(result.scalars().all())
                deleted["summaries"] = len(summaries)
                for su in summaries:
                    await session.delete(su)

                # 13. Reminders (привязаны к чату)
                stmt = select(Reminder).where(Reminder.chat_id == chat_id)
                result = await session.execute(stmt)
                reminders = list(result.scalars().all())
                deleted["reminders"] = len(reminders)
                for r in reminders:
                    await session.delete(r)

                await session.commit()

            total = sum(deleted.values())
            return f"✅ Чат очищен. Удалено: {total} записей\n" \
                   f"  • Память: {deleted.get('memory_items', 0)}\n" \
                   f"  • Саммари: {deleted.get('memory_summaries', 0)}\n" \
                   f"  • Профили: {deleted.get('user_profiles', 0)}\n" \
                   f"  • Сообщения: {deleted.get('messages', 0)}\n" \
                   f"  • Вайб: {deleted.get('vibe_profile', 0)}\n" \
                   f"  • Скиллы: {deleted.get('skill_states', 0)}\n" \
                   f"  • Игры: {deleted.get('game_sessions', 0)} сессий, {deleted.get('game_events', 0)} событий\n" \
                   f"  • События скиллов: {deleted.get('skill_events', 0)}\n" \
                   f"  • Саммари: {deleted.get('summaries', 0)}\n" \
                   f"  • Напоминания: {deleted.get('reminders', 0)}\n\n" \
                   f"Начинаем с чистого листа."

        except Exception as e:
            import structlog
            structlog.get_logger().exception("clear_chat_failed", error=str(e))
            return f"❌ Ошибка при очистке чата: {str(e)}"

    async def handle_model_command(self) -> str:
        return (
            f"🧠 <b>Модель:</b>\n\n"
            f"Провайдер: <code>{settings.llm_provider}</code>\n"
            f"Модель: <code>{settings.llm_model}</code>\n"
            f"Fallback: <code>{settings.llm_fallback_provider}</code>"
        )

    async def handle_budget_command(self) -> str:
        usage = await self.memory_engine.get_today_usage()
        remaining = settings.daily_token_budget - (usage.tokens_prompt + usage.tokens_completion)
        return (
            f"📊 <b>Бюджет токенов:</b>\n\n"
            f"Использовано: {usage.tokens_prompt + usage.tokens_completion}\n"
            f"Осталось: {remaining}\n"
            f"Лимит: {settings.daily_token_budget}\n"
            f"Запросов сегодня: {usage.requests_count}"
        )

    async def handle_silence_command(self, chat_id: int, minutes: int) -> str:
        settings_obj = self._get_settings(chat_id)
        settings_obj.silence(minutes)
        response = bot_personality.get_silence_response()
        return f"{response} ({minutes} мин)"

    async def handle_remind_command(self, user_id: int, chat_id: int, args: str) -> str:
        """Обработка команды /remind."""
        # Пробуем распарсить как натуральное напоминание
        full_text = f"напомни {args}"
        parsed = reminder_manager.parse_natural_reminder(full_text)

        if not parsed:
            return (
                "⏰ Не понял формат времени.\n\n"
                "Примеры:\n"
                "• «напомни через 30 минут что проверить почту»\n"
                "• «напомни завтра в 15:00 что совещание»\n"
                "• «напомни в пятницу что сдать отчёт»"
            )

        remind_at, content, target_user_id = parsed
        reminder = await reminder_manager.create_reminder(
            chat_id=chat_id,
            user_id=user_id,
            content=content,
            remind_at=remind_at,
        )

        time_str = remind_at.strftime("%d.%m.%Y в %H:%M")
        return f"📝 Запомнил! Напомню {time_str}:\n{content}"

    async def handle_reminders_command(self, user_id: int, chat_id: int) -> str:
        """Показать активные напоминания."""
        reminders = await reminder_manager.get_user_reminders(chat_id, user_id)

        if not reminders:
            return "⏰ У тебя нет активных напоминаний."

        lines = ["⏰ <b>Твои напоминания:</b>"]
        for r in reminders:
            time_str = r.remind_at.strftime("%d.%m %H:%M")
            lines.append(f"• {time_str} — {r.content[:50]}")

        return "\n".join(lines)

    # ========== SKILL COMMANDS ==========

    async def handle_skills_list_command(self, chat_id: int) -> str:
        """Show available and active skills."""
        skills = await skill_registry.get_all_skills()
        if not skills:
            return "🧩 Установленных скиллов нет."

        lines = ["🧩 <b>Доступные скиллы:</b>"]
        for skill in skills:
            status = "✅" if skill.is_active else "⏸️"
            lines.append(f"\n{status} <b>{skill.name}</b> ({skill.slug})")
            lines.append(f"  {skill.description[:120]}...")

        # Show active skills for this chat
        active = await skill_router.skill_state_manager.get_all_active_skills(chat_id)
        if active:
            lines.append(f"\n🎮 <b>Активны в этом чате:</b> {', '.join(active)}")

        return "\n".join(lines)

    async def handle_skill_activate_command(self, chat_id: int, skill_slug: str) -> str:
        """Manually activate a skill."""
        slug = skill_slug.lower().strip()
        skill = skill_registry.get_skill(slug)
        if not skill:
            return f"❌ Скилл '{slug}' не найден."

        await skill_router.activate_skill(chat_id, slug)
        return f"✅ Скилл <b>{skill.name}</b> активирован."

    async def handle_skill_deactivate_command(self, chat_id: int, skill_slug: str) -> str:
        """Manually deactivate a skill."""
        slug = skill_slug.lower().strip()
        await skill_router.skill_state_manager.delete_state(slug, chat_id)
        return f"⏹️ Скилл <b>{slug}</b> деактивирован."
