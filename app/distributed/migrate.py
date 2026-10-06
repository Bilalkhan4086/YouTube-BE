"""Forward-only, transactional SQL revisions for PostgreSQL and disposable SQLite tests."""
from pathlib import Path
import time

from sqlalchemy import Boolean, Float, Integer, JSON, String, inspect, text
from sqlalchemy.engine import Engine

LATEST_REVISION = 2
MIGRATIONS = Path(__file__).with_name('migrations')
LEGACY_COLUMNS = {
    'media_capacity': {'id'},
    'media_jobs': {
        'id', 'owner', 'payload', 'status', 'stage', 'created', 'started', 'expires',
        'publish_after', 'lease_until', 'run_token', 'attempts', 'cancel_requested',
        'error_code', 'error', 'object_key', 'filename', 'size', 'stats',
    },
}


LEGACY_JOB_TYPES = {
    'id': (String, 32), 'owner': (String, 64), 'payload': (JSON, None),
    'status': (String, 20), 'stage': (String, 30), 'created': (Float, None),
    'started': (Float, None), 'expires': (Float, None), 'publish_after': (Float, None),
    'lease_until': (Float, None), 'run_token': (String, 32), 'attempts': (Integer, None),
    'cancel_requested': (Boolean, None), 'error_code': (String, 50), 'error': (String, None),
    'object_key': (String, None), 'filename': (String, 100), 'size': (Integer, None),
    'stats': (JSON, None),
}
LEGACY_NULLABLE = {
    'started', 'expires', 'lease_until', 'run_token', 'error_code', 'error',
    'object_key', 'filename', 'size', 'stats',
}


def upgrade(engine: Engine) -> None:
    """Serialize upgrades; reject unknown versions and partially initialized legacy schemas."""
    with engine.begin() as connection:
        if engine.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_advisory_xact_lock(734921806)'))
        elif engine.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            raise RuntimeError('Only PostgreSQL and SQLite migrations are supported')
        tables = set(inspect(connection).get_table_names())
        connection.exec_driver_sql(
            'CREATE TABLE IF NOT EXISTS media_schema_version '
            '(id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL)'
        )
        revision = connection.scalar(text('SELECT revision FROM media_schema_version WHERE id = 1'))
        if revision is None:
            present = tables & LEGACY_COLUMNS.keys()
            if present:
                if present != LEGACY_COLUMNS.keys():
                    raise RuntimeError('Incomplete legacy schema; restore or review before upgrading')
                for table, expected in LEGACY_COLUMNS.items():
                    columns = {column['name']: column for column in inspect(connection).get_columns(table)}
                    if columns.keys() != expected:
                        raise RuntimeError('Unrecognized legacy schema; review before upgrading')
                    specification = LEGACY_JOB_TYPES if table == 'media_jobs' else {'id': (Integer, None)}
                    for name, (kind, length) in specification.items():
                        column = columns[name]
                        nullable = table == 'media_jobs' and name in LEGACY_NULLABLE
                        if (not isinstance(column['type'], kind)
                                or column['nullable'] != nullable
                                or (length is not None and column['type'].length != length)):
                            raise RuntimeError('Unrecognized legacy column definition')
                    if inspect(connection).get_pk_constraint(table)['constrained_columns'] != ['id']:
                        raise RuntimeError('Unrecognized legacy primary key')
                revision = 1
            else:
                revision = 0
            connection.execute(text('INSERT INTO media_schema_version VALUES (1, :revision)'),
                               {'revision': revision})
        if revision not in range(LATEST_REVISION + 1):
            raise RuntimeError('Unsupported schema revision; use the matching application release')
        for number in range(revision + 1, LATEST_REVISION + 1):
            path, = MIGRATIONS.glob(f'{number:03d}_*.sql')
            for statement in path.read_text().split(';'):
                if statement.strip():
                    connection.exec_driver_sql(statement)
            if number == 2:
                # Preserve replay protection for pre-upgrade authorizations, even if a
                # terminal result is deleted immediately after this deployment.
                connection.execute(text(
                    'INSERT INTO media_admissions (id, owner, expires) '
                    'SELECT id, owner, :expires FROM media_jobs'
                ), {'expires': time.time() + 121})
            connection.execute(text('UPDATE media_schema_version SET revision = :revision WHERE id = 1'),
                               {'revision': number})
        connection.execute(text(
            'INSERT INTO media_capacity (id) SELECT 1 '
            'WHERE NOT EXISTS (SELECT 1 FROM media_capacity WHERE id = 1)'
        ))


def main() -> None:
    import os
    from app.distributed.db import Database

    database = Database(os.environ['DATABASE_URL'])
    try:
        upgrade(database.engine)
    finally:
        database.engine.dispose()
    print(f'Database upgraded to revision {LATEST_REVISION}.')


if __name__ == '__main__':
    main()
