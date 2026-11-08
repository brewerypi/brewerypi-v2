"""add batch properties: event_frame_properties and two template columns

Revision ID: e5b8a1c40f27
Revises: a3f6c2d81b47
Create Date: 2026-11-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b8a1c40f27'
down_revision: Union[str, Sequence[str], None] = 'a3f6c2d81b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The checks need batch mode's rebuild of event_frame_attribute_templates.
# Rebuilding from reflection is safe for this table: it has no CHECK
# constraints and no ON DELETE clauses for the rebuild to drop (the trap
# d52f7c1a9e04 spells out). Existing rows become measurements (both false).

TEMPLATES = 'event_frame_attribute_templates'
PROPERTIES = 'event_frame_properties'
INDEXED = (
    'event_frame_id',
    'event_frame_attribute_template_id',
    'lookup_value_id',
    'recorded_by_id',
    'updated_by_id',
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table(TEMPLATES, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'is_property',
                sa.Boolean(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                'is_required',
                sa.Boolean(),
                server_default=sa.text('0'),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f(f'ck_{TEMPLATES}_required_needs_property'),
            'is_property OR NOT is_required',
        )
        batch_op.create_check_constraint(
            op.f(f'ck_{TEMPLATES}_property_no_end_default'),
            'NOT is_property OR (default_end_value IS NULL'
            ' AND default_end_lookup_value_id IS NULL)',
        )

    op.create_table(
        PROPERTIES,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_frame_id', sa.Integer(), nullable=False),
        sa.Column(
            'event_frame_attribute_template_id', sa.Integer(), nullable=False
        ),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('lookup_value_id', sa.Integer(), nullable=True),
        sa.Column('recorded_by_id', sa.Integer(), nullable=True),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.CheckConstraint(
            '(value IS NOT NULL) + (lookup_value_id IS NOT NULL) = 1',
            name=op.f('ck_event_frame_properties_value_xor_lookup_value_id'),
        ),
        sa.ForeignKeyConstraint(
            ['event_frame_attribute_template_id'],
            ['event_frame_attribute_templates.id'],
            name=op.f(
                'fk_event_frame_properties_event_frame_attribute_template_id'
                '_event_frame_attribute_templates'
            ),
        ),
        sa.ForeignKeyConstraint(
            ['event_frame_id'],
            ['event_frames.id'],
            name=op.f('fk_event_frame_properties_event_frame_id_event_frames'),
        ),
        sa.ForeignKeyConstraint(
            ['lookup_value_id'],
            ['lookup_values.id'],
            name=op.f(
                'fk_event_frame_properties_lookup_value_id_lookup_values'
            ),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['recorded_by_id'],
            ['users.id'],
            name=op.f('fk_event_frame_properties_recorded_by_id_users'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['updated_by_id'],
            ['users.id'],
            name=op.f('fk_event_frame_properties_updated_by_id_users'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_event_frame_properties')),
        sa.UniqueConstraint(
            'event_frame_id',
            'event_frame_attribute_template_id',
            name=op.f('uq_event_frame_properties_event_frame_id'),
        ),
    )
    for column in INDEXED:
        op.create_index(
            op.f(f'ix_{PROPERTIES}_{column}'),
            PROPERTIES,
            [column],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    for column in INDEXED:
        op.drop_index(op.f(f'ix_{PROPERTIES}_{column}'), table_name=PROPERTIES)
    op.drop_table(PROPERTIES)
    with op.batch_alter_table(TEMPLATES, schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f(f'ck_{TEMPLATES}_property_no_end_default'), type_='check'
        )
        batch_op.drop_constraint(
            op.f(f'ck_{TEMPLATES}_required_needs_property'), type_='check'
        )
        batch_op.drop_column('is_required')
        batch_op.drop_column('is_property')
