"""Opt-in tests. TEST_POSTGRES_URL must point to an explicitly disposable database."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from app.distributed.api import create_app
from app.distributed.db import Database, Job
from app.distributed.dispatcher import cleanup_jobs, recover_leases
from app.distributed.migrate import MIGRATIONS
from app.distributed.security import Tokens, anonymous_subject
from app.distributed.worker import claim, finish


@pytest.fixture
def database():
    url = os.getenv('TEST_POSTGRES_URL')
    if not url or os.getenv('ALLOW_DISPOSABLE_POSTGRES_TESTS') != '1':
        pytest.skip('Explicit disposable PostgreSQL target not configured')
    database = Database(url)
    schema = 'audit_test_' + uuid.uuid4().hex
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA {schema}')
    database.engine.dispose()
    def namespace(connection, record):
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO {schema}')
        connection.commit()
    event.listen(database.engine, 'connect', namespace)
    try:
        yield database
    finally:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA {schema} CASCADE')
        database.engine.dispose()


@pytest.fixture
def settings():
    return SimpleNamespace(
        signing_secret='s' * 48, api_key='k' * 48, allow_anonymous=False,
        auth_mode='internal', issuer_secret='', conversion_base='', rate_per_minute=100,
        max_jobs=1, retention=3600, max_queue_wait=900, max_attempts=3,
        lease_seconds=60, timeout=600, max_duration=1800,
    )


class Broker:
    def eval(self, *args):
        return 1
    def ping(self):
        return True


def test_concurrent_migration_and_populated_legacy_upgrade(database):
    with database.engine.begin() as connection:
        for statement in (MIGRATIONS / '001_initial.sql').read_text().split(';'):
            if statement.strip():
                connection.exec_driver_sql(statement)
        connection.execute(text('''INSERT INTO media_jobs
            (id, owner, payload, status, stage, created, publish_after, attempts, cancel_requested)
            VALUES ('existing', 'owner', '{}', 'queued', 'queued', 1, 0, 0, false)'''))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: database.initialize(), range(2)))
    with database.sessions() as session:
        assert session.get(Job, 'existing').cleanup_attempts == 0
        assert session.scalar(text('SELECT count(*) FROM media_admissions')) == 1


@pytest.mark.parametrize("anonymous", [False, True])
def test_parallel_admission_cannot_exceed_capacity(database, settings, anonymous):
    database.initialize()
    tokens = Tokens(settings.signing_secret)
    payload = {'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'format': 'mp4', 'bitrate': 192}
    claims = {'owner': 'owner'}
    if anonymous:
        settings.auth_mode = 'anonymous'
        settings.max_jobs = 100
        settings.anonymous_max_active_jobs = 1
        settings.anonymous_jobs_per_hour = 100
        claims = {'owner': anonymous_subject('198.51.100.1', settings.signing_secret),
                  'mode': 'anonymous'}
    grants = [tokens.issue('convert', {'id': uuid.uuid4().hex, **claims, 'payload': payload})
              for _ in range(8)]
    barrier = threading.Barrier(len(grants))
    with TestClient(create_app(settings, database, Broker(), object()),
                    client=('198.51.100.1', 1234)) as client:
        def submit(token):
            barrier.wait()
            return client.post('/api/v1/convert', params={'sig': token}).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, grants))
    assert results.count(202) == 1
    assert results.count(429) == 7


def test_duplicate_claims_and_stale_completion(database, settings):
    database.initialize()
    with database.sessions.begin() as session:
        session.add(Job(id='job', owner='owner', payload={}))
    barrier = threading.Barrier(4)
    def attempt(_):
        barrier.wait()
        return claim(database, settings, 'job')
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [job for job in pool.map(attempt, range(4)) if job]
    assert len(jobs) == 1
    with database.sessions.begin() as session:
        session.get(Job, 'job').lease_until = time.time() - 1
    recover_leases(database, settings)
    current = claim(database, settings, 'job')
    assert not finish(database, settings, jobs[0], 'completed', object_key='stale')
    assert finish(database, settings, current, 'completed', object_key='current')


def test_cleanup_network_call_does_not_hold_job_lock(database):
    database.initialize()
    with database.sessions.begin() as session:
        session.add(Job(id='job', owner='owner', payload={}, status='completed',
                        expires=time.time() - 1, object_key='object'))
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Storage:
        def delete(self, key):
            calls.append(key)
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cleanup_jobs, database, Storage())
        assert entered.wait(5)
        try:
            # NOWAIT fails immediately if cleanup still holds the row lock during S3.
            with database.sessions.begin() as session:
                assert session.get(Job, 'job', with_for_update={'nowait': True})
            cleanup_jobs(database, Storage())  # Reservation prevents duplicate cleanup.
        finally:
            release.set()
        first.result()
    assert calls == ['object']
