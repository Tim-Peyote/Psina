"""
Reminder Manager — система напоминаний.

Бот может запоминать напоминания и отправлять их в нужное время.
Примеры:
  «Псина, напомни завтра в 15:00 что совещание»
  «Псина, напомни через 2 часа что позвонить маме»
  «Псина, напомни в пятницу что сдать отчёт»
"""

import re
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from src.database.session import get_session
from src.database.models import Reminder

logger = structlog.get_logger()


class ReminderManager:
    """Управление напоминаниями."""

    async def create_reminder(
        self,
        chat_id: int,
        user_id: int,
        content: str,
        remind_at: datetime,
    ) -> Reminder:
        """Создать напоминание."""
        async for session in get_session():
            reminder = Reminder(
                chat_id=chat_id,
                user_id=user_id,
                content=content,
                remind_at=remind_at,
            )
            session.add(reminder)
            await session.commit()
            await session.refresh(reminder)

            logger.info(
                "Reminder created",
                reminder_id=reminder.id,
                chat_id=chat_id,
                remind_at=remind_at.isoformat(),
            )
            return reminder

    async def get_pending_reminders(self) -> list[Reminder]:
        """Получить все ненапоминанные напоминания, время которых пришло."""
        now = datetime.now(timezone.utc)

        async for session in get_session():
            stmt = (
                select(Reminder)
                .where(
                    Reminder.is_sent == False,
                    Reminder.remind_at <= now,
                )
                .order_by(Reminder.remind_at)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def mark_sent(self, reminder_id: int) -> None:
        """Отметить напоминание как отправленное."""
        async for session in get_session():
            from sqlalchemy import update

            stmt = (
                update(Reminder)
                .where(Reminder.id == reminder_id)
                .values(is_sent=True)
            )
            await session.execute(stmt)
            await session.commit()

    async def get_user_reminders(self, chat_id: int, user_id: int) -> list[Reminder]:
        """Получить напоминания пользователя."""
        async for session in get_session():
            stmt = (
                select(Reminder)
                .where(
                    Reminder.chat_id == chat_id,
                    Reminder.user_id == user_id,
                    Reminder.is_sent == False,
                )
                .order_by(Reminder.remind_at)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    def parse_natural_reminder(self, text: str) -> tuple[datetime, str] | None:
        """
        Распарсить естественное напоминание.
        Возвращает (время, текст) или None.

        Примеры:
          «напомни завтра в 15:00 что совещание»
          «напомни через 2 часа что позвонить»
          «напомни в пятницу что сдать отчёт»
          «напомни через 30 минут что проверить почту»
        """
        text_lower = text.lower()

        # Убираем обращение к боту
        text_clean = re.sub(
            r'^(псина|пес|пёс|песик|пёсик)[,\s:]+',
            '',
            text_lower,
            flags=re.IGNORECASE,
        )

        # Убираем «напомни»
        text_clean = re.sub(r'^напомни\s+', '', text_clean)

        now = datetime.now(timezone.utc)

        # 1. «через N минут/часов/дней»
        delta_match = re.search(
            r'через\s+(\d+)\s+(минут[уы]|час[аов]|дней|день|недел[юи])\s+(?:что\s+)?(.+)',
            text_clean,
        )
        if delta_match:
            num = int(delta_match.group(1))
            unit = delta_match.group(2)
            content = delta_match.group(3).strip()

            if "минут" in unit:
                delta = timedelta(minutes=num)
            elif "час" in unit:
                delta = timedelta(hours=num)
            elif "дн" in unit or unit == "день":
                delta = timedelta(days=num)
            elif "недел" in unit:
                delta = timedelta(weeks=num)
            else:
                delta = timedelta(hours=num)

            return now + delta, content

        # 2. «завтра в HH:MM»
        tomorrow_match = re.search(
            r'завтра\s+(?:в\s+)?(\d{1,2}):?(\d{2})?\s+(?:что\s+)?(.+)',
            text_clean,
        )
        if tomorrow_match:
            hour = int(tomorrow_match.group(1))
            minute = int(tomorrow_match.group(2) or 0)
            content = tomorrow_match.group(3).strip()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)
            return target, content

        # 3. «в HH:MM» — сегодня или завтра если время прошло
        time_match = re.search(
            r'(?:в\s+)?(\d{1,2}):?(\d{2})?\s+(?:что\s+)?(.+)',
            text_clean,
        )
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            content = time_match.group(3).strip()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # Если время уже прошло — завтра
            if target <= now:
                target += timedelta(days=1)
            return target, content

        # 4. «в понедельник/вторник/...»
        weekdays = {
            "понедельник": 0, "пн": 0,
            "вторник": 1, "вт": 1,
            "среда": 2, "ср": 2,
            "четверг": 3, "чт": 3,
            "пятница": 4, "пт": 4,
            "суббота": 5, "сб": 5,
            "воскресенье": 6, "вс": 6,
        }
        for day_name, day_num in weekdays.items():
            if day_name in text_clean:
                # Найти текст напоминания после дня недели
                content_match = re.search(
                    rf'{day_name}\s+(?:в\s+(\d{{1,2}}):?(\d{{2}})?\s+)?(?:что\s+)?(.+)',
                    text_clean,
                )
                if content_match:
                    hour = int(content_match.group(1) or 12)
                    minute = int(content_match.group(2) or 0)
                    content = content_match.group(3).strip()

                    days_ahead = day_num - now.weekday()
                    if days_ahead <= 0:
                        days_ahead += 7

                    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
                    return target, content

        # 5. Просто текст без времени — через 1 час
        if text_clean:
            return now + timedelta(hours=1), text_clean

        return None


reminder_manager = ReminderManager()
