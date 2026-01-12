"""add_debug_mode

Revision ID: 004_add_debug_mode
Revises: 003_add_rule_config
Create Date: 2026-01-12 12:00:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "004_add_debug_mode"
down_revision: Union[str, None] = "003_add_rule_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("debug_mode", sa.Boolean(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("debug_mode")
