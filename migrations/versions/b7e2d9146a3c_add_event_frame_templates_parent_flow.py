"""add event_frame_templates.parent_flow

Revision ID: b7e2d9146a3c
Revises: d52f7c1a9e04
Create Date: 2026-10-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2d9146a3c'
down_revision: Union[str, Sequence[str], None] = 'd52f7c1a9e04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A plain nullable column with no constraint, so SQLite can add it in place.
# Existing templates stay NULL -- none of them implies a transfer end until an
# admin says so -- and existing frames are left alone: an implied end is
# filled in when a frame is created or next linked, never retroactively.


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'event_frame_templates',
        sa.Column('parent_flow', sa.String(length=11), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        'event_frame_templates', schema=None
    ) as batch_op:
        batch_op.drop_column('parent_flow')
