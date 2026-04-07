"""per-chat user profiles

Revision ID: a006_per_chat_profiles
Revises: a005_memory_upgrade
Create Date: 2026-04-07

Changes:
- Add chat_id to user_profiles
- Drop unique constraint on user_id, add composite unique (user_id, chat_id)
- Create index on (chat_id, user_id)
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "a006_per_chat_profiles"
down_revision = "a005_memory_upgrade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add chat_id column (nullable first to allow existing data)
    op.add_column(
        "user_profiles",
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
    )

    # 2. For existing profiles without chat_id, set a sentinel value
    #    We use 0 as default for private chats (will be populated properly on next update)
    op.execute(
        "UPDATE user_profiles SET chat_id = 0 WHERE chat_id IS NULL"
    )

    # 3. Make chat_id NOT NULL
    op.alter_column(
        "user_profiles",
        "chat_id",
        nullable=False,
        existing_type=sa.BigInteger(),
    )

    # 4. Drop unique constraint on user_id and add composite unique
    # PostgreSQL: drop the auto-created unique index first
    op.drop_index("ix_user_profiles_user_id", table_name="user_profiles", if_exists=True)
    
    # The unique constraint might be named differently, try to drop it
    op.execute("""
        DO $$
        BEGIN
            -- Try to drop the unique constraint on user_id
            ALTER TABLE user_profiles DROP CONSTRAINT IF EXISTS uq_user_profiles_user_id;
            -- Also try common auto-generated name
            ALTER TABLE user_profiles DROP CONSTRAINT IF EXISTS user_profiles_user_id_key;
        END
        $$;
    """)

    # 5. Create composite unique index
    op.create_unique_constraint(
        "uq_user_profiles_user_chat",
        "user_profiles",
        ["user_id", "chat_id"],
    )

    # 6. Create index for fast lookups
    op.create_index(
        "ix_user_profiles_chat_user",
        "user_profiles",
        ["chat_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_profiles_chat_user", table_name="user_profiles")
    op.drop_constraint("uq_user_profiles_user_chat", "user_profiles", type_="unique")
    
    # Restore unique on user_id
    op.create_unique_constraint(
        "user_profiles_user_id_key",
        "user_profiles",
        ["user_id"],
    )
    
    # Remove chat_id
    op.drop_column("user_profiles", "chat_id")
