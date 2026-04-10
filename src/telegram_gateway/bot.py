import structlog

from aiohttp import ClientTimeout
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from src.config import settings

logger = structlog.get_logger()


class BotManager:
    """Manages the Telegram bot lifecycle."""

    def __init__(self) -> None:
        self.bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=AiohttpSession(timeout=ClientTimeout(total=60)),
        )
        self.dp = Dispatcher()
        logger.info("BotManager initialized", bot_name=settings.bot_name)

    async def start_polling(self) -> None:
        """Start polling loop."""
        logger.info("Starting bot polling")
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Clean shutdown."""
        logger.info("Shutting down bot")
        await self.bot.session.close()

    def get_bot(self) -> Bot:
        return self.bot

    def get_dispatcher(self) -> Dispatcher:
        return self.dp
