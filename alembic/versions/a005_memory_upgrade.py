"""memory system upgrade

Revision ID: a005_memory_upgrade
Revises: a004_reminder_target
Create Date: 2026-04-07

Adds memory lifecycle fields, memory summaries table, and extraction batch tracking.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision = "a005_memory_upgrade"
down_revision = "a004_reminder_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Helper: check if column exists
    def col_exists(table, col):
        return conn.execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                f"WHERE table_name='{table}' AND column_name='{col}')"
            )
        ).scalar()

    # Helper: check if table exists
    def table_exists(table):
        return conn.execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                f"WHERE table_name='{table}')"
            )
        ).scalar()

    # 1. Add lifecycle fields to memory_items (only if missing)
    cols_to_add = [
        ("frequency", lambda: op.add_column("memory_items", sa.Column("frequency", sa.Integer(), nullable=False, server_default="1"))),
        ("last_used_at", lambda: op.add_column("memory_items", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))),
        ("access_count", lambda: op.add_column("memory_items", sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"))),
        ("ttl_seconds", lambda: op.add_column("memory_items", sa.Column("ttl_seconds", sa.Integer(), nullable=True))),
        ("consolidated_from_ids", lambda: op.add_column("memory_items", sa.Column("consolidated_from_ids", postgresql.ARRAY(sa.Integer()), nullable=True))),
        ("is_active", lambda: op.add_column("memory_items", sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"))),
        ("tags", lambda: op.add_column("memory_items", sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True))),
    ]
    for col_name, add_fn in cols_to_add:
        if not col_exists("memory_items", col_name):
            add_fn()

    # 2. Create memory_summaries table
    if not table_exists("memory_summaries"):
        op.create_table(
            "memory_summaries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("topics", postgresql.ARRAY(sa.Text()), nullable=True),
            sa.Column("start_message_id", sa.Integer(), nullable=True),
            sa.Column("end_message_id", sa.Integer(), nullable=True),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("embedding_vector", Vector(768), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_memory_summaries_chat_id", "memory_summaries", ["chat_id"])
        op.create_index("ix_memory_summaries_user_id", "memory_summaries", ["user_id"])
        op.create_index(
            "ix_memory_summaries_chat_time",
            "memory_summaries",
            ["chat_id", "created_at"],
        )

    # 3. Create memory_extraction_batches table
    if not table_exists("memory_extraction_batches"):
        op.create_table(
            "memory_extraction_batches",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("start_message_id", sa.Integer(), nullable=False),
            sa.Column("end_message_id", sa.Integer(), nullable=False),
            sa.Column("message_count", sa.Integer(), nullable=False),
            sa.Column("items_extracted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_extraction_batches_chat_id",
            "memory_extraction_batches",
            ["chat_id"],
        )
        op.create_index(
            "ix_extraction_batches_status",
            "memory_extraction_batches",
            ["status"],
        )

    # 4. Create embedding index (IF NOT EXISTS)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_items_embedding ON memory_items USING ivfflat (embedding_vector vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_embedding", table_name="memory_items")
    op.drop_index("ix_extraction_batches_status", table_name="memory_extraction_batches")
    op.drop_index("ix_extraction_batches_chat_id", table_name="memory_extraction_batches")
    op.drop_table("memory_extraction_batches")
    op.drop_index("ix_memory_summaries_chat_time", table_name="memory_summaries")
    op.drop_index("ix_memory_summaries_user_id", table_name="memory_summaries")
    op.drop_index("ix_memory_summaries_chat_id", table_name="memory_summaries")
    op.drop_table("memory_summaries")
    op.drop_column("memory_items", "tags")
    op.drop_column("memory_items", "is_active")
    op.drop_column("memory_items", "consolidated_from_ids")
    op.drop_column("memory_items", "ttl_seconds")
    op.drop_column("memory_items", "access_count")
    op.drop_column("memory_items", "last_used_at")
    op.drop_column("memory_items", "frequency")
