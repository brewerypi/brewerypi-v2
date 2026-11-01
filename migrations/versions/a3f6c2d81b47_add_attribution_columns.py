"""add attribution columns to tag_values and event_frames

Revision ID: a3f6c2d81b47
Revises: fc00cd54b37d
Create Date: 2026-11-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from brewerypi.database import NAMING_CONVENTION


# revision identifiers, used by Alembic.
revision: str = 'a3f6c2d81b47'
down_revision: Union[str, Sequence[str], None] = 'fc00cd54b37d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLite cannot ALTER a table to add a foreign key, so these nullable columns
# need batch mode's copy-and-move rebuild, and a rebuild from reflection would
# silently drop both tables' CHECK constraints and every ON DELETE RESTRICT
# (see d52f7c1a9e04). So each table is spelled out as it stands before the
# operation and passed as copy_from.
#
# Existing rows keep NULL, meaning "not known": nothing recorded who made them,
# and guessing would invent history.

TAG_VALUE_COLUMNS = ('recorded_by_id', 'updated_by_id')
TAG_VALUE_INDEXED = ('lookup_value_id', 'tag_id')
EVENT_FRAME_COLUMNS = ('started_by_id', 'ended_by_id', 'updated_by_id')
EVENT_FRAME_INDEXED = (
    'element_id',
    'event_frame_template_id',
    'parent_id',
    'source_event_frame_id',
    'destination_event_frame_id',
)


def _create_indexes(table: str, columns: tuple[str, ...]) -> None:
    for column in columns:
        op.create_index(
            op.f(f'ix_{table}_{column}'), table, [column], unique=False
        )


def _author_column(table: str, name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.Integer(),
        sa.ForeignKey(
            'users.id',
            name=f'fk_{table}_{name}_users',
            ondelete='RESTRICT',
        ),
        nullable=True,
    )


def _tag_values(with_authors: bool) -> sa.Table:
    """The table before the operation; indexes go with the rebuild."""
    columns = [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.Column('observed_at', sa.DateTime(), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('lookup_value_id', sa.Integer(), nullable=True),
    ]
    if with_authors:
        columns.extend(
            _author_column('tag_values', column)
            for column in TAG_VALUE_COLUMNS
        )
    return sa.Table(
        'tag_values',
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        *columns,
        sa.CheckConstraint(
            '(value IS NOT NULL) + (lookup_value_id IS NOT NULL) = 1',
            name='value_xor_lookup_value_id',
        ),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id']),
        sa.ForeignKeyConstraint(
            ['lookup_value_id'], ['lookup_values.id'], ondelete='RESTRICT'
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def _event_frames(with_authors: bool) -> sa.Table:
    """The table before the operation; indexes go with the rebuild."""
    columns = [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('element_id', sa.Integer(), nullable=False),
        sa.Column('event_frame_template_id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=45), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('source_event_frame_id', sa.Integer(), nullable=True),
        sa.Column('destination_event_frame_id', sa.Integer(), nullable=True),
    ]
    if with_authors:
        columns.extend(
            _author_column('event_frames', column)
            for column in EVENT_FRAME_COLUMNS
        )
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
        sa.ForeignKeyConstraint(
            ['source_event_frame_id'], ['event_frames.id'], ondelete='RESTRICT'
        ),
        sa.ForeignKeyConstraint(
            ['destination_event_frame_id'],
            ['event_frames.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table(
        'tag_values', copy_from=_tag_values(with_authors=False)
    ) as batch_op:
        for column in TAG_VALUE_COLUMNS:
            batch_op.add_column(_author_column('tag_values', column))
    _create_indexes('tag_values', TAG_VALUE_INDEXED + TAG_VALUE_COLUMNS)

    with op.batch_alter_table(
        'event_frames', copy_from=_event_frames(with_authors=False)
    ) as batch_op:
        for column in EVENT_FRAME_COLUMNS:
            batch_op.add_column(_author_column('event_frames', column))
    _create_indexes('event_frames', EVENT_FRAME_INDEXED + EVENT_FRAME_COLUMNS)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        'event_frames', copy_from=_event_frames(with_authors=True)
    ) as batch_op:
        for column in reversed(EVENT_FRAME_COLUMNS):
            batch_op.drop_column(column)
    _create_indexes('event_frames', EVENT_FRAME_INDEXED)

    with op.batch_alter_table(
        'tag_values', copy_from=_tag_values(with_authors=True)
    ) as batch_op:
        for column in reversed(TAG_VALUE_COLUMNS):
            batch_op.drop_column(column)
    _create_indexes('tag_values', TAG_VALUE_INDEXED)
