"""initial_schema

Revision ID: 001_initial
Revises:
Create Date: 2024-01-08 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("session_string", sa.Text(), nullable=True),
        sa.Column(
            "autoread_service_messages",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("autoread_polls", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("autoread_self", sa.Boolean(), server_default="0", nullable=False),
        sa.Column(
            "autoread_bots",
            sa.Text(),
            server_default="'@lolsBotCatcherBot'",
            nullable=True,
        ),
        sa.Column("autoread_regex", sa.Text(), server_default="''", nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
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
