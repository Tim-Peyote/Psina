"""
Telegram handlers — обработка команд и сообщений.

Все текстовые сообщения идут через orchestrator.process_message(),
который сам решает — отвечать или нет.
"""

import structlog

from aiogram import F, Router as AiogramRouter
from aiogram.types import Message, ChatMemberUpdated
from aiogram.filters import Command, CommandStart, CommandObject, IS_NOT_MEMBER, IS_MEMBER
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter

from src.config import settings
from src.message_processor.processor import NormalizedMessage
from src.orchestration_engine.orchestrator import Orchestrator
from src.orchestration_engine.session_manager import session_manager
from src.orchestration_engine.censorship_manager import censorship_manager, CensorshipLevel
from src.orchestration_engine.vibe_adapter import vibe_adapter
from src.orchestration_engine.message_router import message_router
from src.orchestration_engine.reaction_engine import reaction_engine
from src.web_search_engine.processor import search_processor

logger = structlog.get_logger()

router = AiogramRouter()
orchestrator = Orchestrator()


async def _reply(message: Message, text: str, reply_to: int | None = None) -> None:
    """Отправить ответ и зарегистрировать ID для reply detection."""
    sent = await message.answer(text, reply_to_message_id=reply_to)
    message_router.register_bot_message(sent.message_id)


async def _try_react(message: Message, bot, normalized: "NormalizedMessage") -> None:
    """Попытаться поставить реакцию на сообщение."""
    from aiogram.types import ReactAction
    emoji = reaction_engine.should_react(normalized)
    if emoji:
        try:
            await bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactAction(emoji=emoji)],
            )
            logger.debug("Reaction set", emoji=emoji, message_id=message.message_id)
        except Exception:
            logger.debug("Failed to set reaction", emoji=emoji, exc_info=True)


def _normalize_message(msg: Message) -> NormalizedMessage:
    """Нормализовать aiogram Message в NormalizedMessage."""
    text = msg.text or msg.caption or ""
    chat_type = msg.chat.type.value if msg.chat.type else "private"

    is_command = text.startswith("/")
    command = None
    command_args: list[str] = []

    if is_command:
        parts = text[1:].split(" ", 1)
        command = parts[0].split("@")[0]  # игнорируем @botname
        if len(parts) > 1:
            command_args = parts[1].split()

    # Больше НЕ определяем is_mention_bot тут — это делает trigger_system
    is_mention_bot = False

    return NormalizedMessage(
        telegram_id=msg.message_id,
        chat_id=msg.chat.id,
        chat_type=chat_type,
        user_id=msg.from_user.id if msg.from_user else 0,
        username=msg.from_user.username if msg.from_user else None,
        first_name=msg.from_user.first_name if msg.from_user else None,
        text=text.strip(),
        reply_to_message_id=msg.reply_to_message_id,
        is_mention_bot=is_mention_bot,  # больше не используется тут
        is_command=is_command,
        command=command,
        command_args=command_args,
        language_code=msg.from_user.language_code if msg.from_user else None,
        created_at=msg.date,
    )


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Команда /start."""
    logger.info("User started bot", user_id=message.from_user.id if message.from_user else None)
    await _reply(message,
        f"🐾 Привет! Я <b>{settings.bot_name}</b>.\n\n"
        f"Я — живой участник чата. Слушаю, запоминаю, отвечаю когда нужно.\n\n"
        f"Позови меня по имени — и я приду. А без дела не лезу 🤫\n\n"
        f"Используй /help, чтобы узнать что я умею."
    )


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Команда /help."""
    help_text = (
        f"📖 <b>Команды {settings.bot_name}:</b>\n\n"
        "/help — показать это сообщение\n"
        "/summary — дневная сводка\n"
        "/memory — что я помню\n"
        "/profile — твой профиль\n"
        "/mode — текущий режим бота\n"
        "/game — игровые команды\n"
        "/settings — настройки чата\n"
        "/model — информация о модели\n"
        "/budget — использование токенов\n"
        "/silence [minutes] — замолчать на N минут\n"
        "/remind [текст] — создать напоминание\n"
        "/reminders — список напоминаний\n"
        "/search [запрос] — поиск в интернете\n"
        "/censorship [strict|moderate|free] — уровень цензуры\n"
        "/vibe — текущий вайб чата\n\n"
        f"💡 <b>Совет:</b> Просто позови по имени — «{settings.bot_name}, ...» "
        f"или «{settings.bot_aliases[0]}, ...»\n\n"
        f"🗣️ <b>Речью:</b> «заткнись», «будь поактивнее», «сбавь», "
        f"«убери цензуру», «пофильтруй», «напомни завтра в 15 что встреча»\n"
        f"🔍 <b>Поиск:</b> «какая погода в Москве», «кто выиграл матч», «курс биткоина»"
    )
    await _reply(message, help_text)


@router.message(Command("summary"))
async def handle_summary(message: Message) -> None:
    """Команда /summary."""
    result = await orchestrator.handle_summary_command(message.chat.id)
    await _reply(message, result)


@router.message(Command("memory"))
async def handle_memory(message: Message) -> None:
    """Команда /memory."""
    user_id = message.from_user.id if message.from_user else 0
    result = await orchestrator.handle_memory_command(user_id, message.chat.id)
    await _reply(message, result)


@router.message(Command("profile"))
async def handle_profile(message: Message) -> None:
    """Команда /profile."""
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id
    result = await orchestrator.handle_profile_command(user_id, chat_id)
    await _reply(message, result)


@router.message(Command("mode"))
async def handle_mode(message: Message, command: CommandObject) -> None:
    """Команда /mode."""
    chat_id = message.chat.id
    if command.args and command.args.strip():
        new_mode = command.args.strip().lower()
        result = await orchestrator.handle_mode_command(chat_id, new_mode)
    else:
        result = await orchestrator.get_current_mode(chat_id)
    await _reply(message, result)


@router.message(Command("game"))
async def handle_game(message: Message, command: CommandObject) -> None:
    """Команда /game."""
    user_id = message.from_user.id if message.from_user else 0
    args = (command.args or "").strip().split()
    result = await orchestrator.handle_game_command(user_id, message.chat.id, args)
    await _reply(message, result)


@router.message(Command("settings"))
async def handle_settings(message: Message) -> None:
    """Команда /settings."""
    chat_id = message.chat.id
    result = await orchestrator.handle_settings_command(chat_id)
    await _reply(message, result)


@router.message(Command("model"))
async def handle_model_info(message: Message) -> None:
    """Команда /model."""
    result = await orchestrator.handle_model_command()
    await _reply(message, result)


@router.message(Command("budget"))
async def handle_budget(message: Message) -> None:
    """Команда /budget."""
    result = await orchestrator.handle_budget_command()
    await _reply(message, result)


@router.message(Command("silence"))
async def handle_silence(message: Message, command: CommandObject) -> None:
    """Команда /silence."""
    chat_id = message.chat.id
    minutes = 60
    if command.args and command.args.strip():
        try:
            minutes = int(command.args.strip())
        except ValueError:
            pass
    result = await orchestrator.handle_silence_command(chat_id, minutes)
    await _reply(message, result)


@router.message(Command("remind"))
async def handle_remind(message: Message, command: CommandObject) -> None:
    """Команда /remind — создать напоминание."""
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id
    args = (command.args or "").strip()

    if not args:
        await _reply(message,
            "⏰ <b>Напоминания:</b>\n\n"
            "Напиши мне: «Псина, напомни [когда] [что]»\n\n"
            "Примеры:\n"
            "• «напомни через 30 минут что проверить почту»\n"
            "• «напомни завтра в 15:00 что совещание»\n"
            "• «напомни в пятницу что сдать отчёт»\n"
            "• «напомни через 2 часа что позвонить маме»\n\n"
            "Или используй: /remind 2026-04-07 15:00 текст"
        )
        return

    result = await orchestrator.handle_remind_command(user_id, chat_id, args)
    await _reply(message, result)


@router.message(Command("reminders"))
async def handle_reminders(message: Message) -> None:
    """Команда /reminders — список напоминаний."""
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id
    result = await orchestrator.handle_reminders_command(user_id, chat_id)
    await _reply(message, result)


@router.message(Command("censorship"))
async def handle_censorship(message: Message, command: CommandObject) -> None:
    """Команда /censorship — уровень цензуры."""
    chat_id = message.chat.id
    args = (command.args or "").strip().lower()

    if args in ("strict", "moderate", "free"):
        level = CensorshipLevel(args)
        censorship_manager.set_level(chat_id, level)
        texts = {
            "strict": "🔒 Цензура: СТРОГАЯ. Буду аккуратнее.",
            "moderate": "⚖️ Цензура: УМЕРЕННАЯ. Без грубого мата.",
            "free": "🔓 Цензура: СВОБОДНАЯ. Без фильтров.",
        }
        await _reply(message, texts[args])
    else:
        current = censorship_manager.get_level(chat_id).value
        await _reply(message,
            f"⚖️ <b>Цензура:</b>\n\n"
            f"Текущий: <code>{current}</code>\n\n"
            f"Уровни:\n"
            f"• strict — без мата и грубостей\n"
            f"• moderate — лёгкие выражения ок\n"
            f"• free — без ограничений\n\n"
            f"Или скажи речью: «убери цензуру» / «пофильтруй»"
        )


@router.message(Command("vibe"))
async def handle_vibe(message: Message) -> None:
    """Команда /vibe — показать вайб чата."""
    chat_id = message.chat.id
    profile = vibe_adapter.get_profile(chat_id)

    formality_text = "Формальный" if profile.is_formal else "Неформальный"
    mate_text = "Есть мат" if profile.has_mate else "Без мата"
    emoji_text = "Много эмодзи" if profile.is_emoji_heavy else "Мало эмодзи"

    await _reply(message,
        f"🎭 <b>Вайб чата:</b>\n\n"
        f"Стиль: {formality_text}\n"
        f"Мат: {mate_text}\n"
        f"Эмодзи: {emoji_text}\n"
        f"Настроение: {profile.mood}\n"
        f"Сообщений проанализировано: {profile.messages_analyzed}\n\n"
        f"Псина подстраивается под этот стиль автоматически."
    )


@router.message(Command("search"))
async def handle_search(message: Message, command: CommandObject) -> None:
    """Команда /search — ручной поиск в интернете."""
    query = (command.args or "").strip()

    if not query:
        await _reply(message,
            "🔍 <b>Поиск в интернете:</b>\n\n"
            f"Просто спроси меня — «{settings.bot_name}, какая погода в Москве?»\n"
            f"Или используй: /search запрос\n\n"
            f"Примеры:\n"
            f"• «кто выиграл матч Барсы»\n"
            f"• «курс биткоина»\n"
            f"• «что случилось с ...»"
        )
        return

    result = await search_processor.search_and_answer(query)
    await _reply(message, result)


@router.message(F.text)
async def handle_message(message: Message) -> None:
    """Все текстовые сообщения → через orchestrator."""
    normalized = _normalize_message(message)
    logger.debug(
        "Received message",
        user_id=normalized.user_id,
        chat_id=normalized.chat_id,
        is_command=normalized.is_command,
        is_group=normalized.is_group,
        text=normalized.text[:100],
    )

    # Реакция на сообщение пользователя
    bot = message.bot
    if bot:
        await _try_react(message, bot, normalized)

    response = await orchestrator.process_message(normalized)
    if response:
        # Отвечаем reply на сообщение пользователя
        await _reply(message, response, reply_to=message.message_id)


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_chat(event: ChatMemberUpdated) -> None:
    """Бота добавили в чат/группу."""
    chat = event.chat
    logger.info("Bot added to chat", chat_id=chat.id, chat_type=chat.type)

    welcome_text = (
        f"🐾 Привет! Я <b>{settings.bot_name}</b>!\n\n"
        f"Я — живой участник чата. Слушаю, запоминаю, отвечаю когда позовут.\n\n"
        f"<b>Что умею:</b>\n"
        f"• Запоминаю кто что любит, где работает, чем увлекается\n"
        f"• Могу напомнить о событии — просто скажи «напомни [когда] [что]»\n"
        f"• Делаю дневные сводки важных событий\n"
        f"• Играю в настолки (DnD и другие)\n"
        f"• Понимаю естественные команды: «заткнись», «будь активнее», «сбавь»\n\n"
        f"<b>Как со мной общаться:</b>\n"
        f"• Позови: «{settings.bot_name}, ...» или «{settings.bot_aliases[0]}, ...»\n"
        f"• Или просто ответь на моё сообщение\n"
        f"• Без обращения — не лезу, уважаю чужие разговоры\n\n"
        f"Используй /help для списка команд 🐕"
    )

    try:
        await event.bot.send_message(chat.id, welcome_text, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send welcome message")


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed_from_chat(event: ChatMemberUpdated) -> None:
    """Бота удалили из чата/группы."""
    chat_id = event.chat.id
    logger.info("Bot removed from chat", chat_id=chat_id)

    # Закрываем все активные сессии
    sessions = session_manager.get_active_sessions(chat_id)
    for session in sessions:
        session_manager.close_session(chat_id, session.user_id)

    # Память НЕ удаляем — на случай если бота вернут
    # Но помечаем что чат неактивен
