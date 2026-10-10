"""add event_frames transfer links (source/destination)

Revision ID: d52f7c1a9e04
Revises: e1c7a4f39b02
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from brewerypi.database import NAMING_CONVENTION


# revision identifiers, used by Alembic.
revision: str = 'd52f7c1a9e04'
down_revision: Union[str, Sequence[str], None] = 'e1c7a4f39b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLite cannot ALTER a table to add a foreign key, so the two nullable
# transfer columns need batch mode's copy-and-move rebuild. Batch mode
# normally rebuilds from reflection, and SQLAlchemy's SQLite dialect reflects
# neither CHECK constraints nor the ON DELETE clause of a column-level foreign
# key -- so a reflected rebuild would silently drop
# ck_event_frames_ended_after_started. Spelling the pre-migration table out
# here and passing it as copy_from is what keeps the rebuild lossless.
#
# Existing rows are untouched: a frame recorded before transfers existed
# simply has no lineage links.

NEW_COLUMNS = ('source_event_frame_id', 'destination_event_frame_id')
INDEXED_COLUMNS = ('element_id', 'event_frame_template_id', 'parent_id')


def _create_indexes(columns: tuple[str, ...]) -> None:
    for column in columns:
        op.create_index(
            op.f(f'ix_event_frames_{column}'),
            'event_frames',
            [column],
            unique=False,
        )


def _link_column(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.Integer(),
        sa.ForeignKey(
            'event_frames.id',
            name=f'fk_event_frames_{name}_event_frames',
            ondelete='RESTRICT',
        ),
        nullable=True,
    )


def _event_frames(with_links: bool) -> sa.Table:
    """The table as it stands before the operation, for copy_from.

    Indexes are omitted: the rebuild drops the table's indexes with it, so
    every index is (re)created explicitly afterwards.
    """
    columns = [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('element_id', sa.Integer(), nullable=False),
        sa.Column('event_frame_template_id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=45), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
    ]
    if with_links:
        columns.extend(_link_column(column) for column in NEW_COLUMNS)
    return sa.Table(
        'event_frames',
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        *columns,
        sa.CheckConstraint(
            'ended_at IS NULL OR ended_at > started_at',
            name='ended_after_started',
        ),
        sa.ForeignKeyConstraint(['element_id'], ['elements.id']),
        sa.ForeignKeyConstraint(
            ['event_frame_template_id'], ['event_frame_templates.id']
        ),
        sa.ForeignKeyConstraint(['parent_id'], ['event_frames.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table(
        'event_frames', copy_from=_event_frames(with_links=False)
    ) as batch_op:
        for column in NEW_COLUMNS:
            batch_op.add_column(_link_column(column))
    _create_indexes(INDEXED_COLUMNS + NEW_COLUMNS)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        'event_frames', copy_from=_event_frames(with_links=True)
    ) as batch_op:
        for column in reversed(NEW_COLUMNS):
            batch_op.drop_column(column)
    _create_indexes(INDEXED_COLUMNS)
