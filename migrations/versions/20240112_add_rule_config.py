"""add_rule_config

Revision ID: 003_add_rule_config
Revises: 002_add_premium
Create Date: 2024-01-12 12:00:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "003_add_rule_config"
down_revision: Union[str, None] = "002_add_premium"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("rules") as batch_op:
        batch_op.add_column(
            sa.Column("config", sa.Text(), server_default="{}", nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("rules") as batch_op:
        batch_op.drop_column("config")
