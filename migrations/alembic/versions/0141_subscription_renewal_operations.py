"""Persist renewal responses atomically with balance charges.

Revision ID: 0142
Revises: 0141
"""

import sqlalchemy as sa
from alembic import op


revision = '0142'
down_revision = '0141'
branch_labels = None
depends_on = None


def upgrade():
    # Both the local receipt-email migration and upstream renewal migration
    # were published as 0141. The DB version alone cannot identify which ran.
    inspector = sa.inspect(op.get_bind())
    for table in ('users', 'guest_purchases'):
        if 'receipt_email' not in {column['name'] for column in inspector.get_columns(table)}:
            op.add_column(table, sa.Column('receipt_email', sa.String(255), nullable=True))
    if inspector.has_table('subscription_renewal_operations'):
        return
    op.create_table(
        'subscription_renewal_operations',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('operation_id', sa.String(36), primary_key=True),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('period_days', sa.Integer(), nullable=False),
        sa.Column('expected_end_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('subscription_renewal_operations')
