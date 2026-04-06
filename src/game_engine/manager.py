import json

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_session
from src.database.models import GameSession, GameEvent

logger = structlog.get_logger()


class GameManager:
    """Manages game sessions and state."""

    async def start_session(self, chat_id: int, owner_id: int, name: str) -> str:
        """Start a new game session."""
        # End any existing active session first
        async for session in get_session():
            stmt = select(GameSession).where(
                and_(GameSession.chat_id == chat_id, GameSession.is_active == True)
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing:
                existing.is_active = False
                await session.commit()

        state = json.dumps({
            "phase": "intro",
            "characters": {},
            "world": {},
            "turn": 0,
        })

        async for session in get_session():
            game_session = GameSession(
                chat_id=chat_id,
                owner_id=owner_id,
                name=name,
                game_type="dnd",
                state=state,
            )
            session.add(game_session)
            await session.commit()
            await session.refresh(game_session)

            session.add(
                GameEvent(
                    session_id=game_session.id,
                    event_type="session_started",
                    content=f"Игра '{name}' начата",
                    actor_id=owner_id,
                )
            )
            await session.commit()

        logger.info("Game session started", chat_id=chat_id, name=name)
        return f"🎮 Игра '{name}' начата! Используйте /game continue для продолжения."

    async def get_active_session(self, chat_id: int) -> GameSession | None:
        """Get the active game session for a chat."""
        async for session in get_session():
            stmt = select(GameSession).where(
                and_(GameSession.chat_id == chat_id, GameSession.is_active == True)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        return None

    async def process_message(self, msg: "NormalizedMessage", game_session: GameSession) -> str:  # type: ignore[name-defined] # noqa: F821
        """Process a message in the context of an active game."""
        state = json.loads(game_session.state)
        state["turn"] = state.get("turn", 0) + 1

        # Simple game loop — in a real DnD, this would be much more complex
        response = f"[Game:{game_session.name}] Ход {state['turn']}. {msg.text}"

        # Save state
        async for session in get_session():
            game_session.state = json.dumps(state)
            await session.commit()

            session.add(
                GameEvent(
                    session_id=game_session.id,
                    event_type="player_action",
                    content=msg.text,
                    actor_id=msg.user_id,
                )
            )
            await session.commit()

        return response

    async def get_status(self, chat_id: int) -> str:
        """Get the status of the active game."""
        session_obj = await self.get_active_session(chat_id)
        if not session_obj:
            return "🎮 Нет активной игры."
        state = json.loads(session_obj.state)
        return (
            f"🎮 <b>{session_obj.name}</b>\n"
            f"Тип: {session_obj.game_type}\n"
            f"Фаза: {state.get('phase', 'unknown')}\n"
            f"Ход: {state.get('turn', 0)}"
        )

    async def resume_session(self, chat_id: int) -> str:
        """Resume an active game session."""
        session_obj = await self.get_active_session(chat_id)
        if not session_obj:
            return "🎮 Нет активной игры. Начните новую с /game start."
        return f"🎮 Игра '{session_obj.name}' продолжена!"

    async def get_event_log(self, chat_id: int) -> str:
        """Get the event log for the active game."""
        async for session in get_session():
            game_stmt = select(GameSession).where(
                and_(GameSession.chat_id == chat_id, GameSession.is_active == True)
            )
            game_result = await session.execute(game_stmt)
            game = game_result.scalar_one_or_none()

            if not game:
                return "🎮 Нет активной игры."

            stmt = (
                select(GameEvent)
                .where(GameEvent.session_id == game.id)
                .order_by(GameEvent.created_at.desc())
                .limit(20)
            )
            result = await session.execute(stmt)
            events = list(result.scalars().all())

            if not events:
                return "🎮 Журнал пуст."

            lines = [f"• {e.created_at.strftime('%H:%M')} — {e.content}" for e in events]
            return "📜 <b>Журнал событий:</b>\n\n" + "\n".join(lines)

    async def end_session(self, chat_id: int) -> str:
        """End the active game session."""
        async for session in get_session():
            stmt = select(GameSession).where(
                and_(GameSession.chat_id == chat_id, GameSession.is_active == True)
            )
            result = await session.execute(stmt)
            game = result.scalar_one_or_none()

            if not game:
                return "🎮 Нет активной игры."

            game.is_active = False
            session.add(
                GameEvent(
                    session_id=game.id,
                    event_type="session_ended",
                    content=f"Игра '{game.name}' завершена",
                )
            )
            await session.commit()

        logger.info("Game session ended", chat_id=chat_id)
        return f"🎮 Игра '{game.name}' завершена."
