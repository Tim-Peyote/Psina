"""Reminders skill handler — stateless, uses ReminderManager + table.

LLM parses time expressions instead of fragile regex.
No SkillState needed — data lives in the `reminders` table.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

import structlog

from src.message_processor.processor import NormalizedMessage
from src.workers.reminders import reminder_manager
from src.context_tracker.tracker import context_tracker
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()

_INTENT_SYSTEM_TEMPLATE = """Ты — классификатор намерений для системы напоминаний.
Сегодня: {now}.

ДЕЙСТВИЯ:
- create: создать напоминание (фразы: напомни, поставь будильник, через X, не забудь)
- list: список напоминаний (фразы: мои напоминания, покажи напоминания, что запланировано)
- delete: отменить (фразы: отмени напоминание, убери напоминалку, удали)

Для create извлеки:
- content: что напомнить (текст без временных указателей)
- time_expression: временное выражение как есть (например: "через 30 минут", "завтра в 9", "в пятницу в 15:00")
- target_username: @username если напоминание для другого человека, иначе null

Ответь СТРОГО в JSON без markdown:
{{"action": "create|list|delete", "content": "текст", "time_expression": "через 30 минут", "target_username": null}}

Примеры:
- "напомни через 30 минут позвонить" → {{"action": "create", "content": "позвонить", "time_expression": "через 30 минут", "target_username": null}}
- "напомни @Vasya завтра в 9 встреча" → {{"action": "create", "content": "встреча", "time_expression": "завтра в 9", "target_username": "@Vasya"}}
- "покажи мои напоминания" → {{"action": "list", "content": null, "time_expression": null, "target_username": null}}
- "отмени напоминание про звонок" → {{"action": "delete", "content": "звонок", "time_expression": null, "target_username": null}}
"""

_TIME_PARSE_SYSTEM_TEMPLATE = """Ты — парсер времени. Преобразуй временное выражение в ISO 8601 UTC datetime.
Текущее время: {now} (Москва, UTC+3).
Пользователь находится в московском часовом поясе — все его времена в МСК.
При конвертации в UTC вычитай 3 часа (МСК = UTC+3).

Ответь СТРОГО в JSON: {{"datetime": "2026-04-14T12:30:00Z", "human": "в 15:30 МСК"}}
Если не можешь распознать — {{"datetime": null, "human": null}}

Примеры (пользователь в МСК = UTC+3):
- "через 30 минут" от 13:00 МСК → {{"datetime": "2026-04-14T10:30:00Z", "human": "через 30 минут"}}
- "завтра в 9" от 2026-04-14 МСК → {{"datetime": "2026-04-15T06:00:00Z", "human": "завтра в 09:00"}}
- "в 15:00" → {{"datetime": "2026-04-14T12:00:00Z", "human": "в 15:00"}}
- "в пятницу в 15:00" → {{"datetime": "...", "human": "в пятницу в 15:00"}}
"""


async def process_message(
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
) -> str | None:
    """Process reminder operation. Fully stateless."""
    intent = await _classify_intent(msg.text)
    if not intent:
        return None

    action = intent.get("action", "list")

    if action == "create":
        return await _handle_create(intent, chat_id, user_id)
    elif action == "list":
        return await _handle_list(chat_id, user_id)
    elif action == "delete":
        return await _handle_delete(intent, chat_id, user_id)

    return None


async def _classify_intent(text: str) -> dict | None:
    """Use LLM to classify reminder operation."""
    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M МСК (UTC+3)")
    system = _INTENT_SYSTEM_TEMPLATE.format(now=now)
    llm = LLMProvider.get_provider()
    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text[:500]},
            ],
            chat_id=0,
            user_id=0,
        )
        raw = response.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.warning("reminders: intent classification failed", error=str(e))
        return {"action": "list"}


async def _parse_time_expression(expr: str) -> tuple[datetime | None, str | None]:
    """Use LLM to parse natural language time into datetime (stored as UTC)."""
    now_msk = datetime.now(MSK)
    llm = LLMProvider.get_provider()

    system = _TIME_PARSE_SYSTEM_TEMPLATE.format(now=now_msk.strftime("%Y-%m-%d %H:%M"))
    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": expr},
            ],
            chat_id=0,
            user_id=0,
        )
        raw = response.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        dt_str = data.get("datetime")
        human = data.get("human")
        if dt_str:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt, human
    except Exception as e:
        logger.warning("reminders: time parse failed, falling back", error=str(e), expr=expr)

    # Fallback: simple regex — interprets times as MSK, stores as UTC
    return _parse_time_fallback(expr, now_msk)


def _parse_time_fallback(expr: str, now_msk: datetime) -> tuple[datetime | None, str | None]:
    """Simple fallback time parser. Interprets times as Moscow (MSK), returns UTC for storage."""
    expr_lower = expr.lower()

    def to_utc(dt_msk: datetime) -> datetime:
        """Convert MSK datetime to UTC."""
        return dt_msk.astimezone(timezone.utc)

    # через N минут/часов
    delta_match = re.search(r"через\s+(\d+)\s+(минут|час)", expr_lower)
    if delta_match:
        n = int(delta_match.group(1))
        unit = delta_match.group(2)
        delta = timedelta(minutes=n) if "минут" in unit else timedelta(hours=n)
        dt_msk = now_msk + delta
        return to_utc(dt_msk), expr

    # завтра в HH(:MM)?
    tomorrow_match = re.search(r"завтра.*?(\d{1,2})(?::(\d{2}))?", expr_lower)
    if tomorrow_match:
        h = int(tomorrow_match.group(1))
        m = int(tomorrow_match.group(2) or 0)
        dt_msk = (now_msk + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
        return to_utc(dt_msk), f"завтра в {h:02d}:{m:02d}"

    # в HH:MM
    time_match = re.search(r"в\s+(\d{1,2}):(\d{2})", expr_lower)
    if time_match:
        h = int(time_match.group(1))
        m = int(time_match.group(2))
        dt_msk = now_msk.replace(hour=h, minute=m, second=0, microsecond=0)
        if dt_msk <= now_msk:
            dt_msk += timedelta(days=1)
        return to_utc(dt_msk), f"в {h:02d}:{m:02d}"

    # Cannot parse — return None so caller can ask user to clarify
    return None, None


async def _handle_create(intent: dict, chat_id: int, user_id: int) -> str:
    content = (intent.get("content") or "").strip()
    time_expr = (intent.get("time_expression") or "").strip()
    target_username = intent.get("target_username")

    if not content:
        return "❓ Что именно напомнить? Уточни текст напоминания."

    if not time_expr:
        return "❓ Когда напомнить? Укажи время (например: «через 30 минут», «завтра в 9»)."

    remind_at, human_time = await _parse_time_expression(time_expr)
    if not remind_at:
        return f"❓ Не могу распознать время: «{time_expr}». Попробуй: «через 30 минут» или «завтра в 9»."

    # Resolve target user
    target_user_id = None
    if target_username:
        username = target_username.lstrip("@")
        target_user_id = context_tracker.resolve_name(username, chat_id)

    await reminder_manager.create_reminder(
        chat_id=chat_id,
        user_id=user_id,
        content=content,
        remind_at=remind_at,
        target_user_id=target_user_id,
    )

    time_display = remind_at.astimezone(MSK).strftime("%H:%M %d.%m")
    target_line = f"\n👤 Для: @{target_username.lstrip('@')}" if target_username else ""
    return f"⏰ Напомню: <b>{content}</b>\n🕐 <b>{time_display} МСК</b>{target_line}"


async def _handle_list(chat_id: int, user_id: int) -> str:
    reminders = await reminder_manager.get_user_reminders(chat_id, user_id)
    if not reminders:
        return "📭 Активных напоминаний нет."

    lines = [f"🔔 <b>Твои напоминания:</b>\n"]
    for i, r in enumerate(reminders, 1):
        time_str = r.remind_at.astimezone(MSK).strftime("%H:%M %d.%m") if r.remind_at else "—"
        lines.append(f"{i}. <b>{r.content}</b> — {time_str}")

    return "\n".join(lines)


async def _handle_delete(intent: dict, chat_id: int, user_id: int) -> str:
    query = (intent.get("content") or "").lower().strip()
    if not query:
        return "❓ Какое напоминание отменить? Уточни текст."

    reminders = await reminder_manager.get_user_reminders(chat_id, user_id)
    target = next((r for r in reminders if query in r.content.lower()), None)

    if not target:
        return f"❓ Напоминание «{query}» не найдено."

    await reminder_manager.mark_sent(target.id)
    return f"✅ Отменено: <b>{target.content}</b>"
