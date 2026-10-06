"""Stateless coordination API. Never downloads, encodes, or streams media bytes."""

from contextlib import asynccontextmanager
from pathlib import Path
import os
import time
import uuid
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.schemas import ConversionRequest
from app.distributed.config import Settings
from app.distributed.db import Admission, Capacity, Database, Job
from app.distributed.security import (Tokens, anonymous_subject, exchange_identity, rate_limit,
                                      verify_anonymous_session,
                                      revoke_identity, subject, verify_key, verify_session)
from app.distributed.storage import Storage


class LivenessResponse(BaseModel):
    status: Literal['ok'] = 'ok'


class AuthRequest(BaseModel):
    api_key: str = Field(default='', max_length=256)
    assertion: str = Field(default='', max_length=4096)


def create_app(settings=None, database=None, broker=None, storage=None):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.load()
        app.state.config = config
        app.state.db = database or Database(config.database_url)
        app.state.redis = broker or Redis.from_url(config.redis_url, socket_timeout=5, socket_connect_timeout=5)
        app.state.storage = storage or Storage(config)
        app.state.tokens = Tokens(config.signing_secret)
        try:
            yield
        finally:
            if database is None:
                app.state.db.engine.dispose()
            if broker is None:
                app.state.redis.close()
            if storage is None:
                app.state.storage.close()

    app = FastAPI(title='Media coordination API', version='2.0.0', lifespan=lifespan)
    origins = [value.strip() for value in os.getenv('CORS_ORIGINS', '').split(',') if value.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=['GET', 'HEAD', 'POST', 'DELETE'],
                           allow_headers=['Authorization', 'Content-Type'])

    @app.middleware('http')
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.exception_handler(RedisError)
    async def unavailable(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'Queue service unavailable. Please retry shortly.'}, status_code=503)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': 'Database unavailable. Please retry shortly.'}, status_code=503)

    def client_address(request: Request) -> str:
        # Uvicorn applies forwarded headers only for its explicitly trusted peers.
        # Never read client-supplied X-Forwarded-For or CF-Connecting-IP here.
        return request.client.host if request.client else ''

    def session_claims(request, authorization):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Authenticate first.')
        claims = request.app.state.tokens.read('session', authorization[7:], 900)
        verify_session(request.app.state, claims)
        verify_anonymous_session(request.app.state, claims, client_address(request))
        return claims

    def lookup_session(session, claims, *, lock=False):
        job = session.get(Job, claims.get('job'), with_for_update=lock)
        if not job or job.owner != claims.get('owner'):
            raise HTTPException(404, 'Job not found.')
        if job.expires is not None and job.expires <= time.time():
            raise HTTPException(410, 'Job expired.')
        return job

    def lookup(db, claims):
        with db.sessions() as session:
            return lookup_session(session, claims)

    def job_claims(request, sig):
        # The authoritative row expiry bounds capabilities, including recovery outages.
        return request.app.state.tokens.read('job', sig, None)

    def urls(request, job, token=None):
        token = token or request.app.state.tokens.issue('job', {'job': job.id, 'owner': job.owner})
        base = request.app.state.config.conversion_base
        return {
            'progressURL': f'{base}/api/v1/progress?sig={token}',
            'downloadURL': f'{base}/api/v1/download?sig={token}&download=true',
            'previewURL': f'{base}/api/v1/download?sig={token}',
            'cancelURL': f'{base}/api/v1/cancel?sig={token}',
            'deleteURL': f'{base}/api/v1/job?sig={token}',
            'jobToken': token,
        }

    @app.get('/', include_in_schema=False)
    def ui():
        return FileResponse(Path(__file__).resolve().parents[1] / 'index.html', media_type='text/html')

    @app.get('/api/v1/config')
    def config(request: Request):
        return {'architecture': 'distributed', 'requires_api_key': (request.app.state.config.auth_mode == 'internal'
                                     and not request.app.state.config.allow_anonymous),
                'auth_mode': request.app.state.config.auth_mode,
                'conversion_base': request.app.state.config.conversion_base,
                'max_duration_seconds': request.app.state.config.max_duration}

    @app.post('/api/v1/auth')
    def auth(payload: AuthRequest, request: Request):
        state = request.app.state
        address = client_address(request)
        owner = (anonymous_subject(address, state.config.signing_secret)
                 if state.config.auth_mode == 'anonymous' else subject(address))
        rate_limit(state.redis, 'media:auth:' + owner, 30)
        if state.config.auth_mode == 'anonymous':
            if payload.api_key or payload.assertion:
                raise HTTPException(400, 'Send an empty body for anonymous access.')
            claims = {'owner': owner, 'mode': 'anonymous'}
            return {'error': 0, 'key': state.tokens.issue('session', claims), 'expires_in': 900}
        if state.config.auth_mode == 'identity':
            claims = exchange_identity(state, payload.assertion)
            return {'error': 0, 'key': state.tokens.issue('session', claims), 'expires_in': 900}
        if verify_key(payload.api_key, state.config.api_key):
            owner = subject(payload.api_key)
        elif state.config.allow_anonymous and not payload.api_key:
            # Development-only sessions share an IP quota; do not trust forwarded IP headers.
            owner = subject('anonymous:' + address)
        else:
            raise HTTPException(401, 'Invalid API key.')
        return {'error': 0, 'key': state.tokens.issue('session', {'owner': owner}), 'expires_in': 900}

    @app.post('/api/v1/auth/revoke', status_code=204)
    def revoke(payload: AuthRequest, request: Request):
        revoke_identity(request.app.state, payload.assertion)

    @app.post('/api/v1/init')
    def initialize(payload: ConversionRequest, request: Request, authorization: str | None = Header(default=None)):
        claims = session_claims(request, authorization)
        state = request.app.state
        rate_limit(state.redis, 'media:init:' + claims['owner'], state.config.rate_per_minute)
        # Bind authorization to this validated URL and format, not arbitrary later query parameters.
        token = state.tokens.issue('convert', {**claims, 'id': uuid.uuid4().hex, 'payload': payload.model_dump()})
        return {'error': 0, 'convertURL': f'{state.config.conversion_base}/api/v1/convert?sig={token}', 'expires_in': 120}

    @app.post('/api/v1/convert', status_code=202)
    def convert(request: Request, sig: str):
        state = request.app.state
        claims = state.tokens.read('convert', sig, 120)
        verify_session(state, claims)
        verify_anonymous_session(state, claims, client_address(request))
        payload = ConversionRequest(**claims['payload']).model_dump()
        with state.db.sessions.begin() as session:
            # A single small row lock serializes quota admission across API replicas.
            session.execute(select(Capacity).where(Capacity.id == 1).with_for_update()).scalar_one()
            job = session.get(Job, claims['id'])
            if job is None:
                if session.get(Admission, claims['id']) is not None:
                    raise HTTPException(410, 'Conversion expired or was deleted.')
                if state.config.auth_mode == 'anonymous':
                    active = session.scalar(select(func.count()).select_from(Job).where(
                        Job.owner == claims['owner'], Job.status.in_(['queued', 'running']),
                    ))
                    if active >= state.config.anonymous_max_active_jobs:
                        raise HTTPException(429, 'Too many active jobs on this network. Wait for a job to finish.',
                                            headers={'Retry-After': '30'})
                rate_limit(state.redis, 'media:submit:' + claims['owner'], state.config.rate_per_minute)
                count = session.scalar(select(func.count()).select_from(Job))
                if count >= state.config.max_jobs:
                    raise HTTPException(429, 'Job capacity reached. Retry after files expire.', headers={'Retry-After': '30'})
                if state.config.auth_mode == 'anonymous':
                    rate_limit(state.redis, 'media:anonymous-hour:' + claims['owner'],
                               state.config.anonymous_jobs_per_hour, seconds=3600)
                job = Job(id=claims['id'], owner=claims['owner'], payload=payload)
                session.add(job)
                session.add(Admission(id=job.id, owner=job.owner, expires=time.time() + 121))
                session.flush()
            elif job.expires is not None and job.expires <= time.time():
                raise HTTPException(410, 'Conversion expired or was deleted.')
            elif job.owner != claims['owner'] or job.payload != payload:
                raise HTTPException(409, 'Conversion authorization mismatch.')
        # The queued DB record is the transactional outbox. The dispatcher publishes it to Redis.
        return {'error': 0, 'id': job.id, 'status': job.status, **urls(request, job)}

    @app.get('/api/v1/progress')
    def progress(request: Request, sig: str):
        job = lookup(request.app.state.db, job_claims(request, sig))
        return {
            'id': job.id, 'status': job.status, 'stage': job.stage, 'format': job.payload['format'],
            # No invented percentage during encoding. 100 means output upload and DB commit completed.
            'progress': 100 if job.status == 'completed' else None,
            'error': job.error, 'error_code': job.error_code, 'stats': job.stats,
            'size_bytes': job.size, 'created_at': job.created, 'expires_at': job.expires,
            'preview_url': urls(request, job, sig)['previewURL'] if job.status == 'completed' else None,
            'download_url': urls(request, job, sig)['downloadURL'] if job.status == 'completed' else None,
        }

    @app.api_route('/api/v1/download', methods=['GET', 'HEAD'])
    def download(request: Request, sig: str, download: bool = False):
        job = lookup(request.app.state.db, job_claims(request, sig))
        if job.status != 'completed' or not job.object_key:
            raise HTTPException(409, 'Media is not ready.')
        ttl = max(1, min(300, int(job.expires - time.time())))
        url = request.app.state.storage.url(job.object_key, job.filename, download, ttl, method=request.method)
        return RedirectResponse(url, status_code=307)

    @app.post('/api/v1/cancel')
    def cancel(request: Request, sig: str):
        claims = job_claims(request, sig)
        with request.app.state.db.sessions.begin() as session:
            job = lookup_session(session, claims, lock=True)
            if job.status in {'queued', 'running'}:
                job.cancel_requested = True
                if job.status == 'queued':
                    job.status, job.stage = 'cancelled', 'cancelled'
                    job.expires = time.time() + request.app.state.config.retention
        return {'status': 'cancel_requested'}

    @app.delete('/api/v1/job', status_code=204)
    def delete(request: Request, sig: str):
        claims = job_claims(request, sig)
        with request.app.state.db.sessions.begin() as session:
            job = lookup_session(session, claims, lock=True)
            if job.status in {'queued', 'running'}:
                raise HTTPException(409, 'Cancel the job before deleting it.')
            job.expires = time.time()  # Dispatcher retries storage deletion until it succeeds.

    @app.get('/live', response_model=LivenessResponse)
    def live():
        return {'status': 'ok'}

    @app.get('/health')
    def health(request: Request):
        with request.app.state.db.sessions() as session:
            from app.distributed.migrate import LATEST_REVISION
            revision = session.scalar(text('SELECT revision FROM media_schema_version WHERE id = 1'))
            if revision != LATEST_REVISION:
                raise HTTPException(503, 'Database schema upgrade required.')
        request.app.state.redis.ping()
        return {'status': 'ok', 'architecture': 'distributed'}

    return app


app = create_app()
