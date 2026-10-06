"""Opt-in real Redis/S3 tests against dedicated disposable services only."""
from concurrent.futures import ThreadPoolExecutor
import os
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import httpx
import pytest
from redis import Redis

from app.distributed.security import rate_limit
from app.distributed.storage import Storage


def test_real_redis_atomic_rate_limit():
    url = os.getenv('TEST_REDIS_URL')
    if not url or os.getenv('ALLOW_DISPOSABLE_SERVICE_TESTS') != '1':
        pytest.skip('Disposable Redis target not configured')
    broker = Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
    key = 'audit-test:' + uuid.uuid4().hex
    try:
        def attempt(_):
            try:
                rate_limit(broker, key, 5)
                return 200
            except HTTPException as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(20)))
        assert results.count(200) == 5
        assert results.count(429) == 15
        assert 0 < broker.ttl(key) <= 60
    finally:
        broker.delete(key)
        broker.close()


def test_real_private_s3_signed_get_head_and_ranges(tmp_path):
    endpoint = os.getenv('TEST_S3_ENDPOINT')
    if not endpoint or os.getenv('ALLOW_DISPOSABLE_SERVICE_TESTS') != '1':
        pytest.skip('Disposable S3 target not configured')
    settings = SimpleNamespace(
        s3_endpoint=endpoint, s3_public_endpoint=endpoint,
        s3_bucket='audit-test-' + uuid.uuid4().hex,
        s3_region='us-east-1', s3_access_key=os.environ['TEST_S3_ACCESS_KEY'],
        s3_secret_key=os.environ['TEST_S3_SECRET_KEY'],
    )
    storage = Storage(settings)
    key = 'media/fixture/audio.mp3'
    data = b'generated-test-media' * 100
    media = tmp_path / 'audio.mp3'
    media.write_bytes(data)
    storage.client.create_bucket(Bucket=settings.s3_bucket)
    try:
        storage.upload(media, key)
        with httpx.Client(timeout=10) as client:
            unsigned = f'{endpoint}/{settings.s3_bucket}/{key}'
            assert client.get(unsigned).status_code == 403
            signed = storage.url(key, media.name, True, 60)
            assert client.get(signed).content == data
            ranged = client.get(signed, headers={'Range': 'bytes=0-31'})
            assert ranged.status_code == 206 and ranged.content == data[:32]
            head = client.head(storage.url(key, media.name, False, 60, method='HEAD'))
            assert head.status_code == 200 and int(head.headers['content-length']) == len(data)
        storage.delete(key)
        storage.delete(key)  # Retrying cleanup after a lost response is safe.
    finally:
        storage.delete(key)
        storage.client.delete_bucket(Bucket=settings.s3_bucket)
        storage.close()
