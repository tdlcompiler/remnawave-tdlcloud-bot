"""Store customer receipt email independently of the account email."""

import sqlalchemy as sa
from alembic import op

revision = '0141'
down_revision = '0140'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table in ('users', 'guest_purchases'):
        if 'receipt_email' not in {column['name'] for column in inspector.get_columns(table)}:
            op.add_column(table, sa.Column('receipt_email', sa.String(255), nullable=True))


def downgrade():
    op.drop_column('guest_purchases', 'receipt_email')
    op.drop_column('users', 'receipt_email')
