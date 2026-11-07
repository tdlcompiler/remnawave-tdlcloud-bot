"""Store customer receipt email independently of the account email."""

import sqlalchemy as sa
from alembic import op

revision = '0141'
down_revision = '0140'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('receipt_email', sa.String(255), nullable=True))
    op.add_column('guest_purchases', sa.Column('receipt_email', sa.String(255), nullable=True))


def downgrade():
    op.drop_column('guest_purchases', 'receipt_email')
    op.drop_column('users', 'receipt_email')
