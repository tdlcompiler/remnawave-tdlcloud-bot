"""One-time transition for the fork's already-deployed numeric revision 0142.

This bridge is only unambiguous before an upstream revision named 0142 exists.
Deploy it to all installations before merging such an upstream revision.
"""
from __future__ import annotations

import structlog
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


logger = structlog.get_logger(__name__)
LEGACY_REVISION = '0142'
FORK_REVISION = 'tdl_renewal_01'
RENEWAL_COLUMNS = {
    'user_id', 'operation_id', 'subscription_id', 'period_days', 'expected_end_date', 'response', 'created_at',
}


def transition_legacy_revision(connection: Connection, numeric_revision_exists: bool) -> bool:
    """Convert only a proven local 0142; the caller owns the transaction."""
    if numeric_revision_exists:
        # A future upstream 0142 is a distinct revision; never rewrite it.
        return False
    inspector = inspect(connection)
    if not inspector.has_table('alembic_version'):
        return False
    if connection.dialect.name == 'postgresql':
        connection.execute(text('LOCK TABLE alembic_version IN EXCLUSIVE MODE'))
    versions = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
    if LEGACY_REVISION not in versions:
        return False
    if versions != [LEGACY_REVISION]:
        raise RuntimeError('Cannot safely transition legacy 0142 with multiple Alembic revisions')

    for table in ('users', 'guest_purchases'):
        if not inspector.has_table(table) or 'receipt_email' not in {
            col['name'] for col in inspector.get_columns(table)
        }:
            raise RuntimeError(f'Cannot safely transition legacy 0142: {table}.receipt_email is missing')
    table = 'subscription_renewal_operations'
    if not inspector.has_table(table):
        raise RuntimeError('Cannot safely transition legacy 0142: renewal operations table is missing')
    columns = {column['name']: column for column in inspector.get_columns(table)}
    if not RENEWAL_COLUMNS.issubset(columns):
        raise RuntimeError('Cannot safely transition legacy 0142: renewal operations columns are incomplete')
    pk = set(inspector.get_pk_constraint(table).get('constrained_columns') or ())
    if pk != {'user_id', 'operation_id'}:
        raise RuntimeError('Cannot safely transition legacy 0142: renewal operations primary key differs')
    result = connection.execute(
        text('UPDATE alembic_version SET version_num = :new WHERE version_num = :old'),
        {'new': FORK_REVISION, 'old': LEGACY_REVISION},
    )
    if result.rowcount != 1:
        raise RuntimeError('Cannot safely transition legacy 0142: version changed concurrently')
    logger.info('Alembic: migrated legacy fork revision', old_revision=LEGACY_REVISION, new_revision=FORK_REVISION)
    return True
