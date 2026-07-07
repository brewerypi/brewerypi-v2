"""rename tag_values timestamp to observed_at

Revision ID: ba9dc47dbd8c
Revises: 949027264204
Create Date: 2026-07-06 18:17:02.114950

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ba9dc47dbd8c'
down_revision: Union[str, Sequence[str], None] = '949027264204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename tag_values.timestamp -> observed_at (data preserved)."""
    with op.batch_alter_table("tag_values") as batch_op:
        batch_op.alter_column(
            "timestamp", new_column_name="observed_at"
        )


def downgrade() -> None:
    """Rename tag_values.observed_at -> timestamp."""
    with op.batch_alter_table("tag_values") as batch_op:
        batch_op.alter_column(
            "observed_at", new_column_name="timestamp"
        )
