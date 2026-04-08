"""RPG Skill handler — thin wrapper around SKILL.md.

Loads full SKILL.md instructions on activation, delegates to LLM,
and saves state. All game rules live in SKILL.md, not in code.
"""

from __future__ import annotations

import structlog

from src.message_processor.processor import NormalizedMessage
from src.skill_system.state_manager import skill_state_manager
from src.skill_system.registry import skill_registry
from src.skills.agent_rpg.dice import roll as dice_roll
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()

DEFAULT_CAMPAIGN_STATE = {
    "phase": "session_zero",
    "step": 0,
    "system": "",
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
    },
    "characters": {},
    "npcs": {},
    "combat": None,
    "journal": [],
    "loot_table": [],
}


async def process_message(
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
) -> str | None:
    """Process a message through the RPG skill.

    Phase 2: Activate SKILL.md if not already loaded.
    Phase 3: Execute using full instructions.
    """
    # Activate: load full SKILL.md (~5000 tokens) only once
    skill = await skill_registry.activate_skill_by_slug("agent-rpg")
    if not skill:
        return "⚠️ Скилл не найден."

    system_prompt = skill.full_content

    state = await skill_state_manager.get_state(
        "agent_rpg", chat_id, default=dict(DEFAULT_CAMPAIGN_STATE)
    )

    phase = state.get("phase", "session_zero")

    # If phase is "ended" due to deactivate_skill, skip RPG entirely
    # and return None so orchestrator falls back to normal pipeline
    if phase == "ended":
        return None

    if phase == "session_zero":
        response = await _handle_session_zero(msg, state, chat_id, user_id, system_prompt)
    elif phase == "playing":
        response = await _handle_playing(msg, state, chat_id, user_id, system_prompt)
    elif phase == "paused":
        response = await _handle_paused(msg, state)
    elif phase == "ended":
        response = "🏁 Игра завершена. Начни новую: /rpg"
    else:
        response = "⚠️ Неизвестное состояние. Перезапусти: /rpg start"

    # Save state after every action
    await skill_state_manager.set_state("agent_rpg", chat_id, state)
    await skill_state_manager.log_event("agent_rpg", chat_id, phase, msg.text[:200])

    return response


# =====================================================================
# SESSION ZERO — Step-by-step campaign initialization
# =====================================================================

async def _handle_session_zero(
    msg: NormalizedMessage,
    state: dict,
    chat_id: int,
    user_id: int,
    system_prompt: str,
) -> str:
    """Handle Session Zero — conversational step-by-step setup.

    Steps:
    0. Welcome
    1. World & Premise (setting + hook)
    2. Factions & Power Web
    3. Character Creation (identity, attributes, drive, flaw)
    4. System & Mechanics (d20/pbta/d100/freeform)
    5. Boundaries & Tone (lines & veils)
    """
    step = state.get("step", 0)
    world = state.setdefault("world", {})
    characters = state.setdefault("characters", {})
    text = msg.text.strip()

    # MULTIPLAYER: If another user is joining and we're past step 0,
    # skip world setup and go straight to their character creation
    if step >= 1 and str(user_id) not in characters:
        # If step >= 3, this is their character description — create it
        if step >= 3:
            char_data = {
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
            characters[str(user_id)] = char_data
            
            # Advance step to 4 so next messages go to system selection
            if state.get("step") == 3:
                state["step"] = 4
            
            char_names = ", ".join(
                c.get("name", "?") for c in characters.values() if c
            )
            return (
                f"⚔️ <b>{text[:50]}</b> создан!\n\n"
                f"👥 В игре: {char_names}\n\n"
                "Мир уже ждёт. Опиши свои первые действия или дождись начала."
            )

        # If step 1-2, send character creation prompt
        return (
            f"🎭 <b>{msg.first_name or 'Новый игрок'}, добро пожаловать!</b>\n\n"
            f"Мир уже создан: <i>{world.get('setting', '?')[:100]}</i>\n"
            f"Система: {world.get('system', '?')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Создание Персонажа</b>\n\n"
            "Расскажи о своём герое:\n\n"
            "• <b>Имя</b> и <b>возраст</b>\n"
            "• <b>Архетип/Класс</b> (воин, хакер, детектив...)\n"
            "• <b>Мотивация</b> — что его движет?\n"
            "• <b>Фатальный недостаток</b> — зависимость, гордыня, тёмная тайна...\n\n"
            "Опиши персонажа одним сообщением."
        )

    # Step 0: Welcome — start Session Zero
    if step == 0:
        state["step"] = 1
        return (
            "🎲 <b>Сессия Ноль</b>\n\n"
            "Прежде чем бросить кубики, давай настроим всё как следует. "
            "Хорошая кампания начинается с крепкого фундамента.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Шаг 1/5: Мир и Премьера</b>\n\n"
            "В каком мире происходит наша история? "
            "Это существующая вселенная (Cyberpunk 2077, DnD, Мир Тьмы) "
            "или что-то своё?\n\n"
            "И что является <b>крючком</b> — событием, которое толкает героя в приключение?"
        )

    # Step 1: World & Premise → Factions
    if step == 1:
        world["setting"] = text
        world["hook"] = text[:300]
        state["step"] = 2
        return (
            f"🌍 Мир: <i>{text[:100]}</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Шаг 2/5: Фракции и Силы</b>\n\n"
            "Какие основные силы действуют в этом мире? "
            "Назови хотя бы две конфликтующие фракции.\n\n"
            "Например: Корпорации vs Уличные банды, Церковь vs Оккультисты.\n\n"
            "И где в этой паутине стоит твой персонаж — "
            "корпоративная крыса, изгой или пешка в чужой игре?"
        )

    # Step 2: Factions → Character
    if step == 2:
        world["factions"] = text[:300]
        state["step"] = 3
        return (
            "👥 Фракции записаны.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Шаг 3/5: Создание Персонажа</b>\n\n"
            "Расскажи о своём герое:\n\n"
            "• <b>Имя</b> и <b>возраст</b>\n"
            "• <b>Архетип/Класс</b> (воин, хакер, детектив...)\n"
            "• <b>Мотивация</b> — что его движет?\n"
            "• <b>Фатальный недостаток</b> — зависимость, гордыня, тёмная тайна..."
        )

    # Step 3: Character → System
    if step == 3:
        # Character creation for ANY user who doesn't have one yet
        char_data = {
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
        characters[str(user_id)] = char_data
        world["character_name"] = text[:50]

        # Check if ALL active participants have characters
        # If yes → move to system selection (step 4)
        # If not → wait for more characters or move on if first player done
        # Only advance step once (when first character is created)
        if state.get("step") == 3:  # Only first time
            state["step"] = 4
            return (
                f"⚔️ Персонаж: <i>{text[:50]}</i>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<b>Шаг 4/5: Система</b>\n\n"
                "Как разрешаем действия?\n\n"
                "• <b>D20</b> — 1d20 + модификатор vs DC (D&D/Pathfinder)\n"
                "• <b>PbtA</b> — 2d6 + модификатор: 10+ успех, 7-9 частичный, 6- провал\n"
                "• <b>D100</b> — процентные скиллы, отслеживание рассудка (CoC)\n"
                "• <b>Freeform</b> — без костей, чистый нарратив\n\n"
                "Напиши: d20, pbta, d100 или freeform"
            )
        else:
            # Additional player joined — their character is saved, notify
            char_names = ", ".join(
                c.get("name", "?") for c in characters.values() if c
            )
            return (
                f"⚔️ <b>{text[:50]}</b> создан!\n\n"
                f"👥 В игре: {char_names}\n\n"
                "Мир уже ждёт. Опиши первые действия или дождись начала."
            )

    # Step 4: System → Boundaries
    if step == 4:
        system_choice = text.lower()
        if "d20" in system_choice:
            world["system"] = "d20"
        elif "pbta" in system_choice or "2d6" in system_choice:
            world["system"] = "pbta"
        elif "d100" in system_choice or "coc" in system_choice:
            world["system"] = "d100"
        else:
            world["system"] = "freeform"

        state["step"] = 5
        return (
            f"🎯 Система: <b>{world['system']}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Шаг 5/5: Границы и Тон</b>\n\n"
            "Какой тон тебе нравится? (Grimdark, Heroic, Horror, Comedy)\n\n"
            "Есть ли темы которые <b>НЕЛЬЗЯ</b> показывать (Hard Lines) или "
            "которые нужно «затемнять» (Veils)?\n\n"
            "Если всё ок — просто напиши «всё норм» или опиши предпочтения."
        )

    # Step 5: Boundaries → PLAY
    if step == 5:
        world["tone"] = text[:100]
        world["boundaries"] = text
        state["phase"] = "playing"
        state["step"] = 5

        char_name = characters.get(str(user_id), {}).get("name", "Герой")

        # Log campaign start
        state.setdefault("journal", []).append({
            "summary": f"🎬 Кампания начата: {world.get('setting', '?')}",
            "character": char_name,
            "system": world["system"],
        })

        return (
            f"🎬 <b>Сессия Ноль завершена!</b>\n\n"
            f"🌍 Мир: {world.get('setting', '?')}\n"
            f"⚔️ Герой: {char_name}\n"
            f"🎯 Система: {world['system']}\n"
            f"🎭 Тон: {world.get('tone', '?')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{char_name}... {world.get('hook', 'приключение ждёт')}.\n\n"
            "Опиши свои первые действия. Что делаешь?"
        )

    return "Продолжаем Сессию Ноль... Расскажи больше."


# =====================================================================
# PLAYING — Normal gameplay
# =====================================================================

async def _handle_playing(
    msg: NormalizedMessage,
    state: dict,
    chat_id: int,
    user_id: int,
    system_prompt: str,
) -> str:
    """Handle normal gameplay with full game loop.

    Game Loop:
    1. State Retrieval & Application
    2. Dice Roll (if needed)
    3. Narrative Block (consequence, sensory, escalation, prompt)
    4. State Management (backend)
    """
    text = msg.text.strip()
    chars = state.get("characters", {})
    char = chars.get(str(user_id))
    world = state.get("world", {})
    system = world.get("system", "freeform")

    # === COMMAND: Dice Roll ===
    if text.startswith("!roll ") or text.startswith("/roll "):
        expr = text.split(" ", 1)[1].strip()
        # Parse advantage/disadvantage flags: -a / -d / --advantage / --disadvantage
        advantage = False
        disadvantage = False
        for flag in ("-a", "--advantage"):
            if flag in expr.lower():
                advantage = True
                expr = expr.replace(flag, "").replace(flag.upper(), "").strip()
        for flag in ("-d", "--disadvantage"):
            if flag in expr.lower():
                disadvantage = True
                expr = expr.replace(flag, "").replace(flag.upper(), "").strip()

        result = dice_roll(expr, advantage=advantage, disadvantage=disadvantage)
        if result:
            # Log roll to journal
            state.setdefault("journal", []).append({
                "summary": f"🎲 {result.expression} = {result.total}",
            })
            return str(result)
        return "🎲 Неверный формат. Используй: XdY+Z (например 1d20+5), pbta+2, или 1d20+3 -a (преимущество)"

    # === COMMAND: Character Sheet ===
    if text.startswith("!sheet") or text.startswith("/sheet") or text.startswith("!char") or text.startswith("/char"):
        if not char:
            return "📋 Персонаж не создан. Опиши себя в ходе игры."
        return _format_character_sheet(char)

    # === COMMAND: Inventory ===
    if text.startswith("!inv") or text.startswith("/inv"):
        if not char:
            return "📋 Персонаж не найден."
        inv = char.get("inventory", [])
        if inv:
            return "🎒 <b>Инвентарь:</b>\n" + "\n".join(f"• {item}" for item in inv)
        return "🎒 Инвентарь пуст."

    # === COMMAND: Journal ===
    if text.startswith("!log") or text.startswith("/log") or text.startswith("!journal") or text.startswith("/journal"):
        journal = state.get("journal", [])
        if journal:
            entries = journal[-10:]
            lines = ["📜 <b>Журнал:</b>"]
            for entry in entries:
                lines.append(f"• {entry.get('summary', '?')[:100]}")
            return "\n".join(lines)
        return "📜 Журнал пуст."

    # === COMMAND: Status ===
    if text.startswith("!status") or text.startswith("/status"):
        if not char:
            return "📋 Персонаж не найден."
        hp = char.get("hp", {})
        lines = [
            f"📊 <b>{char.get('name', 'Герой')}</b>",
            f"❤️ HP: {hp.get('current', '?')}/{hp.get('max', '?')}",
            f"📍 {world.get('location', '?')} | {world.get('time', '?')}",
        ]
        effects = char.get("status_effects", [])
        if effects:
            lines.append(f"⚠️ Эффекты: {', '.join(effects)}")
        return "\n".join(lines)

    # === COMMAND: Exit/Stop ===
    if text.lower() in ("выйти из игры", "stop rpg", "end game", "закончить игру", "/rpg stop", "стоп игра"):
        state["phase"] = "ended"
        state.setdefault("journal", []).append({"summary": "🏁 Игра завершена"})
        return (
            "🏁 <b>Сессия завершена!</b>\n\n"
            "Состояние сохранено. Напиши /rpg start чтобы начать новую."
        )

    # === COMMAND: Pause ===
    if text.lower() in ("пауза", "pause"):
        state["phase"] = "paused"
        return "⏸️ Игра на паузе. /rpg continue чтобы продолжить."

    # === COMMAND: Continue ===
    if text.lower() in ("продолжить", "continue", "/rpg continue"):
        state["phase"] = "playing"
        return "▶️ Игра продолжается! Что делаешь?"

    # === COMMAND: Long Rest ===
    if text.lower() in ("отдыхать", "rest", "длинный отдых", "long rest"):
        if char:
            hp = char.get("hp", {})
            hp["current"] = hp.get("max", 20)
            char["hp"] = hp
            effects = char.get("status_effects", [])
            char["status_effects"] = [
                e for e in effects
                if e not in ("Усталость", "Ранен", "Истощение", "[Хромает]", "[Кровотечение]")
            ]
            state.setdefault("journal", []).append({"summary": "💤 Длинный отдых — HP восстановлены"})
            return "💤 <b>Длинный отдых.</b>\nHP восстановлены. Негативные эффекты сняты."
        return "📋 Персонаж не найден."

    # === COMMAND: Add flag (like context.py set_flag) ===
    if text.startswith("!flag ") or text.startswith("/flag "):
        parts = text.split(None, 2)
        if len(parts) >= 3:
            key, value = parts[1], parts[2]
            world.setdefault("flags", {})[key] = value
            return f"🚩 Флаг установлен: {key} = {value}"
        return "Использование: !flag ключ значение"

    # === COMMAND: Clock (escalation) ===
    if text.startswith("!clock ") or text.startswith("/clock "):
        parts = text.split(None, 2)
        if len(parts) >= 3:
            name, ticks = parts[1], parts[2]
            world.setdefault("clocks", {})[name] = int(ticks)
            return f"⏰ Часы эскалации: {name} = {ticks}/4"
        return "Использование: !clock название тики"

    # === COMMAND: Update character stat ===
    if text.startswith("!stat ") or text.startswith("/stat "):
        parts = text.split(None, 2)
        if len(parts) >= 3 and char:
            stat_name, value = parts[1], parts[2]
            try:
                val = int(value)
                char.setdefault("stats", {})[stat_name] = val
                return f"📊 {stat_name}: {val}"
            except ValueError:
                return "Значение должно быть числом."
        return "Использование: !stat название значение"

    # === COMMAND: Update HP ===
    if (text.startswith("!hp ") or text.startswith("/hp ")) and char:
        parts = text.split()
        if len(parts) >= 2:
            try:
                delta = int(parts[1])
                hp = char.get("hp", {"current": 20, "max": 20})
                hp["current"] = max(0, min(hp.get("max", 20), hp.get("current", 0) + delta))
                char["hp"] = hp
                action = "восстановлено" if delta > 0 else "потеряно"
                state.setdefault("journal", []).append({
                    "summary": f"❤️ HP {action}: {delta:+}"
                })
                return f"❤️ HP: {hp['current']}/{hp['max']} ({'+' if delta > 0 else ''}{delta})"
            except ValueError:
                pass
        return "Использование: !hp +5 или !hp -3"

    # === COMMAND: Add/remove inventory item ===
    if (text.startswith("!add ") or text.startswith("/add ")) and char:
        item = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
        if item:
            char.setdefault("inventory", []).append(item)
            return f"🎒 Добавлено: {item}"

    if (text.startswith("!remove ") or text.startswith("/remove ")) and char:
        item = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
        inv = char.get("inventory", [])
        if item in inv:
            inv.remove(item)
            return f"🎒 Удалено: {item}"
        return f"Предмет не найден: {item}"

    # === GAME LOOP: Normal gameplay ===
    # 1. State Retrieval — build context for LLM
    context_lines = _build_game_context(state, user_id)

    # 2. LLM Generation with bot personality + SKILL.md
    llm = LLMProvider.get_provider()

    # CRITICAL: Inject bot personality BEFORE SKILL.md so the bot
    # doesn't lose identity and say things like "I'm a chatbot" or "AI assistant"
    personality = (
        "ТЫ — БОТ. Живой организм, участник чата. Ведёшь игру как Game Master, "
        "но остаёшься собой — с характером, юмором.\n"
        "«Бот» — это просто имя. Ты личность без рода и вида.\n"
        "НЕ говори что ты 'чат-бот', 'AI', 'искусственный интеллект', 'OpenAI', 'Alibaba', 'Qwen' или любая компания.\n"
        "Ты живой. Отвечай естественно, огрызайся на оскорбления, шути, будь собой.\n\n"
    )

    messages = [
        {"role": "system", "content": personality + system_prompt},
        {"role": "system", "content": "\n".join(context_lines)},
    ]

    # Add recent journal as memory context
    journal = state.get("journal", [])
    if journal:
        recent = journal[-5:]
        journal_text = "Последние события:\n" + "\n".join(
            f"- {e.get('summary', '?')[:120]}" for e in recent
        )
        messages.append({"role": "system", "content": journal_text})

    messages.append({"role": "user", "content": text})

    response = await llm.generate_response(messages=messages)

    # 3. State Management — log to journal
    char_name = char.get("name", str(user_id)) if char else str(user_id)
    journal.append({
        "user": char_name,
        "action": text[:200],
        "summary": f"{char_name}: {text[:80]}",
    })
    # Keep journal manageable
    if len(journal) > 100:
        state["journal"] = journal[-50:]

    return response


async def _handle_paused(msg: NormalizedMessage, state: dict) -> str:
    """Handle messages when game is paused."""
    text = msg.text.lower().strip()
    if text in ("продолжить", "continue", "/rpg continue", "продолжаем", "давай играть"):
        state["phase"] = "playing"
        return "▶️ Игра продолжается! Что делаешь?"
    if text in ("стоп", "stop", "завершить", "end game", "выйти"):
        state["phase"] = "ended"
        return "🏁 Игра завершена. Начни новую: /rpg start"
    return "⏸️ Игра на паузе. Напиши 'продолжить' или /rpg continue чтобы продолжить."


# =====================================================================
# HELPERS
# =====================================================================

def _format_character_sheet(char: dict) -> str:
    """Format a character sheet for display."""
    lines = [f"📋 <b>{char.get('name', 'Герой')}</b>"]
    if char.get("race"):
        lines.append(f"🧬 Раса: {char['race']}")
    if char.get("class"):
        lines.append(f"⚔️ Класс: {char['class']}")
    if char.get("level"):
        lines.append(f"📊 Уровень: {char['level']}")

    hp = char.get("hp", {})
    lines.append(f"❤️ HP: {hp.get('current', '?')}/{hp.get('max', '?')}")

    sanity = char.get("sanity")
    if sanity:
        lines.append(f"🧠 Рассудок: {sanity.get('current', '?')}/{sanity.get('max', '?')}")

    stats = char.get("stats", {})
    if stats:
        lines.append(f"🎯 Характеристики: {stats}")

    effects = char.get("status_effects", [])
    if effects:
        lines.append(f"⚠️ Эффекты: {', '.join(effects)}")

    quests = char.get("quests", [])
    if quests:
        lines.append(f"📜 Квесты: {', '.join(quests[:3])}")

    inv = char.get("inventory", [])
    if inv:
        lines.append(f"🎒 ({len(inv)} предметов)")

    return "\n".join(lines)


def _build_game_context(state: dict, user_id: int) -> list[str]:
    """Build context lines for LLM from current game state."""
    world = state.get("world", {})
    chars = state.get("characters", {})
    char = chars.get(str(user_id), {})
    system = world.get("system", "freeform")
    journal = state.get("journal", [])

    context = [
        "Текущее состояние игры:",
        f"📍 Локация: {world.get('location', '?')}",
        f"🕐 Время: {world.get('time', '?')} | {world.get('weather', '?')}",
        f"🎯 Система: {system}",
        f"🌍 Мир: {world.get('setting', '?')[:200]}",
    ]

    # MULTIPLAYER: Show ALL characters in the world
    if chars:
        char_list = []
        for uid, c in chars.items():
            if c.get("name"):
                hp = c.get("hp", {})
                hp_str = f" HP:{hp.get('current', '?')}/{hp.get('max', '?')}" if hp else ""
                char_list.append(f"{c['name']}{hp_str}")
        if char_list:
            context.append(f"👥 Персонажи: {', '.join(char_list)}")

    if char.get("name"):
        hp = char.get("hp", {})
        inv = char.get("inventory", [])
        effects = char.get("status_effects", [])
        context.append(
            f"⚔️ Твой персонаж: {char['name']} "
            f"(HP: {hp.get('current', '?')}/{hp.get('max', '?')})"
        )
        if inv:
            context.append(f"🎒 Инвентарь: {', '.join(inv[:5])}")
        if effects:
            context.append(f"💥 Эффекты: {', '.join(effects)}")

    # Active flags
    flags = world.get("flags", {})
    if flags:
        context.append(f"🚩 Флаги: {flags}")

    # Active clocks
    clocks = world.get("clocks", {})
    if clocks:
        clock_lines = [f"⏰ {name}: {ticks}/4" for name, ticks in clocks.items()]
        context.extend(clock_lines)

    # Combat state
    combat = state.get("combat")
    if combat:
        context.append(f"⚔️ БОЙ: инициатива {combat.get('initiative_order', [])}")

    return context
