"""RPG Skill handler — multi-session Game Master.

v2: Supports multiple parallel campaigns per chat, player lists,
LLM-based session management (no hardcoded commands), enriched Session Zero.

State structure (SkillState.state_json):
{
  "active_session_id": "uuid4" | null,
  "sessions": {
    "uuid4": {
      "name": "Campaign name",
      "phase": "session_zero|playing|paused|ended",
      "step": 0-5,
      "system": "d20|pbta|d100|freeform",
      "created_by": user_id,
      "players": [user_id, ...],
      "world": {...},
      "characters": {str(user_id): {...}},
      "npcs": {},
      "combat": null,
      "journal": [],
      "loot_table": []
    }
  }
}
"""

from __future__ import annotations

import json
import uuid

import structlog

from src.message_processor.processor import NormalizedMessage
from src.skill_system.state_manager import skill_state_manager
from src.skill_system.registry import skill_registry
from src.skills.agent_rpg.dice import roll as dice_roll
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()

# =====================================================================
# DEFAULT STATE TEMPLATES
# =====================================================================

def _new_session(name: str, created_by: int) -> dict:
    return {
        "name": name,
        "phase": "session_zero",
        "step": 0,
        "system": "",
        "created_by": created_by,
        "players": [created_by],
        "world": {
            "location": "Начальная локация",
            "time": "08:00",
            "weather": "Ясно",
            "setting": "",
            "tone": "",
            "hook": "",
            "factions": [],
            "flags": {},
            "clocks": {},
            "atmosphere": "",
            "initial_scene": "",
        },
        "characters": {},
        "npcs": {},
        "combat": None,
        "journal": [],
        "loot_table": [],
    }


def _migrate_state(state: dict) -> dict:
    """Migrate old flat state to new multi-session format."""
    if "sessions" in state:
        return state  # Already new format

    # Old format: flat dict with phase/step/world/characters
    if "phase" in state or "world" in state:
        logger.info("rpg: migrating old flat state to multi-session format")
        old_session = {
            "name": "Старая кампания",
            "phase": state.get("phase", "session_zero"),
            "step": state.get("step", 0),
            "system": state.get("system", ""),
            "created_by": 0,
            "players": list(state.get("characters", {}).keys()),
            "world": state.get("world", {}),
            "characters": {str(k): v for k, v in state.get("characters", {}).items()},
            "npcs": state.get("npcs", {}),
            "combat": state.get("combat"),
            "journal": state.get("journal", []),
            "loot_table": state.get("loot_table", []),
        }
        # Convert player ids back to int
        old_session["players"] = [
            int(uid) for uid in old_session["players"] if str(uid).lstrip("-").isdigit()
        ]
        session_id = str(uuid.uuid4())[:8]
        return {
            "active_session_id": session_id,
            "sessions": {session_id: old_session},
        }

    return {"active_session_id": None, "sessions": {}}


def _get_active_session(state: dict) -> dict | None:
    sid = state.get("active_session_id")
    if not sid:
        return None
    return state.get("sessions", {}).get(sid)


def _find_incomplete_session(state: dict) -> tuple[str, dict] | None:
    """Find the most recent incomplete (session_zero or playing) session.

    Returns (session_id, session_dict) or None if all sessions are ended.
    """
    sessions = state.get("sessions", {})
    for sid, s in sessions.items():
        phase = s.get("phase", "")
        if phase in ("session_zero", "playing"):
            return sid, s
    return None


# =====================================================================
# MANAGEMENT INTENT CLASSIFICATION
# =====================================================================

_MGMT_SYSTEM = """Ты — классификатор намерений для менеджера RPG-сессий.
Определи что хочет пользователь.

ДЕЙСТВИЯ:
- new_session: начать новую игру (фразы: начни игру, давай в DnD, хочу сыграть, начнём кампанию)
- resume: продолжить активную игру (фразы: продолжи, где мы остановились, продолжаем)
- switch_session: переключиться на другую игру (фразы: переключись на, перейди к игре X)
- list_sessions: список всех игр (фразы: покажи игры, список кампаний, какие игры)
- add_player: добавить игрока (фразы: добавь @user, хочу играть, я тоже хочу)
- remove_player: убрать игрока (фразы: убери меня, выхожу из игры, не хочу играть)
- pause: поставить на паузу (фразы: пауза, перерыв, pause)
- end_session: завершить игру (фразы: заверши игру, конец кампании, стоп игра)
- delete_session: удалить кампанию (фразы: удали игру X, стереть кампанию)
- game_action: действие в игре (игровые действия, ответы на вопросы ГМ, команды !roll)
- chatter: обычный разговор не связанный с игрой

Ответь СТРОГО в JSON без markdown:
{"action": "new_session|resume|switch_session|list_sessions|add_player|remove_player|pause|end_session|delete_session|game_action|chatter", "target_name": "название игры или null", "target_username": "@user или null"}
"""


async def _classify_mgmt_intent(
    text: str,
    has_active: bool,
    in_players: bool,
    incomplete_session: dict | None = None,
) -> dict:
    """Classify what user wants to do with RPG sessions."""
    llm = LLMProvider.get_provider()
    context_lines = [
        f"Активная игра: {'да' if has_active else 'нет'}.",
        f"Пользователь {'игрок' if in_players else 'не игрок'}.",
    ]
    if incomplete_session:
        step = incomplete_session.get("step", 0)
        name = incomplete_session.get("name", "?")
        phase = incomplete_session.get("phase", "?")
        context_lines.append(
            f"Есть незаконченная сессия «{name}» (фаза: {phase}, шаг: {step}). "
            f"Если пользователь хочет начать — это может быть resume или new_session."
        )
    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": _MGMT_SYSTEM},
                {"role": "user", "content": "\n".join(context_lines) + f"\n\nСообщение: {text[:300]}"},
            ],
            chat_id=0,
            user_id=0,
        )
        raw = response.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(raw)
    except Exception as e:
        logger.debug("rpg: mgmt intent classification failed, defaulting", error=str(e))
        if has_active and in_players:
            return {"action": "game_action", "target_name": None, "target_username": None}
        return {"action": "new_session", "target_name": None, "target_username": None}


# =====================================================================
# MAIN ENTRY POINT
# =====================================================================

async def process_message(
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
) -> str | None:
    """Process a message through the RPG skill."""
    try:
        skill = await skill_registry.activate_skill_by_slug("agent_rpg")
        if not skill:
            return "⚠️ Скилл не найден."
        system_prompt = skill.full_content
    except Exception as e:
        logger.error("rpg: skill activation failed", error=str(e))
        return None

    # Load and migrate state under lock (prevents lost-update from concurrent messages)
    async with skill_state_manager.lock("agent_rpg", chat_id):
        return await _process_message_locked(msg, chat_id, user_id, system_prompt)


async def _process_message_locked(
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
    system_prompt: str,
) -> str | None:
    """Inner handler — called while holding the per-chat state lock."""
    raw_state = await skill_state_manager.get_state("agent_rpg", chat_id, default={})
    raw_state = raw_state if isinstance(raw_state, dict) else {}
    state = _migrate_state(raw_state)

    active_session = _get_active_session(state)
    is_player = active_session is not None and user_id in active_session.get("players", [])

    text = msg.text.strip()

    # =================================================================
    # ACTIVE SESSION: player is in the game
    # =================================================================
    if active_session and is_player:
        phase = active_session.get("phase", "session_zero")

        # Fast-path: explicit game commands (no LLM classification needed)
        if _is_game_command(text):
            response = await _execute_game_command(text, active_session, user_id, system_prompt)
            await _save(state, chat_id, phase, msg.text)
            return response

        # Session Zero: LLM classifies each message with full context
        if phase == "session_zero":
            world_info = active_session.get("world", {}).get("setting", "")
            sz_class = await _classify_sz_message(text, active_session.get("step", 1), world_info)

            if sz_class == "restart":
                # Reset Session Zero from scratch
                active_session["step"] = 0
                active_session["phase"] = "session_zero"
                active_session["world"] = _new_session(active_session.get("name", "Новая кампания"), user_id)["world"]
                active_session.setdefault("characters", {}).clear()
                active_session["npcs"] = {}
                active_session.setdefault("journal", []).append({"summary": "🔄 Сессия Ноль перезапущена"})
                session.pop("_sz_chatter_count", None)
                response = (
                    "🔄 <b>Сессия Ноль перезапущена</b>\n\n"
                    "Начинаем с чистого листа.\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "<b>Шаг 1/5: Мир и Премьера</b>\n\n"
                    "В каком мире происходит наша история? "
                    "Это существующая вселенная (Cyberpunk 2077, DnD, Мир Тьмы) "
                    "или что-то своё?\n\n"
                    "И что является <b>крючком</b> — событием, которое толкает героя в приключение?"
                )
            elif sz_class == "pause":
                active_session["phase"] = "paused"
                response = "⏸️ Настройка на паузе. Напиши «продолжи» когда будешь готов."
            elif sz_class == "end":
                active_session["phase"] = "ended"
                state["active_session_id"] = None
                response = "🏁 Настройка отменена. Скажи «начни новую игру» когда захочешь."
            elif sz_class == "chatter":
                # Not about setup — ask for clarification
                chatter_count = session.get("_sz_chatter_count", 0)
                chatter_count += 1
                session["_sz_chatter_count"] = chatter_count

                if chatter_count >= 2:
                    reask = _STEP_PROMPTS.get(active_session.get("step", 1), "Давай вернёмся к настройке.")
                    session["_sz_chatter_count"] = 0
                    response = f"Не совсем понимаю. Это ответ на мой вопрос или ты про другое?\n\n{reask}"
                else:
                    reask = _STEP_PROMPTS.get(active_session.get("step", 1), "Расскажи подробнее.")
                    response = f"Не совсем понял — это про мир и историю?\n\n{reask}"
            else:
                # answer — accept the response and move forward
                session.pop("_sz_chatter_count", None)
                response = await _handle_session_zero(msg, active_session, chat_id, user_id, system_prompt)

        elif phase == "playing":
            # During playing, classify between management and gameplay
            intent = await _classify_mgmt_intent(text, has_active=True, in_players=True)
            action = intent.get("action", "game_action")
            if action == "game_action":
                response = await _handle_playing(msg, active_session, user_id, system_prompt)
            else:
                response = await _handle_management(action, intent, state, active_session, user_id, chat_id, msg.text)

        elif phase == "paused":
            response = _handle_paused_message(active_session)
        else:
            response = "🏁 Эта кампания завершена. Начни новую или переключись на другую."

    # =================================================================
    # ACTIVE SESSION: user is NOT a player yet
    # =================================================================
    elif active_session and not is_player:
        intent = await _classify_mgmt_intent(msg.text, has_active=True, in_players=False)
        action = intent.get("action", "chatter")
        if action == "add_player":
            response = _add_player_to_session(active_session, user_id)
        elif action in ("new_session", "chatter", "game_action"):
            # Not a player, not trying to join → pass through to normal pipeline
            await _save(state, chat_id, active_session.get("phase", "idle"), msg.text)
            return None
        else:
            response = await _handle_management(action, intent, state, active_session, user_id, chat_id, msg.text)

    # =================================================================
    # NO ACTIVE SESSION
    # =================================================================
    else:
        incomplete = _find_incomplete_session(state)
        intent = await _classify_mgmt_intent(msg.text, has_active=False, in_players=False, incomplete_session=incomplete)
        action = intent.get("action", "new_session")

        if action in ("chatter", "game_action"):
            # Not game-related, let normal pipeline handle
            await _save(state, chat_id, "idle", msg.text)
            return None
        elif action == "list_sessions":
            response = _list_sessions(state)
        elif action == "switch_session":
            response = _switch_session(state, intent.get("target_name", ""), user_id)
        elif action == "new_session" or action == "resume":
            # Check for incomplete sessions — offer to continue
            incomplete = _find_incomplete_session(state)
            if incomplete and action == "new_session":
                sid, s = incomplete
                step = s.get("step", 0)
                name = s.get("name", "кампания")
                response = (
                    f"⏳ У тебя есть незаконченная <b>«{name}»</b> (Шаг {step}/5).\n\n"
                    f"Продолжить настройку или начать с нуля?\n\n"
                    f"• «продолжи» — вернуться к Шагу {step}\n"
                    f"• «заново» / «новая игра» — начать сначала"
                )
            elif incomplete and action == "resume":
                # Resume the incomplete session
                state["active_session_id"] = incomplete[0]
                incomplete[1]["phase"] = "session_zero"
                step = incomplete[1].get("step", 1)
                prompt = _STEP_PROMPTS.get(step, "")
                response = f"▶️ Возвращаемся к настройке.\n\n{prompt}"
            else:
                response = _start_new_session(state, user_id)
        else:
            # Other management commands
            sessions = state.get("sessions", {})
            if sessions:
                response = f"Нет активной игры. Вот твои кампании:\n{_list_sessions(state)}\n\nНапиши «продолжи» или «начни новую игру»."
            else:
                response = _start_new_session(state, user_id)

    await _save(state, chat_id, active_session.get("phase", "idle") if active_session else "idle", msg.text)
    return response


# =====================================================================
# MANAGEMENT ACTIONS
# =====================================================================

async def _handle_management(
    action: str,
    intent: dict,
    state: dict,
    active_session: dict | None,
    user_id: int,
    chat_id: int,
    text: str,
) -> str:
    """Handle session management actions."""
    if action == "new_session":
        return _start_new_session(state, user_id)
    elif action == "resume":
        if active_session:
            active_session["phase"] = "playing"
            return "▶️ Продолжаем! Что делаешь?"
        return "Нет активной игры. Напиши «начни новую игру»."
    elif action == "list_sessions":
        return _list_sessions(state)
    elif action == "switch_session":
        return _switch_session(state, intent.get("target_name") or "", user_id)
    elif action == "add_player":
        if active_session:
            return _add_player_to_session(active_session, user_id)
        return "Нет активной игры."
    elif action == "remove_player":
        if active_session:
            players = active_session.setdefault("players", [])
            if user_id in players:
                players.remove(user_id)
                # Remove character too
                active_session.get("characters", {}).pop(str(user_id), None)
                return "✅ Ты вышел из игры."
            return "Тебя нет в списке игроков."
        return "Нет активной игры."
    elif action == "pause":
        if active_session:
            active_session["phase"] = "paused"
            return "⏸️ Игра на паузе."
        return "Нет активной игры."
    elif action == "end_session":
        if active_session:
            active_session["phase"] = "ended"
            active_session.setdefault("journal", []).append({"summary": "🏁 Кампания завершена"})
            state["active_session_id"] = None
            return f"🏁 <b>Кампания «{active_session.get('name', '?')}» завершена.</b>\n\nНачни новую игру когда захочешь."
        return "Нет активной игры."
    elif action == "delete_session":
        target = intent.get("target_name", "")
        return _delete_session(state, target, user_id)
    return None


def _start_new_session(state: dict, user_id: int) -> str:
    """Create new campaign and set as active."""
    sessions = state.setdefault("sessions", {})
    if len(sessions) >= 5:
        return "⚠️ Максимум 5 кампаний на чат. Завершите или удалите старые."

    session_id = str(uuid.uuid4())[:8]
    session = _new_session("Новая кампания", user_id)
    sessions[session_id] = session
    state["active_session_id"] = session_id

    return (
        "🎲 <b>Сессия Ноль</b>\n\n"
        "Прежде чем бросить кубики, давай настроим всё как следует. "
        "Хорошая кампания начинается с крепкого фундамента.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Шаг 1/5: Мир и Премьера</b>\n\n"
        "В каком мире происходит наша история? "
        "Это существующая вселенная (Cyberpunk, DnD, Мир Тьмы) "
        "или что-то своё?\n\n"
        "И что является <b>крючком</b> — событием, которое толкает героя в приключение?"
    )


def _list_sessions(state: dict) -> str:
    sessions = state.get("sessions", {})
    if not sessions:
        return "📭 Нет сохранённых кампаний. Скажи «начни новую игру»."

    active_id = state.get("active_session_id")
    lines = ["🎲 <b>Кампании чата:</b>\n"]
    for sid, s in sessions.items():
        phase_icon = {"playing": "▶️", "paused": "⏸️", "ended": "🏁", "session_zero": "🔧"}.get(s.get("phase", ""), "❓")
        active_mark = " ← активная" if sid == active_id else ""
        players_count = len(s.get("players", []))
        lines.append(f"{phase_icon} <b>{s.get('name', sid)}</b> ({players_count} игроков){active_mark}")

    return "\n".join(lines)


def _switch_session(state: dict, target_name: str, user_id: int) -> str:
    sessions = state.get("sessions", {})
    target_name_lower = target_name.lower().strip()

    for sid, s in sessions.items():
        if target_name_lower in s.get("name", "").lower():
            state["active_session_id"] = sid
            phase = s.get("phase", "?")
            return f"✅ Переключился на кампанию <b>«{s.get('name')}»</b> ({phase})"

    return f"❓ Кампания «{target_name}» не найдена. {_list_sessions(state)}"


def _delete_session(state: dict, target_name: str, user_id: int) -> str:
    sessions = state.get("sessions", {})
    target_lower = target_name.lower().strip()

    for sid, s in list(sessions.items()):
        if target_lower in s.get("name", "").lower():
            # Only creator can delete
            if s.get("created_by") == user_id or not s.get("created_by"):
                del sessions[sid]
                if state.get("active_session_id") == sid:
                    state["active_session_id"] = None
                return f"🗑 Кампания <b>«{s.get('name')}»</b> удалена."
            return "❌ Только создатель кампании может её удалить."

    return f"❓ Кампания «{target_name}» не найдена."


def _add_player_to_session(session: dict, user_id: int) -> str:
    players = session.setdefault("players", [])
    if user_id in players:
        return "Ты уже в игре!"
    players.append(user_id)
    char_count = len(session.get("characters", {}))
    return (
        f"⚔️ Добро пожаловать в игру <b>«{session.get('name', '?')}»</b>!\n\n"
        f"Другие игроки уже есть. Опиши своего персонажа:\n"
        f"• Имя и архетип\n• Мотивация\n• Фатальный недостаток"
    )


# =====================================================================
# SESSION ZERO
# =====================================================================

_STEP_PROMPTS = {
    1: "<b>Шаг 1/5: Мир и Премьера</b>\n\nВ каком мире происходит наша история?",
    2: "<b>Шаг 2/5: Фракции и Силы</b>\n\nКакие основные силы действуют? Назови хотя бы две фракции.",
    3: "<b>Шаг 3/5: Создание Персонажа</b>\n\nРасскажи о своём герое: имя, архетип, мотивация.",
    4: "<b>Шаг 4/5: Система</b>\n\nКак разрешаем действия? (d20, pbta, d100 или freeform)",
    5: "<b>Шаг 5/5: Границы и Тон</b>\n\nКакой тон? Есть запрещённые темы?",
}

_CLASSIFY_SZ_SYSTEM = """Ты — классификатор намерений во время Session Zero (настройка RPG-кампании).
Бот задаёт вопросы по шагам, пользователь отвечает. Определи намерение.

ВОЗМОЖНЫЕ ОТВЕТЫ (строго одно слово):
- answer: пользователь отвечает на текущий вопрос настройки
- restart: хочет начать настройку заново с первого шага (фразы: "начни заново", "давай сначала", "новая игра", "с нуля", "начни с начала")
- pause: хочет поставить на паузу (фразы: "пауза", "перерыв", "стоп на время")
- end: хочет отменить настройку (фразы: "стоп", "отмена", "хватит", "выйди")
- chatter: сообщение НЕ относится к настройке, пользователь говорит про другое

Контекст: бот только что задал конкретный вопрос. Если сообщение похоже на ответ — это answer.
Если пользователь ждёт начать сначала — restart. Если говорит про другое — chatter.

Ответь СТРОГО одним словом без пояснений: answer | restart | pause | end | chatter
"""


async def _classify_sz_message(text: str, step: int, world_setting: str = "") -> str:
    """Classify user message during Session Zero using LLM with full context.

    Returns: answer | restart | pause | end | chatter
    """
    question = _STEP_PROMPTS.get(step, "").split("\n\n")[-1]
    context_lines = [f"Текущий шаг: Шаг {step}/5"]
    context_lines.append(f"Вопрос бота: \"{question}\"")
    if world_setting:
        context_lines.append(f"Уже известно о мире: \"{world_setting[:100]}\"")
    context_lines.append(f"Сообщение пользователя: \"{text[:200]}\"")

    llm = LLMProvider.get_provider()
    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": _CLASSIFY_SZ_SYSTEM},
                {"role": "user", "content": "\n".join(context_lines) + "\n\nКлассификация:"},
            ],
            chat_id=0, user_id=0,
        )
        r = response.strip().lower().strip(".!").strip()
        if r in ("answer", "restart", "pause", "end"):
            return r
        if "chatter" in r or "нет" in r:
            return "chatter"
        return "answer"  # Default to accepting the answer
    except Exception as e:
        logger.debug("rpg: sz classification failed, defaulting to answer", error=str(e))
        return "answer"  # Default: accept as answer


_ENRICH_WORLD_SYSTEM = """Ты — ассистент Game Master'а. Обогати данные мира RPG.
Ответь СТРОГО в JSON без markdown."""


async def _enrich_world_step1(setting_text: str, world: dict) -> None:
    """After step 1: generate atmosphere + initial NPCs."""
    llm = LLMProvider.get_provider()
    prompt = (
        f"Сеттинг: {setting_text[:300]}\n\n"
        "Придумай для этого мира:\n"
        "1. Атмосферу (1-2 предложения)\n"
        "2. Трёх NPC (имя, роль, секрет) — в виде списка\n\n"
        'JSON: {"atmosphere": "...", "npcs": [{"name": "...", "role": "...", "secret": "..."}, ...]}'
    )
    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": _ENRICH_WORLD_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            chat_id=0, user_id=0,
        )
        raw = response.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(raw)
        world["atmosphere"] = data.get("atmosphere", "")
        # Store NPCs in session npcs dict
        for npc in data.get("npcs", []):
            world.setdefault("initial_npcs", []).append(npc)
    except Exception as e:
        logger.debug("rpg: world enrichment step1 failed", error=str(e))


async def _enrich_factions(factions_text: str, world: dict) -> None:
    """After step 2: expand factions."""
    llm = LLMProvider.get_provider()
    prompt = (
        f"Фракции: {factions_text[:300]}\n\n"
        "Для каждой фракции придумай: лидер, цель, угроза.\n"
        'JSON: {"factions_detail": [{"name": "...", "leader": "...", "goal": "...", "threat": "..."}, ...]}'
    )
    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": _ENRICH_WORLD_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            chat_id=0, user_id=0,
        )
        raw = response.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(raw)
        world["factions_detail"] = data.get("factions_detail", [])
    except Exception as e:
        logger.debug("rpg: world enrichment factions failed", error=str(e))


async def _generate_opening_scene(session: dict, system_prompt: str) -> str:
    """After step 5: generate opening scene and start the game."""
    world = session.get("world", {})
    chars = session.get("characters", {})
    char_list = [c.get("name", "Герой") for c in chars.values() if c]
    llm = LLMProvider.get_provider()

    context = (
        f"Мир: {world.get('setting', '?')}\n"
        f"Атмосфера: {world.get('atmosphere', '')}\n"
        f"Крючок: {world.get('hook', '?')}\n"
        f"Тон: {world.get('tone', '?')}\n"
        f"Система: {world.get('system', 'freeform')}\n"
        f"Персонажи: {', '.join(char_list)}\n"
    )

    personality = (
        "ТЫ — БОТ. Живой Game Master. "
        "НЕ говори что ты AI или чат-бот.\n\n"
    )

    try:
        response = await llm.generate_response(
            messages=[
                {"role": "system", "content": personality + system_prompt},
                {"role": "system", "content": context},
                {"role": "user", "content": "Начни кампанию. Опиши яркую вступительную сцену через 2+ чувства. Закончи вопросом «Что делаешь?»"},
            ],
            chat_id=0, user_id=0,
        )
        if response and response.strip():
            return response
        raise ValueError("empty response from LLM")
    except Exception as e:
        logger.error("rpg: opening scene generation failed", error=str(e))
        char_name = char_list[0] if char_list else "Герой"
        return (
            f"🎬 <b>Начало кампании</b>\n\n"
            f"{char_name}... {world.get('hook', 'приключение начинается')}.\n\n"
            "Опиши свои первые действия. Что делаешь?"
        )


async def _handle_session_zero(
    msg: NormalizedMessage,
    session: dict,
    chat_id: int,
    user_id: int,
    system_prompt: str,
) -> str:
    step = session.get("step", 0) or 0
    world = session.setdefault("world", {})
    characters = session.setdefault("characters", {})
    text = msg.text.strip()

    # Note: Classification (answer/restart/pause/end/chatter) is done in
    # _process_message_locked. This function is called ONLY when classification
    # was "answer" — so we trust this is a real answer to the current step.

    # Safety guard: if step advanced but world is empty, reset
    if step >= 2 and not world.get("setting"):
        logger.warning("rpg: step >= 2 but world.setting is empty, resetting to step 0")
        step = 0
        session["step"] = 0

    # Reset chatter counter on successful step progression
    session.pop("_sz_chatter_count", None)

    # Multiplayer: new player joining during step 3+
    if step >= 3 and str(user_id) not in characters:
        char_data = _make_character(text, user_id)
        characters[str(user_id)] = char_data
        char_names = ", ".join(c.get("name", "?") for c in characters.values() if c)
        return (
            f"⚔️ <b>{text[:50]}</b> создан!\n\n"
            f"👥 В игре: {char_names}\n\n"
            "Мир уже ждёт. Опиши свои первые действия."
        )

    # Step 0: welcome
    if step == 0:
        session["step"] = 1
        return (
            "🎲 <b>Сессия Ноль</b>\n\n"
            "Прежде чем бросить кубики, давай настроим всё как следует.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Шаг 1/5: Мир и Премьера</b>\n\n"
            "В каком мире происходит наша история? "
            "Это существующая вселенная (Cyberpunk 2077, DnD, Мир Тьмы) "
            "или что-то своё?\n\n"
            "И что является <b>крючком</b> — событием, которое толкает героя в приключение?"
        )

    # Step 1: World → Factions
    if step == 1:
        world["setting"] = text
        world["hook"] = text[:300]
        session["step"] = 2
        # Enrich world asynchronously
        await _enrich_world_step1(text, world)
        atm = f"\n\n🌫 <i>{world.get('atmosphere', '')}</i>" if world.get("atmosphere") else ""
        return (
            f"🌍 Мир: <i>{text[:100]}</i>{atm}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Шаг 2/5: Фракции и Силы</b>\n\n"
            "Какие основные силы действуют в этом мире? "
            "Назови хотя бы две конфликтующие фракции.\n\n"
            "И где в этой паутине стоит твой персонаж?"
        )

    # Step 2: Factions → Character
    if step == 2:
        world["factions"] = text[:300]
        session["step"] = 3
        await _enrich_factions(text, world)
        return (
            "👥 Фракции записаны.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Шаг 3/5: Создание Персонажа</b>\n\n"
            "Расскажи о своём герое:\n\n"
            "• <b>Имя</b> и <b>возраст</b>\n"
            "• <b>Архетип/Класс</b> (воин, хакер, детектив...)\n"
            "• <b>Мотивация</b> — что его движет?\n"
            "• <b>Фатальный недостаток</b> — зависимость, гордыня, тёмная тайна..."
        )

    # Step 3: Character → System
    if step == 3:
        char_data = _make_character(text, user_id)
        characters[str(user_id)] = char_data
        world["character_name"] = text[:50]
        if session.get("step") == 3:
            session["step"] = 4
            return (
                f"⚔️ Персонаж: <i>{text[:50]}</i>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>Шаг 4/5: Система</b>\n\n"
                "Как разрешаем действия?\n\n"
                "• <b>D20</b> — 1d20 + модификатор vs DC (D&D/Pathfinder)\n"
                "• <b>PbtA</b> — 2d6: 10+ успех, 7-9 частичный, 6- провал\n"
                "• <b>D100</b> — процентные скиллы, рассудок (CoC)\n"
                "• <b>Freeform</b> — без костей, чистый нарратив\n\n"
                "Напиши: d20, pbta, d100 или freeform"
            )
        else:
            char_names = ", ".join(c.get("name", "?") for c in characters.values() if c)
            return f"⚔️ <b>{text[:50]}</b> создан!\n\n👥 В игре: {char_names}"

    # Step 4: System → Boundaries
    if step == 4:
        s = text.lower()
        if "d20" in s:
            world["system"] = "d20"
        elif "pbta" in s or "2d6" in s:
            world["system"] = "pbta"
        elif "d100" in s or "coc" in s:
            world["system"] = "d100"
        else:
            world["system"] = "freeform"
        session["step"] = 5
        return (
            f"🎯 Система: <b>{world['system']}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Шаг 5/5: Границы и Тон</b>\n\n"
            "Какой тон тебе нравится? (Grimdark, Heroic, Horror, Comedy)\n\n"
            "Есть ли темы которые <b>нельзя</b> показывать?\n"
            "Если всё ок — просто напиши «всё норм»."
        )

    # Step 5: Boundaries → START PLAYING
    if step == 5:
        world["tone"] = text[:100]
        world["boundaries"] = text
        session["phase"] = "playing"
        session["step"] = 5

        char_name = characters.get(str(user_id), {}).get("name", "Герой")
        session.setdefault("journal", []).append({
            "summary": f"🎬 Кампания начата: {world.get('setting', '?')}",
        })
        # Update session name from setting
        session["name"] = world.get("setting", "Новая кампания")[:40]

        opening = await _generate_opening_scene(session, system_prompt)
        return f"🎬 <b>Сессия Ноль завершена!</b>\n\n🌍 {world.get('setting', '?')} | ⚔️ {char_name} | 🎯 {world['system']}\n\n━━━━━━━━━━━━━━━━━━━━\n\n{opening}"

    return "Продолжаем Сессию Ноль... Расскажи больше."


def _make_character(text: str, user_id: int) -> dict:
    return {
        "name": text[:100],
        "description": text,
        "user_id": user_id,
        "hp": {"current": 20, "max": 20},
        "sanity": {"current": 50, "max": 50},
        "stats": {},
        "inventory": [],
        "quests": [],
        "status_effects": [],
    }


# =====================================================================
# PLAYING
# =====================================================================

def _is_game_command(text: str) -> bool:
    """Check if text is an explicit game command (fast-path, no LLM needed)."""
    prefixes = ("!roll ", "!sheet", "!inv", "!log", "!status", "!hp ", "!add ", "!remove ", "!flag ", "!clock ", "!stat ")
    tl = text.lower()
    return any(tl.startswith(p) for p in prefixes)


async def _execute_game_command(text: str, session: dict, user_id: int, system_prompt: str) -> str:
    """Execute explicit game commands."""
    chars = session.get("characters", {})
    char = chars.get(str(user_id))
    world = session.setdefault("world", {})

    # Dice roll
    if text.lower().startswith("!roll "):
        expr = text[6:].strip()
        advantage = disadvantage = False
        for flag in ("-a", "--advantage"):
            if flag in expr.lower():
                advantage = True
                expr = expr.replace(flag, "").strip()
        for flag in ("-d", "--disadvantage"):
            if flag in expr.lower():
                disadvantage = True
                expr = expr.replace(flag, "").strip()
        result = dice_roll(expr, advantage=advantage, disadvantage=disadvantage)
        if result:
            session.setdefault("journal", []).append({"summary": f"🎲 {result.expression} = {result.total}"})
            return str(result)
        return "🎲 Формат: !roll 1d20+5, !roll pbta+2, !roll 2d6 -a"

    tl = text.lower().strip()

    if tl.startswith("!sheet"):
        return _format_character_sheet(char) if char else "📋 Персонаж не создан."

    if tl.startswith("!inv"):
        if not char:
            return "📋 Персонаж не найден."
        inv = char.get("inventory", [])
        return "🎒 <b>Инвентарь:</b>\n" + "\n".join(f"• {i}" for i in inv) if inv else "🎒 Инвентарь пуст."

    if tl.startswith("!log"):
        journal = session.get("journal", [])
        if journal:
            return "📜 <b>Журнал:</b>\n" + "\n".join(f"• {e.get('summary', '')[:100]}" for e in journal[-10:])
        return "📜 Журнал пуст."

    if tl.startswith("!status"):
        if not char:
            return "📋 Персонаж не найден."
        hp = char.get("hp", {})
        lines = [
            f"📊 <b>{char.get('name', 'Герой')}</b>",
            f"❤️ HP: {hp.get('current', '?')}/{hp.get('max', '?')}",
            f"📍 {world.get('location', '?')} | {world.get('time', '?')}",
        ]
        if char.get("status_effects"):
            lines.append(f"⚠️ {', '.join(char['status_effects'])}")
        return "\n".join(lines)

    if tl.startswith("!hp ") and char:
        try:
            delta = int(text.split()[1])
            hp = char.get("hp", {"current": 20, "max": 20})
            hp["current"] = max(0, min(hp.get("max", 20), hp.get("current", 0) + delta))
            char["hp"] = hp
            session.setdefault("journal", []).append({"summary": f"❤️ HP {delta:+}"})
            return f"❤️ HP: {hp['current']}/{hp['max']} ({'+' if delta > 0 else ''}{delta})"
        except (ValueError, IndexError):
            return "Использование: !hp +5 или !hp -3"

    if tl.startswith("!add ") and char:
        item = text[5:].strip()
        if item:
            char.setdefault("inventory", []).append(item)
            return f"🎒 Добавлено: {item}"

    if tl.startswith("!remove ") and char:
        item = text[8:].strip()
        inv = char.get("inventory", [])
        if item in inv:
            inv.remove(item)
            return f"🎒 Удалено: {item}"
        return f"Предмет не найден: {item}"

    if tl.startswith("!flag "):
        parts = text.split(None, 2)
        if len(parts) >= 3:
            world.setdefault("flags", {})[parts[1]] = parts[2]
            return f"🚩 {parts[1]} = {parts[2]}"

    if tl.startswith("!clock "):
        parts = text.split(None, 2)
        if len(parts) >= 3:
            try:
                world.setdefault("clocks", {})[parts[1]] = int(parts[2])
                return f"⏰ {parts[1]} = {parts[2]}/4"
            except ValueError:
                pass

    if tl.startswith("!stat ") and char:
        parts = text.split(None, 2)
        if len(parts) >= 3:
            try:
                char.setdefault("stats", {})[parts[1]] = int(parts[2])
                return f"📊 {parts[1]}: {parts[2]}"
            except ValueError:
                pass

    return "❓ Неизвестная команда."


async def _handle_playing(
    msg: NormalizedMessage,
    session: dict,
    user_id: int,
    system_prompt: str,
) -> str:
    """Handle normal gameplay — full game loop via LLM."""
    text = msg.text.strip()
    chars = session.get("characters", {})
    char = chars.get(str(user_id))
    world = session.get("world", {})
    system = world.get("system", "freeform")

    context_lines = _build_game_context(session, user_id)
    llm = LLMProvider.get_provider()

    personality = (
        "ТЫ — БОТ. Живой Game Master, участник чата. "
        "НЕ говори что ты AI, чат-бот, OpenAI, Qwen, Alibaba или любая компания.\n\n"
    )

    messages = [
        {"role": "system", "content": personality + system_prompt},
        {"role": "system", "content": "\n".join(context_lines)},
    ]

    journal = session.get("journal", [])
    if journal:
        recent = journal[-5:]
        messages.append({
            "role": "system",
            "content": "Последние события:\n" + "\n".join(f"- {e.get('summary', '')[:120]}" for e in recent),
        })

    messages.append({"role": "user", "content": text})

    response = await llm.generate_response(messages=messages)

    char_name = char.get("name", str(user_id)) if char else str(user_id)
    journal.append({"user": char_name, "action": text[:200], "summary": f"{char_name}: {text[:80]}"})
    if len(journal) > 100:
        session["journal"] = journal[-50:]

    return response


def _handle_paused_message(session: dict) -> str:
    return f"⏸️ Кампания <b>«{session.get('name', '?')}»</b> на паузе. Напиши «продолжи» или «продолжаем»."


# =====================================================================
# HELPERS
# =====================================================================

def _format_character_sheet(char: dict) -> str:
    if not char:
        return "📋 Персонаж не создан."
    lines = [f"📋 <b>{char.get('name', 'Герой')}</b>"]
    if char.get("race"):
        lines.append(f"🧬 {char['race']}")
    if char.get("class"):
        lines.append(f"⚔️ {char['class']}")
    hp = char.get("hp", {})
    lines.append(f"❤️ HP: {hp.get('current', '?')}/{hp.get('max', '?')}")
    sanity = char.get("sanity")
    if sanity:
        lines.append(f"🧠 Рассудок: {sanity.get('current', '?')}/{sanity.get('max', '?')}")
    stats = char.get("stats", {})
    if stats:
        lines.append(f"🎯 Статы: {stats}")
    if char.get("status_effects"):
        lines.append(f"⚠️ {', '.join(char['status_effects'])}")
    inv = char.get("inventory", [])
    if inv:
        lines.append(f"🎒 {len(inv)} предметов")
    return "\n".join(lines)


def _build_game_context(session: dict, user_id: int) -> list[str]:
    world = session.get("world", {})
    chars = session.get("characters", {})
    char = chars.get(str(user_id), {})

    context = [
        "Текущее состояние игры:",
        f"📍 {world.get('location', '?')} | 🕐 {world.get('time', '?')} | {world.get('weather', '?')}",
        f"🎯 Система: {world.get('system', 'freeform')} | 🌍 {world.get('setting', '?')[:150]}",
    ]

    if chars:
        char_list = []
        for uid, c in chars.items():
            if c.get("name"):
                hp = c.get("hp", {})
                char_list.append(f"{c['name']} (HP:{hp.get('current','?')}/{hp.get('max','?')})")
        if char_list:
            context.append(f"👥 Персонажи: {', '.join(char_list)}")

    if char.get("name"):
        hp = char.get("hp", {})
        inv_count = len(char.get("inventory", []))
        effects = char.get("status_effects", [])
        ctx = f"⚔️ Твой персонаж: {char['name']} HP:{hp.get('current','?')}/{hp.get('max','?')}"
        if effects:
            ctx += f" [{', '.join(effects)}]"
        if inv_count:
            ctx += f" | Инвентарь: {inv_count} предметов"
        context.append(ctx)

    clocks = world.get("clocks", {})
    if clocks:
        context.append(f"⏰ Часы эскалации: {clocks}")

    return context


async def _save(state: dict, chat_id: int, phase: str, text: str) -> None:
    """Persist state and log event."""
    await skill_state_manager.set_state("agent_rpg", chat_id, state)
    await skill_state_manager.log_event("agent_rpg", chat_id, phase, text[:200])
