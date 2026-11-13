"""Safely transition the fork revision that was previously deployed as 0142."""
import pytest
import sqlalchemy as sa

from app.database.migration_0142_transition import transition_legacy_revision


@pytest.fixture
def engine():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)')
        conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('0142')")
        conn.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY, receipt_email VARCHAR(255), balance INTEGER)')
        conn.exec_driver_sql('CREATE TABLE guest_purchases (id INTEGER PRIMARY KEY, receipt_email VARCHAR(255))')
        conn.exec_driver_sql("INSERT INTO users VALUES (1, 'receipt@example.com', 12345)")
        conn.exec_driver_sql('''CREATE TABLE subscription_renewal_operations (
            user_id INTEGER NOT NULL, operation_id VARCHAR(36) NOT NULL,
            subscription_id INTEGER NOT NULL, period_days INTEGER NOT NULL,
            expected_end_date TIMESTAMP NOT NULL, response JSON NOT NULL,
            created_at TIMESTAMP NOT NULL, PRIMARY KEY (user_id, operation_id))''')
        conn.exec_driver_sql("""INSERT INTO subscription_renewal_operations VALUES
            (1, 'operation-1', 1, 30, '2026-11-01', '{}', '2026-11-01')""")
    return engine


def test_deployed_0142_is_transitioned_once_and_data_is_preserved(engine):
    with engine.begin() as conn:
        assert transition_legacy_revision(conn, False)
        assert not transition_legacy_revision(conn, False)
        assert conn.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one() == 'tdl_renewal_01'
        assert conn.exec_driver_sql('SELECT balance, receipt_email FROM users').one() == (12345, 'receipt@example.com')
        assert conn.exec_driver_sql('SELECT operation_id FROM subscription_renewal_operations').scalar_one() == 'operation-1'


def test_incomplete_legacy_schema_does_not_change_revision(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql('DROP TABLE subscription_renewal_operations')
        with pytest.raises(RuntimeError, match='table is missing'):
            transition_legacy_revision(conn, False)
        assert conn.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one() == '0142'


def test_future_upstream_0142_is_not_rewritten(engine):
    with engine.begin() as conn:
        assert not transition_legacy_revision(conn, True)
        assert conn.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one() == '0142'


def test_no_version_table_is_untouched():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        assert not transition_legacy_revision(conn, False)
        assert not sa.inspect(conn).has_table('alembic_version')
