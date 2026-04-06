"""add reminders table

Revision ID: a002_reminders
Revises: a001_initial
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa

revision = "a002_reminders"
down_revision = "a001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_sent", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_reminders_chat_remind_at", "reminders", ["chat_id", "remind_at"])
    op.create_index("ix_reminders_is_sent", "reminders", ["is_sent"])


def downgrade() -> None:
    op.drop_table("reminders")
