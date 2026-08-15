"""add event_frame_templates.step_order

Revision ID: e1c7a4f39b02
Revises: 75ae90dc029d
Create Date: 2026-08-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1c7a4f39b02'
down_revision: Union[str, Sequence[str], None] = '75ae90dc029d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Existing nested templates have no recorded order, so number each parent's
# children alphabetically -- deterministic, collision-free, and an admin can
# re-set them. Top-level templates keep the server default of 1: they are the
# process rather than a step inside one.
BACKFILL = """
UPDATE event_frame_templates
SET step_order = (
    SELECT ordered.step
    FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY parent_id ORDER BY name
               ) AS step
        FROM event_frame_templates
        WHERE parent_id IS NOT NULL
    ) AS ordered
    WHERE ordered.id = event_frame_templates.id
)
WHERE parent_id IS NOT NULL
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'event_frame_templates',
        sa.Column(
            'step_order', sa.Integer(), nullable=False, server_default='1'
        ),
    )
    op.execute(BACKFILL)
    with op.batch_alter_table(
        'event_frame_templates', schema=None
    ) as batch_op:
        batch_op.alter_column('step_order', server_default=None)
        batch_op.create_unique_constraint(
            op.f('uq_event_frame_templates_parent_id'),
            ['parent_id', 'step_order'],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        'event_frame_templates', schema=None
    ) as batch_op:
        batch_op.drop_constraint(
            op.f('uq_event_frame_templates_parent_id'), type_='unique'
        )
        batch_op.drop_column('step_order')
