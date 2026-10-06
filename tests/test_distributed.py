from types import SimpleNamespace
import asyncio
import json
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.distributed.api import create_app
from app.distributed.db import Database, Job
from app.distributed.dispatcher import dispatch_once
from app.distributed.security import Tokens
from app.distributed.worker import claim, finish
from app.distributed import worker as worker_module


class Broker:
    def __init__(self):
        self.counts, self.messages = {}, []
        self.values = {}
    def eval(self, script, count, key, seconds):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]
    def rpush(self, queue, job_id):
        self.messages.append(job_id)
    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True
    def get(self, key):
        return self.values.get(key)
    def expire(self, key, seconds):
        return key in self.values
    def ping(self):
        return True


class Storage:
    def __init__(self):
        self.deleted = []
    def url(self, key, filename, download, ttl, method="GET"):
        return f'https://storage.example/{key}?signed=1'
    def delete(self, key):
        if key:
            self.deleted.append(key)


@pytest.fixture
def services(tmp_path):
    config = SimpleNamespace(
        signing_secret='s' * 48, api_key='k' * 48, allow_anonymous=False,
        auth_mode='internal', issuer_secret='i' * 48,
        conversion_base='', rate_per_minute=10, max_jobs=10, retention=3600,
        max_queue_wait=900, max_attempts=3,
        lease_seconds=60, timeout=600, max_duration=1800,
    )
    db = Database(f'sqlite:///{tmp_path / "cloud.db"}')
    db.initialize()
    broker, storage = Broker(), Storage()
    with TestClient(create_app(config, db, broker, storage)) as client:
        yield config, db, broker, storage, client
    db.engine.dispose()


def submit(client, key='k' * 48):
    auth = client.post('/api/v1/auth', json={'api_key': key})
    assert auth.status_code == 200
    initialized = client.post('/api/v1/init', headers={'Authorization': 'Bearer ' + auth.json()['key']},
                              json={'url': 'https://youtu.be/abcdefghijk', 'format': 'mp4'})
    assert initialized.status_code == 200
    response = client.post(initialized.json()['convertURL'])
    assert response.status_code == 202
    return initialized.json()['convertURL'], response.json()


def test_auth_signed_flow_outbox_and_direct_download(services):
    config, db, broker, storage, client = services
    convert_url, job = submit(client)
    assert client.post(convert_url).json()['id'] == job['id']  # Idempotent signed authorization.
    assert client.get(job['downloadURL'], follow_redirects=False).status_code == 409
    assert broker.messages == []  # Committed job is the durable outbox.
    dispatch_once(db, broker, storage, config)
    assert broker.messages == [job['id']]
    first = claim(db, config, job['id'])
    assert first and claim(db, config, job['id']) is None
    assert finish(db, config, first, 'completed', object_key='media/test/video.mp4',
                  filename='video.mp4', size=500, stats={'cpu_seconds': 1.5})
    progress = client.get(job['progressURL']).json()
    assert progress['progress'] == 100 and progress['stats']['cpu_seconds'] == 1.5
    download = client.get(job['downloadURL'], follow_redirects=False)
    assert download.status_code == 307
    assert download.headers['location'].startswith('https://storage.example/')
    assert download.content == b''  # API is not proxying media bytes.


def test_tokens_reject_tamper_wrong_purpose_and_expiry():
    tokens = Tokens('secret' * 8)
    signed = tokens.issue('job', {'job': 'abc'})
    assert tokens.read('job', signed, 60)['job'] == 'abc'
    for purpose, token, ttl in [('convert', signed, 60), ('job', signed + 'x', 60), ('job', signed, -1)]:
        with pytest.raises(HTTPException) as error:
            tokens.read(purpose, token, ttl)
        assert error.value.status_code == 401


def test_invalid_key_and_missing_authorization(services):
    *_, client = services
    assert client.post('/api/v1/auth', json={'api_key': 'wrong'}).status_code == 401
    assert client.post('/api/v1/init', json={'url': 'https://youtu.be/abcdefghijk'}).status_code == 401
    assert client.post('/api/v1/convert?sig=bad').status_code == 401


def test_broker_outage_does_not_lose_committed_job(services, monkeypatch):
    config, db, broker, storage, client = services
    _, job = submit(client)
    original = broker.rpush
    monkeypatch.setattr(broker, 'rpush', lambda *args: (_ for _ in ()).throw(ConnectionError('offline')))
    with pytest.raises(ConnectionError):
        dispatch_once(db, broker, storage, config)
    with db.sessions() as session:
        record = session.get(Job, job['id'])
        assert record.status == 'queued' and record.publish_after == 0
    monkeypatch.setattr(broker, 'rpush', original)
    dispatch_once(db, broker, storage, config)
    assert broker.messages == [job['id']]


def test_worker_lease_recovery_and_fencing(services):
    config, db, broker, storage, client = services
    _, job = submit(client)
    stale = claim(db, config, job['id'])
    with db.sessions.begin() as session:
        session.get(Job, job['id']).lease_until = time.time() - 1
    dispatch_once(db, broker, storage, config)
    current = claim(db, config, job['id'])
    assert current.run_token != stale.run_token
    assert not finish(db, config, stale, 'completed', object_key='stale')
    assert finish(db, config, current, 'completed', object_key='fresh')


def test_expiry_deletes_storage_and_blocks_download(services):
    config, db, broker, storage, client = services
    _, job = submit(client)
    worker = claim(db, config, job['id'])
    finish(db, config, worker, 'completed', object_key='media/expired', filename='video.mp4')
    with db.sessions.begin() as session:
        session.get(Job, job['id']).expires = time.time() - 1
    assert client.get(job['downloadURL']).status_code == 410
    dispatch_once(db, broker, storage, config)
    assert storage.deleted == ['media/expired']
    assert client.get(job['progressURL']).status_code == 404


def test_rate_limit_and_cancellation(services):
    config, db, broker, storage, client = services
    _, job = submit(client)
    assert client.post(job['cancelURL']).status_code == 200
    assert client.get(job['progressURL']).json()['status'] == 'cancelled'
    auth = client.post('/api/v1/auth', json={'api_key': config.api_key}).json()['key']
    config.rate_per_minute = 1
    response = client.post('/api/v1/init', json={'url': 'https://youtu.be/abcdefghijk'},
                            headers={'Authorization': 'Bearer ' + auth})
    assert response.status_code == 429


@pytest.mark.parametrize('upload_failure', [False, True])
def test_worker_publishes_only_after_upload(services, monkeypatch, upload_failure):
    config, db, broker, storage, client = services
    _, job = submit(client)
    claimed = claim(db, config, job['id'])

    async def convert(payload, directory, timeout, duration):
        output = directory / 'video.mp4'
        output.write_bytes(b'fixture')
        (directory / 'stats.json').write_text(json.dumps({'cpu_seconds': 1.5}))
        return output

    def upload(path, key, cancelled):
        with db.sessions() as session:
            record = session.get(Job, job['id'])
            assert record.status == 'running' and record.object_key is None
        if upload_failure:
            raise ConnectionError('Storage unavailable')

    monkeypatch.setattr(worker_module, 'run_conversion', convert)
    monkeypatch.setattr(storage, 'upload', upload, raising=False)
    asyncio.run(worker_module.execute(db, storage, config, claimed))
    with db.sessions() as session:
        record = session.get(Job, job['id'])
        if upload_failure:
            assert record.status == 'running' and record.object_key is None
            assert storage.deleted  # Partial attempt cleaned, lease enables a bounded retry.
        else:
            assert record.status == 'completed'
            assert claimed.run_token in record.object_key
            assert record.stats['upload_seconds'] >= 0
            assert record.size == len(b'fixture')
            assert not storage.deleted


def test_worker_cancellation_stops_conversion_before_publish(services, monkeypatch):
    config, db, broker, storage, client = services
    _, job = submit(client)
    claimed = claim(db, config, job['id'])
    assert client.post(job['cancelURL']).status_code == 200
    stopped = []

    async def convert(*args):
        try:
            await asyncio.sleep(30)
        finally:
            stopped.append(True)

    monkeypatch.setattr(worker_module, 'run_conversion', convert)
    asyncio.run(worker_module.execute(db, storage, config, claimed))
    assert stopped
    with db.sessions() as session:
        record = session.get(Job, job['id'])
        assert record.status == 'cancelled' and record.object_key is None


def test_config_exposes_actual_duration_limit(services):
    config, _, _, _, client = services
    config.max_duration = 7200
    assert client.get('/api/v1/config').json()['max_duration_seconds'] == 7200


def test_worker_rejects_expired_queue_message_without_dispatcher(services):
    config, db, broker, storage, client = services
    _, job = submit(client)
    with db.sessions.begin() as session:
        session.get(Job, job['id']).created = time.time() - config.max_queue_wait - 1
    assert claim(db, config, job['id']) is None
    with db.sessions() as session:
        record = session.get(Job, job['id'])
        assert record.status == 'failed'
        assert record.error_code == 'queue_timeout'
        assert record.attempts == 0
        assert record.expires > time.time()


def test_non_ascii_api_key_is_rejected_without_server_error(services):
    *_, client = services
    response = client.post('/api/v1/auth', json={'api_key': 'invalid-🔑'})
    assert response.status_code == 401


@pytest.mark.parametrize('method,url_field', [('POST', 'cancelURL'), ('DELETE', 'deleteURL')])
def test_mutation_handles_record_removed_before_lock(services, monkeypatch, method, url_field):
    from sqlalchemy.orm import Session

    config, db, broker, storage, client = services
    _, job = submit(client)
    original_get = Session.get
    removed = []

    def get(session, entity, ident, **kwargs):
        if entity is Job and kwargs.get('with_for_update') and not removed:
            # Model cleanup committing between request validation and locked access.
            with db.sessions.begin() as cleanup:
                cleanup.delete(original_get(cleanup, Job, ident))
            removed.append(True)
        return original_get(session, entity, ident, **kwargs)

    monkeypatch.setattr(Session, 'get', get)
    assert client.request(method, job[url_field]).status_code == 404
    assert removed


@pytest.mark.parametrize('method,url_field', [('POST', 'cancelURL'), ('DELETE', 'deleteURL')])
def test_mutation_rejects_expired_job(services, method, url_field):
    config, db, broker, storage, client = services
    _, job = submit(client)
    with db.sessions.begin() as session:
        session.get(Job, job['id']).expires = time.time() - 1
    assert client.request(method, job[url_field]).status_code == 410


def test_cleanup_runs_during_broker_outage(services, monkeypatch):
    config, db, broker, storage, client = services
    _, expired = submit(client)
    first = claim(db, config, expired['id'])
    finish(db, config, first, 'completed', object_key='expired')
    with db.sessions.begin() as session:
        session.get(Job, first.id).expires = time.time() - 1
    _, pending = submit(client)
    def offline(*args):
        raise ConnectionError('offline')
    monkeypatch.setattr(broker, 'rpush', offline)
    with pytest.raises(ConnectionError):
        dispatch_once(db, broker, storage, config)
    with db.sessions() as session:
        assert session.get(Job, first.id) is None
        assert session.get(Job, pending['id']).status == 'queued'
    assert storage.deleted == ['expired']


def test_cleanup_failure_isolated_and_backed_off(services, monkeypatch):
    config, db, broker, storage, client = services
    ids = []
    for key in ('broken', 'healthy'):
        _, response = submit(client)
        job = claim(db, config, response['id'])
        finish(db, config, job, 'completed', object_key=key)
        with db.sessions.begin() as session:
            session.get(Job, job.id).expires = time.time() - 1
        ids.append(job.id)
    calls = []
    def delete(key):
        calls.append(key)
        if key == 'broken':
            raise ConnectionError('offline')
    monkeypatch.setattr(storage, 'delete', delete)
    dispatch_once(db, broker, storage, config)
    with db.sessions() as session:
        failed = session.get(Job, ids[0])
        assert failed.cleanup_attempts == 1 and failed.cleanup_after > time.time()
        assert session.get(Job, ids[1]) is None
    dispatch_once(db, broker, storage, config)
    assert calls.count('broken') == 1


def test_deleted_job_cannot_be_resubmitted_with_same_grant(services):
    config, db, broker, storage, client = services
    url, job = submit(client)
    assert client.post(job['cancelURL']).status_code == 200
    assert client.delete(job['deleteURL']).status_code == 204
    dispatch_once(db, broker, storage, config)
    assert client.post(url).status_code == 410
    with db.sessions() as session:
        assert session.get(Job, job['id']) is None


def test_job_capability_uses_database_expiry_after_long_recovery(services, monkeypatch):
    from itsdangerous.timed import TimestampSigner

    config, db, broker, storage, client = services
    url, job = submit(client)
    clock = TimestampSigner.get_timestamp
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: clock(self) + 100000)
    # Pending jobs remain owned/access-controlled even beyond the old derived timeout.
    assert client.get(job['progressURL']).status_code == 200
    with db.sessions.begin() as session:
        session.get(Job, job['id']).expires = time.time() - 1
    assert client.get(job['progressURL']).status_code == 410
    assert client.post(url).status_code == 401  # Conversion grant still expires normally.


def assertion(config, user, purpose='identity-session'):
    import uuid
    return Tokens(config.issuer_secret).issue(purpose, {'sub': user, 'nonce': str(uuid.uuid4())})


def test_identity_exchange_quotas_replay_and_revocation(services):
    config, db, broker, storage, client = services
    config.auth_mode = 'identity'
    config.rate_per_minute = 1
    signed = assertion(config, 'alice')
    alice = client.post('/api/v1/auth', json={'assertion': signed}).json()['key']
    assert client.post('/api/v1/auth', json={'assertion': signed}).status_code == 401
    bob = client.post('/api/v1/auth', json={'assertion': assertion(config, 'bob')}).json()['key']
    payload = {'url': 'https://youtu.be/abcdefghijk'}
    def initialize(token):
        return client.post('/api/v1/init', json=payload,
                           headers={'Authorization': 'Bearer ' + token})
    alice_grant = initialize(alice)
    assert alice_grant.status_code == 200
    assert initialize(alice).status_code == 429
    assert initialize(bob).status_code == 200  # Per-user quota, independent of shared key.
    revocation = assertion(config, 'alice', 'identity-revoke')
    assert client.post('/api/v1/auth/revoke', json={'assertion': revocation}).status_code == 204
    assert initialize(alice).status_code == 401
    assert client.post(alice_grant.json()['convertURL']).status_code == 401
    assert client.post('/api/v1/auth', json={'api_key': config.api_key}).status_code == 401


def test_identity_assertion_purpose_expiry_and_tampering(services, monkeypatch):
    from itsdangerous.timed import TimestampSigner

    config, *_, client = services
    config.auth_mode = 'identity'
    token = assertion(config, 'alice')
    assert client.post('/api/v1/auth', json={'assertion': token + 'x'}).status_code == 401
    assert client.post('/api/v1/auth/revoke', json={'assertion': token}).status_code == 401
    clock = TimestampSigner.get_timestamp
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: clock(self) + 61)
    assert client.post('/api/v1/auth', json={'assertion': token}).status_code == 401


def test_shutdown_cancels_active_conversion_and_cleans_directory(services, monkeypatch):
    config, db, broker, storage, client = services
    _, job = submit(client)
    directories = []
    async def scenario():
        stopping = asyncio.Event()
        def pop(*args, **kwargs):
            return (b'queue', job['id'].encode())
        async def convert(payload, directory, *args):
            directories.append(directory)
            stopping.set()
            await asyncio.sleep(30)
        monkeypatch.setattr(broker, 'blpop', pop, raising=False)
        monkeypatch.setattr(worker_module, 'run_conversion', convert)
        await asyncio.wait_for(worker_module.serve(db, broker, storage, config, stopping), 3)
    asyncio.run(scenario())
    assert directories and not directories[0].exists()
    with db.sessions() as session:
        record = session.get(Job, job['id'])
        assert record.status == 'running'  # Interrupted attempt remains lease-recoverable.
        assert record.object_key is None


def test_readiness_rejects_old_schema_but_liveness_stays_up(services):
    from sqlalchemy import text

    _, db, _, _, client = services
    assert client.get('/health').status_code == 200
    with db.sessions.begin() as session:
        session.execute(text('UPDATE media_schema_version SET revision = 1'))
    assert client.get('/health').status_code == 503
    assert client.get('/live').status_code == 200


def test_shutdown_during_upload_waits_before_deleting_object(services, monkeypatch):
    import threading

    config, db, broker, storage, client = services
    _, response = submit(client)
    job = claim(db, config, response['id'])
    uploading = threading.Event()
    finished = threading.Event()
    calls = []
    async def convert(payload, directory, *args):
        path = directory / 'video.mp4'
        path.write_bytes(b'fixture')
        (directory / 'stats.json').write_text('{}')
        return path
    def upload(path, key, cancelled):
        uploading.set()
        assert cancelled.wait(3)
        assert path.exists()
        finished.set()
        raise RuntimeError('cancelled')
    def delete(key):
        assert finished.is_set()
        calls.append(key)
    async def scenario():
        work = asyncio.create_task(worker_module.execute(db, storage, config, job))
        assert await asyncio.to_thread(uploading.wait, 3)
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    monkeypatch.setattr(worker_module, 'run_conversion', convert)
    monkeypatch.setattr(storage, 'upload', upload, raising=False)
    monkeypatch.setattr(storage, 'delete', delete)
    asyncio.run(scenario())
    assert len(calls) == 1
    with db.sessions() as session:
        assert session.get(Job, job.id).status == 'running'


def test_sigterm_exits_worker_through_cleanup(tmp_path):
    from pathlib import Path
    import signal
    import subprocess
    import sys

    script = '''
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from app.distributed.db import Database, Job
from app.distributed import worker
root = Path(sys.argv[1])
db = Database('sqlite:///' + str(root / 'signal.db'))
db.initialize()
with db.sessions.begin() as session:
    session.add(Job(id='signal-test', owner='owner', payload={
        'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'format': 'mp4', 'bitrate': 192}))
class Broker:
    def blpop(self, *args, **kwargs):
        return b'queue', b'signal-test'
async def convert(payload, directory, *args):
    (root / 'ready').write_text(str(directory))
    await asyncio.sleep(60)
worker.run_conversion = convert
settings = SimpleNamespace(max_queue_wait=900, lease_seconds=60, timeout=600,
                           max_duration=1800, retention=3600)
asyncio.run(worker.run_worker(db, Broker(), object(), settings))
db.engine.dispose()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        ready = tmp_path / 'ready'
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), 'Worker did not begin processing'
        directory = Path(ready.read_text())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode()
        assert not directory.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.fixture
def anonymous_services(services):
    config, db, broker, storage, _ = services
    config.auth_mode = 'anonymous'
    config.anonymous_jobs_per_hour = 5
    config.anonymous_max_active_jobs = 2
    with TestClient(create_app(config, db, broker, storage), client=('198.51.100.1', 1234)) as client:
        yield config, db, broker, storage, client


def anonymous_grant(client):
    auth = client.post('/api/v1/auth', json={})
    assert auth.status_code == 200
    response = client.post('/api/v1/init', headers={'Authorization': 'Bearer ' + auth.json()['key']},
                           json={'url': 'https://youtu.be/abcdefghijk'})
    assert response.status_code == 200
    return response.json()['convertURL']


def test_anonymous_active_limit_and_idempotency(anonymous_services):
    config, db, broker, storage, client = anonymous_services
    config.anonymous_max_active_jobs = 1
    grant = anonymous_grant(client)
    job = client.post(grant).json()
    assert client.post(grant).status_code == 202
    next_grant = anonymous_grant(client)
    assert client.post(next_grant).status_code == 429
    assert client.post(job['cancelURL']).status_code == 200
    assert client.post(next_grant).status_code == 202
    assert client.get('/api/v1/config').json()['requires_api_key'] is False


def test_anonymous_hour_quota_survives_new_sessions(anonymous_services):
    config, db, broker, storage, client = anonymous_services
    config.anonymous_jobs_per_hour = 1
    job = client.post(anonymous_grant(client)).json()
    client.post(job['cancelURL'])
    client.delete(job['deleteURL'])
    dispatch_once(db, broker, storage, config)
    response = client.post(anonymous_grant(client))
    assert response.status_code == 429
    assert response.headers['Retry-After'] == '3600'
    with TestClient(create_app(config, db, broker, storage), client=('198.51.100.2', 1234)) as other:
        assert other.post(anonymous_grant(other)).status_code == 202


def test_anonymous_ip_binding_and_spoofed_headers(anonymous_services):
    config, db, broker, storage, client = anonymous_services
    auth = client.post('/api/v1/auth', json={}).json()['key']
    grant = anonymous_grant(client)
    job = client.post(grant).json()
    with TestClient(create_app(config, db, broker, storage), client=('198.51.100.2', 1234)) as other:
        spoofed = {'X-Forwarded-For': '198.51.100.1', 'CF-Connecting-IP': '198.51.100.1'}
        assert other.post(grant, headers=spoofed).status_code == 401
        assert other.post('/api/v1/init', json={'url': 'https://youtu.be/abcdefghijk'},
                          headers={**spoofed, 'Authorization': 'Bearer ' + auth}).status_code == 401
        assert other.get(job['progressURL']).status_code == 200
        assert other.get('/api/v1/progress?sig=invalid').status_code == 401


def test_anonymous_redis_outage_fails_closed(anonymous_services, monkeypatch):
    from redis.exceptions import ConnectionError
    _, _, broker, _, client = anonymous_services
    grant = anonymous_grant(client)
    def offline(*args):
        raise ConnectionError('offline')
    monkeypatch.setattr(broker, 'eval', offline)
    assert client.post('/api/v1/auth', json={}).status_code == 503
    assert client.post(grant).status_code == 503


def test_anonymous_ipv6_normalization():
    from app.distributed.security import anonymous_subject
    secret = 's' * 48
    assert anonymous_subject('2001:db8::1', secret) == anonymous_subject('2001:db8::2', secret)
    assert anonymous_subject('2001:db8::1', secret) != anonymous_subject('2001:db8:0:1::1', secret)
    assert anonymous_subject('::ffff:198.51.100.1', secret) == anonymous_subject('198.51.100.1', secret)
    with pytest.raises(HTTPException):
        anonymous_subject('unknown', secret)
