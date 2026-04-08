"""Context pack builder.

Assembles a strictly limited context for LLM generation:
- System prompt
- Recent messages (token-limited)
- Session summary (if available)
- Relevant memory (top-k)
- Optional web context

Enforces hard token limits to prevent context overflow.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import Message, MemorySummary, UserProfile
from src.memory_services.retrieval_service import retrieval_service
from src.memory_services.models import ContextPack, MemorySearchResult
from src.utils.sanitize import sanitize_for_prompt

logger = structlog.get_logger()


class ContextPackBuilder:
    """Build limited, focused context packs for LLM generation."""

    def __init__(self) -> None:
        self.max_tokens = getattr(settings, "max_context_pack_tokens", 4000)
        self.max_recent_messages = getattr(settings, "max_context_recent_messages", 20)
        self.max_memory_items = getattr(settings, "max_context_memory_items", 10)
        self.max_summary_tokens = getattr(settings, "max_context_summary_tokens", 500)

    async def build_context_pack(
        self,
        system_prompt: str,
        chat_id: int,
        user_id: int | None = None,
        query: str = "",
        include_user_profile: bool = True,
        include_web_context: str = "",
        knowledge_context: str = "",
        reply_context: dict | None = None,
    ) -> ContextPack:
        """Build a complete context pack for LLM generation.

        Args:
            system_prompt: Base system prompt
            chat_id: Current chat ID
            user_id: User who sent the message
            query: What the user is asking about (for memory retrieval)
            include_user_profile: Whether to include user profile summary
            include_web_context: Web search results if available
            knowledge_context: Additional knowledge context from analyzer

        Returns:
            ContextPack with all components and token estimate
        """
        pack = ContextPack(system_prompt=system_prompt)

        # 1. Get recent messages (limited)
        recent_messages = await self._get_recent_messages(chat_id)
        pack.recent_messages = recent_messages

        # 2. Get session summary if available
        session_summary = await self._get_session_summary(chat_id)
        pack.session_summary = session_summary

        # 3. Retrieve relevant memory
        if query:
            relevant_memories = await retrieval_service.search(
                query=query,
                chat_id=chat_id,
                user_id=user_id,
                top_k=self.max_memory_items,
            )
        else:
            # Get recent context for the chat
            relevant_memories = await retrieval_service.get_chat_context(
                chat_id=chat_id,
                top_k=self.max_memory_items,
            )

        pack.relevant_memories = relevant_memories[:self.max_memory_items]

        # Record access for retrieved memories
        for memory in pack.relevant_memories:
            await retrieval_service.record_access(memory.item_id)

        # 4. Get user profile summary (per-chat isolated)
        if include_user_profile and user_id:
            profile_summary = await self._get_user_profile_summary(user_id, chat_id)
            pack.user_profile_summary = profile_summary

        # 5. Web context
        pack.web_context = include_web_context

        # 6. Knowledge context
        pack.knowledge_context = knowledge_context

        # 7. Reply context
        pack.reply_context = reply_context

        # 7. Estimate tokens and trim if needed
        pack.total_tokens_estimate = self._estimate_pack_tokens(pack)
        if pack.total_tokens_estimate > self.max_tokens:
            pack = self._trim_pack(pack)

        logger.debug(
            "Context pack built",
            chat_id=chat_id,
            user_id=user_id,
            tokens=pack.total_tokens_estimate,
            memories_count=len(pack.relevant_memories),
            messages_count=len(pack.recent_messages),
        )

        return pack

    async def _get_recent_messages(self, chat_id: int) -> list[dict]:
        """Get recent messages from the chat, resolving author names."""
        from src.database.models import UserProfile, User

        async for session in get_session():
            stmt = (
                select(Message)
                .where(Message.chat_id == chat_id)
                .order_by(Message.created_at.desc())
                .limit(self.max_recent_messages)
            )
            result = await session.execute(stmt)
            messages = list(result.scalars().all())

            # Reverse to chronological order
            messages.reverse()

            # Collect user IDs to resolve names
            user_ids = {m.user_id for m in messages if m.user_id and m.user_id > 0}

            # Resolve names: first try user_profiles.display_name, then User.first_name/username
            user_names: dict[int, str] = {}

            # From user_profiles
            if user_ids:
                stmt = select(UserProfile).where(
                    UserProfile.chat_id == chat_id,
                    UserProfile.user_id.in_(user_ids),
                )
                result = await session.execute(stmt)
                profiles = list(result.scalars().all())
                for p in profiles:
                    if p.display_name:
                        user_names[p.user_id] = p.display_name

                # From User table (fallback for users without profiles)
                missing_ids = user_ids - set(user_names.keys())
                if missing_ids:
                    stmt = select(User).where(User.id.in_(missing_ids))
                    result = await session.execute(stmt)
                    users = list(result.scalars().all())
                    for u in users:
                        name = u.first_name or u.username
                        if name:
                            user_names[u.id] = name

            # Return within the session context
            return [
                {
                    "user_id": m.user_id,
                    "text": m.text,
                    "role": m.role.value if hasattr(m.role, "value") else str(m.role),
                    "author": user_names.get(m.user_id, "") if m.user_id and m.user_id > 0 else "бот",
                }
                for m in messages
            ]

        return []

    async def _get_session_summary(self, chat_id: int) -> str:
        """Get the most recent session summary."""
        async for session in get_session():
            stmt = (
                select(MemorySummary)
                .where(MemorySummary.chat_id == chat_id)
                .order_by(MemorySummary.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            return summary.content if summary else ""

    async def _get_user_profile_summary(self, user_id: int, chat_id: int) -> str:
        """Get user profile as a summary string for a specific chat."""
        async for session in get_session():
            stmt = select(UserProfile).where(
                UserProfile.user_id == user_id,
                UserProfile.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            profile = result.scalar_one_or_none()

            if not profile:
                return ""

            parts = []
            if profile.display_name:
                parts.append(f"Имя: {profile.display_name}")
            if profile.traits:
                import json
                try:
                    traits = json.loads(profile.traits)
                    # Фильтруем мусор
                    clean_traits = [t for t in traits if 5 < len(t) < 200 and not t.startswith("[")]
                    if clean_traits:
                        parts.append(f"Черты: {', '.join(clean_traits[:5])}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if profile.interests:
                import json
                try:
                    interests = json.loads(profile.interests)
                    clean_interests = [i for i in interests if 5 < len(i) < 200 and not i.startswith("[")]
                    if clean_interests:
                        parts.append(f"Интересы: {', '.join(clean_interests[:5])}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if profile.summary and 5 < len(profile.summary) < 500:
                parts.append(f"О пользователе: {profile.summary}")

            return "\n".join(parts) if parts else ""

        return ""

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        if not text:
            return 0
        # ~4 chars per token for Cyrillic/Latin mix
        return len(text) // 4

    def _estimate_pack_tokens(self, pack: ContextPack) -> int:
        """Estimate total tokens for the pack."""
        total = self._estimate_tokens(pack.system_prompt)
        total += self._estimate_tokens(pack.session_summary)
        total += self._estimate_tokens(pack.user_profile_summary)
        total += self._estimate_tokens(pack.web_context)
        total += self._estimate_tokens(pack.knowledge_context)

        for msg in pack.recent_messages:
            total += self._estimate_tokens(msg.get("text", ""))

        for memory in pack.relevant_memories:
            total += self._estimate_tokens(memory.content)

        return total

    def _trim_pack(self, pack: ContextPack) -> ContextPack:
        """Trim context pack to fit within token limits."""
        current_tokens = pack.total_tokens_estimate

        # 1. Trim memories first (lowest score first)
        if current_tokens > self.max_tokens and pack.relevant_memories:
            memories_tokens = sum(
                self._estimate_tokens(m.content) for m in pack.relevant_memories
            )
            keep_count = max(
                1,
                int(len(pack.relevant_memories) * (self.max_tokens / current_tokens)),
            )
            pack.relevant_memories = pack.relevant_memories[:keep_count]
            current_tokens = self._estimate_pack_tokens(pack)

        # 2. Trim messages (keep most recent)
        if current_tokens > self.max_tokens and pack.recent_messages:
            keep_count = max(3, int(len(pack.recent_messages) * 0.5))
            pack.recent_messages = pack.recent_messages[-keep_count:]
            current_tokens = self._estimate_pack_tokens(pack)

        # 3. Trim session summary
        if current_tokens > self.max_tokens and pack.session_summary:
            summary_tokens = self._estimate_tokens(pack.session_summary)
            if summary_tokens > self.max_summary_tokens:
                pack.session_summary = pack.session_summary[: self.max_summary_tokens * 4]
                current_tokens = self._estimate_pack_tokens(pack)

        pack.total_tokens_estimate = current_tokens
        return pack

    def format_pack_for_llm(self, pack: ContextPack) -> list[dict]:
        """Format context pack into LLM messages."""
        messages = [
            {"role": "system", "content": pack.system_prompt},
        ]

        # Add user profile context
        if pack.user_profile_summary:
            messages.append({
                "role": "system",
                "content": f"Информация о пользователе:\n{pack.user_profile_summary}",
            })

        # Add session summary
        if pack.session_summary:
            messages.append({
                "role": "system",
                "content": f"Краткое содержание предыдущего общения:\n{pack.session_summary}",
            })

        # Add knowledge context
        if pack.knowledge_context:
            messages.append({
                "role": "system",
                "content": f"Контекст знаний:\n{pack.knowledge_context}",
            })

        # Add reply context (what the user is replying to)
        if pack.reply_context:
            reply_author = sanitize_for_prompt(
                pack.reply_context.get("username") or pack.reply_context.get("first_name") or "кто-то",
                max_length=50,
            )
            reply_text = pack.reply_context.get("text", "")[:200]
            messages.append({
                "role": "system",
                "content": f"Пользователь отвечает на сообщение {reply_author}: «{reply_text}»",
            })

        # Add relevant memories
        if pack.relevant_memories:
            memory_text = "🧠 Важная память по теме:\n"
            for i, memory in enumerate(pack.relevant_memories, 1):
                memory_text += f"{i}. {memory.content}\n"
            messages.append({"role": "system", "content": memory_text.strip()})

        # Add web context
        if pack.web_context:
            messages.append({
                "role": "system",
                "content": f"Данные из интернета:\n{pack.web_context}",
            })

        # Add recent messages
        for msg in pack.recent_messages:
            role = msg.get("role", "user")
            author = msg.get("author", "")
            if not author:
                # No name known — just show the text
                messages.append({
                    "role": "user" if role == "user" else "assistant",
                    "content": msg.get("text", ""),
                })
            else:
                safe_author = sanitize_for_prompt(author, max_length=50)
                messages.append({
                    "role": "user" if role == "user" else "assistant",
                    "content": f"[{safe_author}]: {msg.get('text', '')}",
                })

        return messages


# Singleton
context_pack_builder = ContextPackBuilder()
