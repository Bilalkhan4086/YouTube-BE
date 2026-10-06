from dataclasses import replace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.distributed.config import Settings


@pytest.fixture
def config_env(monkeypatch):
    values = {
        'SIGNING_SECRET': 's' * 48, 'CLIENT_API_KEY': 'k' * 48,
        'DATABASE_URL': 'sqlite://', 'REDIS_URL': 'redis://unused',
        'S3_BUCKET': 'test', 'S3_ACCESS_KEY': 'test', 'S3_SECRET_KEY': 'test',
        'DEV_ALLOW_ANONYMOUS': 'false', 'AUTH_MODE': 'internal', 'APP_ENV': 'development',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_production_rejects_internal_mode(config_env, monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    with pytest.raises(ValueError, match='Production requires'):
        Settings.load()
    monkeypatch.setenv('AUTH_MODE', 'identity')
    monkeypatch.setenv('SESSION_ISSUER_SECRET', 'i' * 48)
    assert Settings.load().auth_mode == 'identity'
    monkeypatch.setenv('DEV_ALLOW_ANONYMOUS', 'true')
    with pytest.raises(ValueError, match='no anonymous'):
        Settings.load()


def test_worker_does_not_need_api_signing_secrets(config_env, monkeypatch):
    monkeypatch.delenv('SIGNING_SECRET')
    monkeypatch.delenv('CLIENT_API_KEY')
    assert Settings.load('worker').signing_secret == ''


def test_lifecycle_outlasts_long_retention(config_env):
    settings = replace(Settings.load(), retention=10 * 86400)
    assert settings.lifecycle_days * 86400 > settings.retention + settings.timeout + 120


def test_only_edge_can_supply_client_address():
    app = FastAPI()
    @app.get('/')
    def address(request: Request):
        return {'address': request.client.host}
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=['172.29.247.2'])
    with TestClient(wrapped, client=('172.29.247.2', 1234)) as edge:
        assert edge.get('/', headers={'X-Forwarded-For': '198.51.100.1'}).json()['address'] == '198.51.100.1'
        assert edge.get('/', headers={'X-Forwarded-For': '198.51.100.2'}).json()['address'] == '198.51.100.2'
    with TestClient(wrapped, client=('198.51.100.3', 1234)) as direct:
        assert direct.get('/', headers={'X-Forwarded-For': 'forged'}).json()['address'] == '198.51.100.3'


def test_production_anonymous_needs_no_identity_secrets(config_env, monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('AUTH_MODE', 'anonymous')
    monkeypatch.delenv('CLIENT_API_KEY')
    monkeypatch.delenv('SESSION_ISSUER_SECRET', raising=False)
    settings = Settings.load()
    assert settings.anonymous_jobs_per_hour == 5
    assert settings.anonymous_max_active_jobs == 2
    monkeypatch.setenv('DEV_ALLOW_ANONYMOUS', 'true')
    with pytest.raises(ValueError, match='must not use'):
        Settings.load()


@pytest.mark.parametrize('name', ['ANONYMOUS_JOBS_PER_HOUR', 'ANONYMOUS_MAX_ACTIVE_JOBS'])
def test_anonymous_limits_must_be_positive(config_env, monkeypatch, name):
    monkeypatch.setenv(name, '0')
    with pytest.raises(ValueError, match='positive'):
        Settings.load()
