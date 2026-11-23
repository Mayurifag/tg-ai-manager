"""Keep the verified Telegram account ID independently of its session."""

import sqlalchemy as sa
from alembic import op

revision = "007_saved_telegram_identity"
down_revision = "006_unique_rule_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("telegram_id")
