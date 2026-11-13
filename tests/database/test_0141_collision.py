"""The colliding 0141 revisions may each already exist in deployed databases."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATIONS = Path(__file__).resolve().parents[2] / 'migrations' / 'alembic' / 'versions'


def load(name):
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('receipt_columns', [False, True])
@pytest.mark.parametrize('renewal_table', [False, True])
def test_0141_collision_is_reconciled_without_data_loss(receipt_columns, renewal_table):
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY, balance INTEGER)')
        connection.exec_driver_sql('CREATE TABLE guest_purchases (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('INSERT INTO users (id, balance) VALUES (1, 500)')
        if receipt_columns:
            connection.exec_driver_sql('ALTER TABLE users ADD COLUMN receipt_email VARCHAR(255)')
            connection.exec_driver_sql('ALTER TABLE guest_purchases ADD COLUMN receipt_email VARCHAR(255)')
            connection.exec_driver_sql("UPDATE users SET receipt_email = 'test@example.com'")
        if renewal_table:
            connection.exec_driver_sql('CREATE TABLE subscription_renewal_operations (user_id INTEGER)')
            connection.exec_driver_sql('INSERT INTO subscription_renewal_operations VALUES (1)')
        with Operations.context(MigrationContext.configure(connection)):
            load('0141_subscription_renewal_operations').upgrade()
        inspector = sa.inspect(connection)
        assert inspector.has_table('subscription_renewal_operations')
        for table in ('users', 'guest_purchases'):
            assert 'receipt_email' in {col['name'] for col in inspector.get_columns(table)}
        assert connection.exec_driver_sql('SELECT balance FROM users WHERE id=1').scalar_one() == 500
        if receipt_columns:
            assert connection.exec_driver_sql('SELECT receipt_email FROM users WHERE id=1').scalar_one() == 'test@example.com'
        if renewal_table:
            assert connection.exec_driver_sql('SELECT count(*) FROM subscription_renewal_operations').scalar_one() == 1
