"""personality weights: aggression_level and sarcasm_level per chat

Revision ID: a008_personality_weights
Revises: a007_skills
Create Date: 2026-04-15

Adds user-adjustable personality weight columns to chat_vibe_profiles.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "a008_personality_weights"
down_revision = "a007_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_vibe_profiles",
        sa.Column("aggression_level", sa.Float(), nullable=False, server_default="0.5"),
    )
    op.add_column(
        "chat_vibe_profiles",
        sa.Column("sarcasm_level", sa.Float(), nullable=False, server_default="0.6"),
    )


def downgrade() -> None:
    op.drop_column("chat_vibe_profiles", "sarcasm_level")
    op.drop_column("chat_vibe_profiles", "aggression_level")
