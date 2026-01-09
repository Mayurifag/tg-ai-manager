"""add_premium_column

Revision ID: 002_add_premium
Revises: 001_initial
Create Date: 2024-01-09 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_add_premium"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add is_premium column with default False
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("is_premium", sa.Boolean(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("is_premium")
