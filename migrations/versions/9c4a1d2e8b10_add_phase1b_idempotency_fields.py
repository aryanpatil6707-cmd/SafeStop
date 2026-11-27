"""add_phase1b_idempotency_fields

Revision ID: 9c4a1d2e8b10
Revises: efdc0b2f90fe
Create Date: 2026-11-27 20:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9c4a1d2e8b10'
down_revision = 'efdc0b2f90fe'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('attendances', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_event_id', sa.String(length=100), nullable=True))
        batch_op.create_index(batch_op.f('ix_attendances_client_event_id'), ['client_event_id'], unique=False)
        batch_op.create_unique_constraint(
            'uq_attendance_driver_client_event',
            ['verified_by_driver_id', 'client_event_id'],
        )

    with op.batch_alter_table('emergency_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('institution_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('client_event_id', sa.String(length=100), nullable=True))
        batch_op.create_index(batch_op.f('ix_emergency_events_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_emergency_events_client_event_id'), ['client_event_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_emergency_events_institution_id',
            'institutions',
            ['institution_id'],
            ['id'],
        )
        batch_op.create_unique_constraint(
            'uq_emergency_user_client_event',
            ['triggered_by_user_id', 'client_event_id'],
        )


def downgrade():
    with op.batch_alter_table('emergency_events', schema=None) as batch_op:
        batch_op.drop_constraint('uq_emergency_user_client_event', type_='unique')
        batch_op.drop_constraint('fk_emergency_events_institution_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_emergency_events_client_event_id'))
        batch_op.drop_index(batch_op.f('ix_emergency_events_institution_id'))
        batch_op.drop_column('client_event_id')
        batch_op.drop_column('institution_id')

    with op.batch_alter_table('attendances', schema=None) as batch_op:
        batch_op.drop_constraint('uq_attendance_driver_client_event', type_='unique')
        batch_op.drop_index(batch_op.f('ix_attendances_client_event_id'))
        batch_op.drop_column('client_event_id')
