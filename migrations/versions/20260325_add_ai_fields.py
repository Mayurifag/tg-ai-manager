"""add_ai_fields

Revision ID: 005_add_ai_fields
Revises: 004_add_debug_mode
Create Date: 2026-03-25 10:00:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "005_add_ai_fields"
down_revision: Union[str, None] = "004_add_debug_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("ai_provider", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_model", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_api_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("ai_prompt")
        batch_op.drop_column("ai_api_key")
        batch_op.drop_column("ai_model")
        batch_op.drop_column("ai_provider")
