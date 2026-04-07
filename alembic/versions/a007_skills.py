"""skills system

Revision ID: a007_skills
Revises: a006_per_chat_profiles
Create Date: 2026-04-07

Adds skills registry and per-chat skill state storage.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "a007_skills"
down_revision = "a006_per_chat_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Skills registry — what skills are installed
    op.create_table(
        "skills",
        sa.Column("slug", sa.String(64), nullable=False, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("triggers", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # 2. Per-chat skill state — isolated state per chat per skill
    op.create_table(
        "skill_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_slug", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("state_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["skill_slug"], ["skills.slug"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_unique_constraint(
        "uq_skill_state_skill_chat",
        "skill_state",
        ["skill_slug", "chat_id"],
    )
    op.create_index("ix_skill_state_chat", "skill_state", ["chat_id"])
    op.create_index("ix_skill_state_skill", "skill_state", ["skill_slug"])
    op.create_index(
        "ix_skill_state_chat_active",
        "skill_state",
        ["chat_id", "is_active"],
    )

    # 3. Skill event log — audit trail for skill actions
    op.create_table(
        "skill_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_slug", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_events_chat", "skill_events", ["chat_id"])
    op.create_index("ix_skill_events_skill", "skill_events", ["skill_slug"])


def downgrade() -> None:
    op.drop_index("ix_skill_events_skill", table_name="skill_events")
    op.drop_index("ix_skill_events_chat", table_name="skill_events")
    op.drop_table("skill_events")
    op.drop_index("ix_skill_state_chat_active", table_name="skill_state")
    op.drop_index("ix_skill_state_skill", table_name="skill_state")
    op.drop_index("ix_skill_state_chat", table_name="skill_state")
    op.drop_constraint("uq_skill_state_skill_chat", "skill_state", type_="unique")
    op.drop_table("skill_state")
    op.drop_table("skills")
