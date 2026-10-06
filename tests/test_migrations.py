import pytest
from sqlalchemy import inspect, text

from app.distributed.db import Database
from app.distributed.migrate import MIGRATIONS, upgrade


def legacy_database(tmp_path):
    database = Database(f'sqlite:///{tmp_path / "legacy.db"}')
    with database.engine.begin() as connection:
        for statement in (MIGRATIONS / '001_initial.sql').read_text().split(';'):
            if statement.strip():
                connection.exec_driver_sql(statement)
        connection.execute(text('INSERT INTO media_capacity VALUES (1)'))
        connection.execute(text('''INSERT INTO media_jobs
            (id, owner, payload, status, stage, created, publish_after, attempts, cancel_requested)
            VALUES ('existing', 'owner', '{}', 'queued', 'queued', 1, 0, 0, false)'''))
    return database


def test_populated_legacy_upgrade_preserves_jobs_and_backfills_admission(tmp_path):
    database = legacy_database(tmp_path)
    try:
        upgrade(database.engine)
        upgrade(database.engine)  # Repeat is safe and does not reset the tombstone.
        with database.engine.connect() as connection:
            assert connection.scalar(text('SELECT revision FROM media_schema_version')) == 2
            assert connection.scalar(text('SELECT id FROM media_jobs')) == 'existing'
            assert connection.scalar(text('SELECT cleanup_attempts FROM media_jobs')) == 0
            assert connection.scalar(text('SELECT id FROM media_admissions')) == 'existing'
    finally:
        database.engine.dispose()


def test_failed_migration_rolls_back_and_retries(tmp_path, monkeypatch):
    database = legacy_database(tmp_path)
    migrations = tmp_path / 'revisions'
    migrations.mkdir()
    (migrations / '002_failure.sql').write_text(
        'ALTER TABLE media_jobs ADD COLUMN cleanup_after FLOAT NOT NULL DEFAULT 0;'
        'INVALID SQL;'
    )
    monkeypatch.setattr('app.distributed.migrate.MIGRATIONS', migrations)
    with pytest.raises(Exception):
        upgrade(database.engine)
    assert 'cleanup_after' not in {c['name'] for c in inspect(database.engine).get_columns('media_jobs')}
    monkeypatch.setattr('app.distributed.migrate.MIGRATIONS', MIGRATIONS)
    upgrade(database.engine)
    database.engine.dispose()


def test_future_revision_is_rejected(tmp_path):
    database = Database(f'sqlite:///{tmp_path / "future.db"}')
    database.initialize()
    with database.engine.begin() as connection:
        connection.execute(text('UPDATE media_schema_version SET revision = 999'))
    with pytest.raises(RuntimeError, match='Unsupported schema revision'):
        database.initialize()
    database.engine.dispose()
