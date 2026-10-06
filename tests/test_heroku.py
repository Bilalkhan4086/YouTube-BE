import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.distributed.broker import create_broker
from app.distributed.db import Database
from app.distributed.heroku import HerokuRouter, create_app


def test_router_uses_observed_peer_not_spoofed_prefix():
    async def address(request: Request):
        return JSONResponse({'client': request.client.host})

    with TestClient(HerokuRouter(Starlette(routes=[Route('/', address)]))) as client:
        response = client.get('/', headers={'X-Forwarded-For': '192.0.2.1, 198.51.100.7'})
        assert response.json() == {'client': '198.51.100.7'}
        assert client.get('/').status_code == 400
        assert client.get('/', headers={'X-Forwarded-For': '192.0.2.1, invalid'}).status_code == 400
        assert client.get('/', headers=[('X-Forwarded-For', '192.0.2.1'),
                                        ('X-Forwarded-For', '198.51.100.7')]).status_code == 400


def test_heroku_entrypoint_requires_explicit_boundary(monkeypatch):
    monkeypatch.delenv('DYNO', raising=False)
    monkeypatch.setenv('HEROKU_PROXY_MODE', 'direct')
    with pytest.raises(RuntimeError):
        create_app()


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql', 'postgresql+psycopg'])
def test_heroku_database_urls_use_installed_driver_and_tls(monkeypatch, scheme):
    monkeypatch.setenv('DYNO', 'web.1')
    database = Database(f'{scheme}://test:test@localhost/test')
    try:
        assert database.engine.dialect.driver == 'psycopg'
        assert database.engine.url.query['sslmode'] == 'require'
    finally:
        database.engine.dispose()
    database = Database(f'{scheme}://test:test@localhost/test?sslmode=verify-full')
    try:
        assert database.engine.url.query['sslmode'] == 'verify-full'
    finally:
        database.engine.dispose()


def test_redis_tls_override_is_explicit(monkeypatch):
    monkeypatch.delenv('REDIS_SSL_CERT_REQS', raising=False)
    broker = create_broker('rediss://localhost:6379')
    assert broker.connection_pool.connection_kwargs['ssl_cert_reqs'] == 'required'
    broker.close()
    monkeypatch.setenv('REDIS_SSL_CERT_REQS', 'none')
    broker = create_broker('rediss://localhost:6379')
    assert broker.connection_pool.connection_kwargs['ssl_cert_reqs'] == 'none'
    broker.close()
    with pytest.raises(ValueError):
        create_broker('redis://localhost:6379')
