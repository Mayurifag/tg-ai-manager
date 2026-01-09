"""initial_schema

Revision ID: 001_initial
Revises:
Create Date: 2024-01-08 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Users Table ---
    # Stores credentials, session string, and global settings
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("api_id", sa.Integer(), nullable=False),  # Unique & Not Null
        sa.Column("api_hash", sa.Text(), nullable=False),  # Not Null
        sa.Column("username", sa.Text(), nullable=True),  # Changed from phone_number
        sa.Column("session_string", sa.Text(), nullable=True),  # Telethon StringSession
        # Boolean Settings (SQLite uses 0/1, handled by app logic)
        sa.Column(
            "autoread_service_messages",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("autoread_polls", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("autoread_self", sa.Boolean(), server_default="0", nullable=False),
        # Other settings
        sa.Column(
            "autoread_bots",
            sa.Text(),
            server_default="'@lolsBotCatcherBot'",
            nullable=True,
        ),
        sa.Column("autoread_regex", sa.Text(), server_default="''", nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_id", name="uq_users_api_id"),
    )

    # --- Rules Table ---
    # Chat-specific rules. 'enabled' column REMOVED. Presence of row = enabled.
    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),  # FK to user
        sa.Column("rule_type", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_index("idx_rules_user_chat", "rules", ["user_id", "chat_id"])
    op.create_index("idx_rules_chat_topic", "rules", ["chat_id", "topic_id"])


def downgrade() -> None:
    op.drop_table("rules")
    op.drop_table("users")
