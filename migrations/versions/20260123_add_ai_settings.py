"""add_ai_settings

Revision ID: 005_add_ai_settings
Revises: 004_add_debug_mode
Create Date: 2026-01-23 12:00:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "005_add_ai_settings"
down_revision: Union[str, None] = "004_add_debug_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as batch_op:
        batch_op.add_column(
            sa.Column("ai_enabled", sa.Boolean(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "ai_provider", sa.Text(), server_default="'gemini'", nullable=True
            )
        )
        batch_op.add_column(
            sa.Column(
                "ai_model", sa.Text(), server_default="'gemini-pro'", nullable=True
            )
        )
        batch_op.add_column(sa.Column("ai_api_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_base_url", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "skip_ads_enabled", sa.Boolean(), server_default="0", nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as batch_op:
        batch_op.drop_column("ai_enabled")
        batch_op.drop_column("ai_provider")
        batch_op.drop_column("ai_model")
        batch_op.drop_column("ai_api_key")
        batch_op.drop_column("ai_base_url")
        batch_op.drop_column("skip_ads_enabled")
