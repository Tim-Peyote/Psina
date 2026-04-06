"""add chat vibe profiles and censorship settings

Revision ID: a003_chat_vibe_and_censorship
Revises: a002_reminders
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa

revision = "a003_chat_vibe_and_censorship"
down_revision = "a002_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Vibe profiles для чатов
    op.create_table(
        "chat_vibe_profiles",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("formality", sa.Float(), server_default=sa.text("0.3")),
        sa.Column("mate_level", sa.Float(), server_default=sa.text("0.0")),
        sa.Column("avg_length", sa.Float(), server_default=sa.text("50.0")),
        sa.Column("emoji_frequency", sa.Float(), server_default=sa.text("0.2")),
        sa.Column("mood", sa.String(32), server_default=sa.text("'neutral'")),
        sa.Column("messages_analyzed", sa.Integer(), server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    # Расширенные настройки чата (цензура)
    op.create_table(
        "chat_settings_extended",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("censorship_level", sa.String(32), server_default=sa.text("'moderate'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("chat_settings_extended")
    op.drop_table("chat_vibe_profiles")
