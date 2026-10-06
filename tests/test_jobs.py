import asyncio
import json
import sqlite3
from pathlib import Path
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.jobs import JobStore
from app.main import app

PAYLOAD = {'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'format': 'mp4', 'bitrate': 192}


def wait_status(client, job_id, statuses):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f'/jobs/{job_id}').json()
        if job['status'] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f'Job did not finish: {job}')


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr('app.main.shutil.which', lambda name: f'/usr/bin/{name}')
    with TestClient(app) as client:
        yield client


def test_job_stream_range_download_and_delete(client, monkeypatch):
    async def converter(payload, directory, *args):
        output = directory / 'video.mp4'
        output.write_bytes(b'0123456789' * 100)
        (directory / 'source.webm').write_bytes(b'source')
        (directory / 'stats.json').write_text(json.dumps({'cpu_seconds': 0.75, 'peak_process_memory_bytes': 123456}))
        return output

    monkeypatch.setattr('app.main.run_conversion', converter)
    response = client.post('/jobs', json=PAYLOAD)
    assert response.status_code == 202
    job = wait_status(client, response.json()['id'], {'completed', 'failed'})
    assert job['status'] == 'completed'
    assert job['size_bytes'] == 1000
    assert job['stats']['cpu_seconds'] == 0.75
    assert job['stats']['peak_process_memory_bytes'] == 123456
    assert job['stats']['output_bytes'] == 1000
    assert job['stats']['queue_seconds'] >= 0
    assert job['stats']['total_seconds'] >= job['stats']['processing_wall_seconds']
    persisted = JobStore(app.state.jobs.store.root, 3600, 20)
    assert persisted.public(persisted.get(job['id']))['stats'] == job['stats']
    partial = client.get(job['preview_url'], headers={'Range': 'bytes=10-19'})
    assert partial.status_code == 206
    assert partial.content == b'0123456789'
    assert partial.headers['content-range'] == 'bytes 10-19/1000'
    assert partial.headers['content-disposition'].startswith('inline')
    # Repeated ranges, preview, and download must not delete the result.
    assert client.get(job['preview_url'], headers={'Range': 'bytes=20-29'}).status_code == 206
    assert client.head(job['preview_url']).headers['content-length'] == '1000'
    download = client.get(job['download_url'])
    assert download.status_code == 200 and len(download.content) == 1000
    assert download.headers['content-disposition'].startswith('attachment')
    assert not app.state.jobs.pinned
    assert not (app.state.jobs.store.root / job['id'] / 'source.webm').exists()
    assert client.delete(f"/jobs/{job['id']}").status_code == 204
    assert client.get(job['preview_url']).status_code == 404


def test_job_failure_is_recorded(client, monkeypatch):
    async def converter(*args):
        raise HTTPException(502, 'YouTube blocked this request.', headers={'X-Error-Code': 'upstream_blocked'})
    monkeypatch.setattr('app.main.run_conversion', converter)
    job_id = client.post('/jobs', json=PAYLOAD).json()['id']
    job = wait_status(client, job_id, {'failed'})
    assert job['error_code'] == 'upstream_blocked'
    assert 'blocked' in job['error']
    assert client.get(f'/jobs/{job_id}/file').status_code == 409
    assert not (app.state.jobs.store.root / job_id).exists()


def test_running_job_cancellation(client, monkeypatch):
    cancelled = []
    async def converter(*args):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)
    monkeypatch.setattr('app.main.run_conversion', converter)
    job_id = client.post('/jobs', json=PAYLOAD).json()['id']
    wait_status(client, job_id, {'running'})
    response = client.post(f'/jobs/{job_id}/cancel')
    assert response.json()['status'] == 'cancelled'
    assert app.state.active == 0
    assert cancelled == [True]


def test_queue_capacity_and_idempotency(client, monkeypatch):
    app.state.active = app.state.limit  # Hold admission to the downloader.
    app.state.jobs.store.capacity = 1
    key = 'a' * 32
    first = client.post('/jobs', json=PAYLOAD, headers={'X-Idempotency-Key': key})
    repeated = client.post('/jobs', json=PAYLOAD, headers={'X-Idempotency-Key': key})
    assert first.status_code == repeated.status_code == 202
    assert first.json()['id'] == repeated.json()['id'] == key
    assert client.post('/jobs', json={**PAYLOAD, 'format': 'mp3'}, headers={'X-Idempotency-Key': key}).status_code == 409
    assert client.post('/jobs', json=PAYLOAD).status_code == 429
    assert client.post(f'/jobs/{key}/cancel').json()['status'] == 'cancelled'
    assert client.delete(f'/jobs/{key}').status_code == 204
    assert client.post('/jobs', json=PAYLOAD).status_code == 202


def test_completed_jobs_survive_restart_and_running_jobs_fail(tmp_path):
    root = tmp_path / 'retained'
    store = JobStore(root, retention=3600, capacity=20)
    completed = store.create(PAYLOAD)
    directory = root / completed['id']
    directory.mkdir()
    output = directory / 'video.mp4'
    output.write_bytes(b'media')
    store.finish(completed['id'], 'completed', output=output)
    running = store.create(PAYLOAD)
    assert store.claim()['id'] == running['id']
    queued = store.create(PAYLOAD)
    recovered = JobStore(root, retention=3600, capacity=20)
    recovered.recover()
    assert recovered.get(completed['id'])['status'] == 'completed'
    assert output.exists()
    assert recovered.get(running['id'])['error_code'] == 'server_restarted'
    assert recovered.get(queued['id'])['status'] == 'queued'


def test_expiration_respects_active_download(tmp_path):
    store = JobStore(tmp_path / 'expiration', retention=3600, capacity=20)
    job = store.create(PAYLOAD)
    directory = store.root / job['id']
    directory.mkdir()
    output = directory / 'video.mp4'
    output.write_bytes(b'media')
    store.finish(job['id'], 'completed', output=output)
    with store.connect() as connection:
        connection.execute('UPDATE jobs SET expires=? WHERE id=?', (time.time() - 1, job['id']))
    store.expire({job['id']: 1})
    assert output.exists()
    store.expire({})
    assert not output.exists()
    with pytest.raises(HTTPException):
        store.get(job['id'])


def test_invalid_job_ids_and_unknown_jobs(client):
    assert client.get('/jobs/../../etc/passwd/file').status_code == 404
    assert client.get('/jobs/not-a-uuid/file').status_code == 422
    assert client.get('/jobs/' + '0' * 32).status_code == 404


def test_existing_database_migrates_without_losing_jobs(tmp_path):
    root = tmp_path / 'old-database'
    root.mkdir()
    with sqlite3.connect(root / 'jobs.sqlite3') as connection:
        connection.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, expires REAL,
            error_code TEXT, error_message TEXT, filename TEXT, size INTEGER
        )""")
        connection.execute('INSERT INTO jobs (id,payload,status,created,updated) VALUES (?,?,?,?,?)',
                           ('a' * 32, json.dumps(PAYLOAD), 'completed', time.time(), time.time()))
    store = JobStore(root, 3600, 20)
    job = store.public(store.get('a' * 32))
    assert job['status'] == 'completed'
    assert job['stats'] is None
    # Migration can safely run again at subsequent startup.
    assert JobStore(root, 3600, 20).get('a' * 32)['id'] == 'a' * 32
