"""Notes skill handler — stateless CRUD for chat notes.

Each operation is independent. State stored in SkillState JSONB,
isolated per chat_id. No persistent session (is_active not set to True).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import structlog

from src.message_processor.processor import NormalizedMessage
from src.skill_system.state_manager import skill_state_manager
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()

_INTENT_SYSTEM = """Ты — классификатор намерений для системы заметок.
Определи действие и параметры из сообщения пользователя.

ДЕЙСТВИЯ:
- add: создать новую заметку (фразы: запомни, запиши, сохрани, создай заметку)
- list: показать все заметки (фразы: покажи заметки, список записей, что записано)
- search: найти заметку (фразы: найди запись, есть ли заметка, что знаем про)
- delete: удалить заметку (фразы: удали заметку, убери запись)
- update: изменить заметку (фразы: обнови, измени заметку)

Ответь СТРОГО в JSON без markdown:
{"action": "add|list|search|delete|update", "title": "краткий заголовок или null", "content": "полный текст для сохранения или null", "query": "поисковый запрос или null", "tags": ["тег1"]}

Примеры:
- "запомни что завтра встреча в 15:00" → {"action": "add", "title": "Встреча завтра", "content": "Встреча в 15:00", "query": null, "tags": ["встречи"]}
- "покажи мои заметки" → {"action": "list", "title": null, "content": null, "query": null, "tags": []}
- "найди заметку про встречу" → {"action": "search", "title": null, "content": null, "query": "встреча", "tags": []}
- "удали заметку про встречу" → {"action": "delete", "title": null, "content": null, "query": "встреча", "tags": []}
"""


async def process_message(
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
) -> str | None:
    """Process notes operation. Stateless — does not keep is_active=True."""
    state = await skill_state_manager.get_state("agent_notes", chat_id, default={"notes": []})
    if not isinstance(state, dict):
        state = {"notes": []}
    state.setdefault("notes", [])

    # Classify intent via LLM
    intent = await _classify_intent(msg.text)
    if not intent:
        return None

    action = intent.get("action", "list")

    if action == "add":
        response = _handle_add(state, intent, user_id)
    elif action == "list":
        response = _handle_list(state)
    elif action == "search":
        response = _handle_search(state, intent)
    elif action == "delete":
        response = _handle_delete(state, intent)
    elif action == "update":
        response = _handle_update(state, intent)
    else:
        response = _handle_list(state)

    # Save state — but do NOT mark skill as active (stateless)
    await skill_state_manager.set_state("agent_notes", chat_id, state)

    return response


async def _classify_intent(text: str) -> dict | None:
    """Use LLM to classify the note operation and extract parameters."""
    llm = LLMProvider.get_provider()
    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": _INTENT_SYSTEM},
                {"role": "user", "content": text[:500]},
            ],
            chat_id=0,
            user_id=0,
        )
        # Strip markdown fences if any
        raw = response.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.warning("notes: intent classification failed", error=str(e))
        return {"action": "list"}


def _handle_add(state: dict, intent: dict, user_id: int) -> str:
    content = (intent.get("content") or "").strip()
    title = (intent.get("title") or "").strip()
    tags = intent.get("tags") or []

    if not content:
        return "❓ Что именно записать? Уточни содержимое."

    if not title:
        # Auto-generate title from first 50 chars
        title = content[:50].rstrip() + ("…" if len(content) > 50 else "")

    note = {
        "id": str(uuid.uuid4())[:8],
        "title": title,
        "content": content,
        "tags": tags,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    state["notes"].append(note)

    tags_line = f"\n🏷 {', '.join(tags)}" if tags else ""
    return f"✅ Записал: <b>{title}</b>{tags_line}"


def _handle_list(state: dict) -> str:
    notes = state.get("notes", [])
    if not notes:
        return "📭 Заметок пока нет. Скажи «запомни...» чтобы создать первую."

    lines = [f"📒 <b>Заметки чата</b> ({len(notes)} записей)\n"]
    for i, note in enumerate(notes, 1):
        preview = note.get("content", "")[:100]
        if len(note.get("content", "")) > 100:
            preview += "…"
        tags = note.get("tags", [])
        tag_line = f"\n   🏷 {', '.join(tags)}" if tags else ""
        lines.append(f"{i}. <b>{note['title']}</b>\n   {preview}{tag_line}")

    return "\n\n".join(lines)


def _handle_search(state: dict, intent: dict) -> str:
    query = (intent.get("query") or "").lower().strip()
    if not query:
        return _handle_list(state)

    notes = state.get("notes", [])
    matches = [
        n for n in notes
        if query in n.get("title", "").lower()
        or query in n.get("content", "").lower()
        or any(query in t.lower() for t in n.get("tags", []))
    ]

    if not matches:
        return f"🔍 По запросу «{query}» ничего не найдено."

    lines = [f"🔍 Найдено ({len(matches)}):\n"]
    for note in matches:
        preview = note.get("content", "")[:150]
        if len(note.get("content", "")) > 150:
            preview += "…"
        lines.append(f"<b>{note['title']}</b>\n{preview}")

    return "\n\n".join(lines)


def _handle_delete(state: dict, intent: dict) -> str:
    query = (intent.get("query") or intent.get("title") or "").lower().strip()
    if not query:
        return "❓ Какую заметку удалить? Уточни название или ключевое слово."

    notes = state.get("notes", [])
    to_delete = next(
        (n for n in notes if query in n.get("title", "").lower() or query in n.get("content", "").lower()),
        None,
    )

    if not to_delete:
        return f"❓ Заметка «{query}» не найдена."

    state["notes"] = [n for n in notes if n["id"] != to_delete["id"]]
    return f"🗑 Удалено: <b>{to_delete['title']}</b>"


def _handle_update(state: dict, intent: dict) -> str:
    query = (intent.get("query") or intent.get("title") or "").lower().strip()
    new_content = (intent.get("content") or "").strip()

    if not query:
        return "❓ Какую заметку обновить?"
    if not new_content:
        return "❓ Укажи новое содержимое заметки."

    notes = state.get("notes", [])
    target = next(
        (n for n in notes if query in n.get("title", "").lower() or query in n.get("content", "").lower()),
        None,
    )

    if not target:
        return f"❓ Заметка «{query}» не найдена."

    target["content"] = new_content
    target["updated_at"] = datetime.now(timezone.utc).isoformat()
    return f"✏️ Обновлено: <b>{target['title']}</b>"
