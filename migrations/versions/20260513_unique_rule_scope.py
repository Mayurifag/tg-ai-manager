"""unique_rule_scope

Revision ID: 006_unique_rule_scope
Revises: 005_add_ai_fields
Create Date: 2026-05-13 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


revision: str = "006_unique_rule_scope"
down_revision: Union[str, None] = "005_add_ai_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM rules
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM rules
            GROUP BY user_id, rule_type, chat_id, COALESCE(topic_id, -1)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_unique_scope
        ON rules (user_id, rule_type, chat_id, COALESCE(topic_id, -1))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_rules_unique_scope")
