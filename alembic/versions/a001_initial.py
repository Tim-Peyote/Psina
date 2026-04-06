"""initial migration — create all tables

Revision ID: a001_initial
Revises:
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("language_code", sa.String(10), nullable=True),
        sa.Column("is_bot", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("type", sa.Enum("private", "group", "supergroup", "channel", name="chattype"), nullable=False),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("bot_mode", sa.String(32), server_default=sa.text("'observer'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.Enum("user", "assistant", "system", name="messagerole"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_index("ix_messages_user_id", "messages", ["user_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])

    op.create_table(
        "memory_items",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "type",
            sa.Enum(
                "raw_message", "fact", "preference", "event",
                "relationship", "group_rule", "summary", "game_state",
                name="memorytype",
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", postgresql.BYTEA(), nullable=True),
        sa.Column("confidence", sa.Float(), server_default=sa.text("0.5")),
        sa.Column("relevance", sa.Float(), server_default=sa.text("1.0")),
        sa.Column("source", sa.String(64), server_default=sa.text("'chat'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_memory_items_chat_id", "memory_items", ["chat_id"])
    op.create_index("ix_memory_items_user_id", "memory_items", ["user_id"])
    op.create_index("ix_memory_items_type", "memory_items", ["type"])

    op.execute(
        """
        ALTER TABLE memory_items
        ADD COLUMN IF NOT EXISTS embedding_vector vector(768)
        """
    )

    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("traits", sa.Text(), nullable=True),
        sa.Column("interests", sa.Text(), nullable=True),
        sa.Column("relationships", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "summaries",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topics", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_summaries_chat_date", "summaries", ["chat_id", "date"])

    op.create_table(
        "usage_stats",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("tokens_prompt", sa.Integer(), server_default=sa.text("0")),
        sa.Column("tokens_completion", sa.Integer(), server_default=sa.text("0")),
        sa.Column("requests_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_usage_stats_date_provider", "usage_stats", ["date", "provider"])

    op.create_table(
        "game_sessions",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("game_type", sa.String(64), server_default=sa.text("'dnd'")),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_game_sessions_chat_id", "game_sessions", ["chat_id"])
    op.create_index("ix_game_sessions_active", "game_sessions", ["is_active"])

    op.create_table(
        "game_events",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_game_events_session_id", "game_events", ["session_id"])


def downgrade() -> None:
    op.drop_table("game_events")
    op.drop_table("game_sessions")
    op.drop_table("usage_stats")
    op.drop_table("summaries")
    op.drop_table("user_profiles")
    op.drop_table("memory_items")
    op.drop_table("messages")
    op.drop_table("chats")
    op.drop_table("users")
    sa.Enum(name="chattype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="messagerole").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="memorytype").drop(op.get_bind(), checkfirst=True)
