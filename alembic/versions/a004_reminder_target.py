"""add target_user_id to reminders

Revision ID: a004_reminder_target
Revises: a003_chat_vibe_and_censorship
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa

revision = "a004_reminder_target"
down_revision = "a003_chat_vibe_and_censorship"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_reminders_target_user", "reminders", ["target_user_id"])


def downgrade() -> None:
    op.drop_index("ix_reminders_target_user")
    op.drop_column("reminders", "target_user_id")
