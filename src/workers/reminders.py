"""
Reminder Manager — система напоминаний.

Бот может запоминать напоминания и отправлять их в нужное время.
Поддержка напоминаний конкретному человеку: «Псина, напомни @Васе завтра в 3».
"""

import re
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from src.database.session import get_session
from src.database.models import Reminder
from src.context_tracker.tracker import context_tracker

logger = structlog.get_logger()


class ReminderManager:
    """Управление напоминаниями."""

    async def create_reminder(
        self,
        chat_id: int,
        user_id: int,
        content: str,
        remind_at: datetime,
        target_user_id: int | None = None,
    ) -> Reminder:
        """Создать напоминание."""
        async for session in get_session():
            reminder = Reminder(
                chat_id=chat_id,
                user_id=user_id,
                content=content,
                remind_at=remind_at,
                target_user_id=target_user_id,
            )
            session.add(reminder)
            await session.commit()
            await session.refresh(reminder)

            logger.info(
                "Reminder created",
                reminder_id=reminder.id,
                chat_id=chat_id,
                target_user_id=target_user_id,
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
        """Получить напоминания пользователя (включая те где он target)."""
        async for session in get_session():
            stmt = (
                select(Reminder)
                .where(
                    Reminder.chat_id == chat_id,
                    Reminder.is_sent == False,
                    Reminder.user_id == user_id | (Reminder.target_user_id == user_id),
                )
                .order_by(Reminder.remind_at)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    def parse_target_user(self, text: str, chat_id: int) -> tuple[str, int | None]:
        """
        Извлечь целевого пользователя из текста напоминания.
        «Псина, напомни @username завтра в 3 что встреча»
        Возвращает (очищенный_текст, target_user_id или None).
        """
        # Ищем @username
        at_match = re.search(r'@(\w+)', text)
        if at_match:
            username = at_match.group(1)
            target_id = context_tracker.resolve_name(username)
            if target_id:
                # Убираем @username из текста
                cleaned = text.replace(f'@{username}', '').strip()
                # Убираем лишние "напомни" если осталось
                cleaned = re.sub(r'^напомни\s+', '', cleaned, flags=re.IGNORECASE).strip()
                return cleaned, target_id

        # Ищем имя: «напомни Васе/Васе/Пете/Маше»
        name_match = re.search(
            r'напомни\s+([А-ЯA-Z][а-яa-z]+)(?:у|е|у|а|и)\s+(.+)',
            text,
            re.IGNORECASE,
        )
        if name_match:
            name = name_match.group(1)
            content = name_match.group(2)
            target_id = context_tracker.resolve_name(name)
            if target_id:
                return content, target_id

        return text, None

    def parse_natural_reminder(self, text: str, chat_id: int = 0) -> tuple[datetime, str, int | None] | None:
        """
        Распарсить естественное напоминание.
        Возвращает (время, текст, target_user_id) или None.
        """
        text_lower = text.lower()

        # Сначала ищем целевого пользователя
        cleaned_text, target_user_id = self.parse_target_user(text_lower, chat_id)

        # Убираем обращение к боту
        text_clean = re.sub(
            r'^(псина|пес|пёс|песик|пёсик)[,\s:]+',
            '',
            cleaned_text,
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

            return now + delta, content, target_user_id

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
            return target, content, target_user_id

        # 3. «в HH:MM»
        time_match = re.search(
            r'(?:в\s+)?(\d{1,2}):?(\d{2})?\s+(?:что\s+)?(.+)',
            text_clean,
        )
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            content = time_match.group(3).strip()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target, content, target_user_id

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
                    return target, content, target_user_id

        # 5. Просто текст без времени — через 1 час
        if text_clean:
            return now + timedelta(hours=1), text_clean, target_user_id

        return None


reminder_manager = ReminderManager()
