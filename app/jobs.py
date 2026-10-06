"""Durable, bounded single-host conversion queue and retained media files."""

import asyncio
from contextlib import contextmanager
import fcntl
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from fastapi import HTTPException

logger = logging.getLogger('uvicorn.error')


class JobStore:
    def __init__(self, root: Path, retention: int, capacity: int):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.retention = retention
        self.capacity = capacity
        self.database = self.root / 'jobs.sqlite3'
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, expires REAL,
                error_code TEXT, error_message TEXT, filename TEXT, size INTEGER
            )''')
            # Additive migration preserves already downloaded files and job records.
            connection.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(jobs)')}
            if 'stats' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN stats TEXT')
            if 'started' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN started REAL')
            connection.execute('CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created)')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, payload, job_id=None):
        now = time.time()
        job_id = job_id or uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if existing:
                if json.loads(existing['payload']) != payload:
                    raise HTTPException(409, 'This idempotency key was already used for another conversion.')
                return dict(existing)
            # Completed results also count: retention must not cause unbounded storage.
            count = connection.execute('SELECT count(*) FROM jobs').fetchone()[0]
            if count >= self.capacity:
                raise HTTPException(429, 'Job storage is full. Delete an old result or try again after results expire.',
                                    headers={'Retry-After': '30'})
            connection.execute('INSERT INTO jobs (id,payload,status,created,updated) VALUES (?,?,?,?,?)',
                               (job_id, json.dumps(payload), 'queued', now, now))
        return self.get(job_id)

    def get(self, job_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or (row['expires'] is not None and row['expires'] <= time.time()):
            raise HTTPException(404, 'Job not found or expired.')
        return dict(row)

    def claim(self):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            connection.execute("UPDATE jobs SET status='running',updated=?,started=? WHERE id=?", (time.time(), time.time(), row['id']))
        return self.get(row['id'])

    def finish(self, job_id, status, *, code=None, message=None, output=None, stats=None):
        now = time.time()
        with self.connect() as connection:
            connection.execute('''UPDATE jobs SET status=?, updated=?, expires=?, error_code=?,
                                  error_message=?, filename=?, size=?, stats=? WHERE id=?''',
                               (status, now, now + self.retention, code, message,
                                output.name if output else None, output.stat().st_size if output else None,
                                json.dumps(stats, allow_nan=False) if stats is not None else None, job_id))

    def recover(self):
        # One service owns this directory. Interrupted work is explicit, never a
        # permanently "running" job. Pending jobs and completed files survive restart.
        with self.connect() as connection:
            rows = connection.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
        for row in rows:
            self.finish(row['id'], 'failed', code='server_restarted',
                        message='The server restarted during conversion. Please submit the video again.')
            shutil.rmtree(self.root / row['id'], ignore_errors=True)

    def clean_orphans(self):
        with self.connect() as connection:
            known = {row['id'] for row in connection.execute('SELECT id FROM jobs')}
        for path in self.root.iterdir():
            # Restrict deletion to directories owned by this job service.
            if path.is_dir() and len(path.name) == 32 and all(c in '0123456789abcdef' for c in path.name) and path.name not in known:
                shutil.rmtree(path, ignore_errors=True)

    def delete(self, job_id):
        with self.connect() as connection:
            connection.execute('DELETE FROM jobs WHERE id=?', (job_id,))
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    def expire(self, pinned):
        with self.connect() as connection:
            rows = connection.execute('SELECT id FROM jobs WHERE expires<=?', (time.time(),)).fetchall()
        for row in rows:
            if not pinned.get(row['id']):
                self.delete(row['id'])

    def public(self, job):
        job_id = job['id']
        stage = job['status']
        if stage == 'running':
            try:
                stage = json.loads((self.root / job_id / 'progress.json').read_text())['stage']
            except (OSError, ValueError, KeyError, TypeError):
                stage = 'starting'
        return {
            'id': job_id, 'status': job['status'], 'stage': stage,
            'format': json.loads(job['payload'])['format'],
            'created_at': job['created'], 'expires_at': job['expires'],
            'stats': json.loads(job['stats']) if job.get('stats') else None,
            'size_bytes': job['size'], 'error_code': job['error_code'], 'error': job['error_message'],
            'status_url': f'/jobs/{job_id}',
            'preview_url': f'/jobs/{job_id}/file' if job['status'] == 'completed' else None,
            'download_url': f'/jobs/{job_id}/file?download=true' if job['status'] == 'completed' else None,
        }


class JobService:
    def __init__(self, store, state, converter):
        self.store = store
        self.state = state
        self.converter = converter
        self.active = {}
        self.pinned = {}
        self.stopping = False
        self.supervisor = None
        self.lock = None

    async def start(self):
        self.lock = (self.store.root / 'service.lock').open('a')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('JOB_DATA_DIR already has a worker. Use one Uvicorn worker for this single-host queue.') from None
        try:
            self.store.recover()
            self.store.clean_orphans()
            self.store.expire(self.pinned)
            self.supervisor = asyncio.create_task(self.run())
        except BaseException:
            self.lock.close()
            raise

    async def close(self):
        self.stopping = True
        if self.supervisor:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*list(self.active.values()), return_exceptions=True)
        if self.lock:
            self.lock.close()

    async def run(self):
        while True:
            try:
                self.store.expire(self.pinned)
                while self.state.active < self.state.limit:
                    job = self.store.claim()
                    if not job:
                        break
                    self.state.active += 1
                    task = asyncio.create_task(self.execute(job))
                    self.active[job['id']] = task
                    task.add_done_callback(lambda task, job_id=job['id']: self.task_done(job_id, task))
            except Exception:
                logger.exception('job_scheduler_failed')
            await asyncio.sleep(0.5)

    async def execute(self, job):
        job_id = job['id']
        directory = self.store.root / job_id
        started = time.perf_counter()
        try:
            directory.mkdir(mode=0o700)
            output = await self.converter(json.loads(job['payload']), directory)
            stats = {}
            try:
                stats = json.loads((directory / 'stats.json').read_text())
            except (OSError, ValueError):
                pass  # Timing and final size remain available if worker stats are absent.
            stats.update({
                'queue_seconds': round(max(0, (job['started'] or job['updated']) - job['created']), 4),
                'processing_wall_seconds': round(time.perf_counter() - started, 4),
                'total_seconds': round(max(0, time.time() - job['created']), 4),
                'output_bytes': output.stat().st_size,
            })
            # Remove source/intermediate files; retain only the validated result.
            for path in directory.iterdir():
                if path != output:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            self.store.finish(job_id, 'completed', output=output, stats=stats)
        except asyncio.CancelledError:
            self.store.finish(job_id, 'failed' if self.stopping else 'cancelled',
                              code='server_stopped' if self.stopping else 'cancelled',
                              message='Server stopped during conversion. Please retry.' if self.stopping else 'Conversion cancelled.')
            shutil.rmtree(directory, ignore_errors=True)
        except Exception as error:
            code, message = 'internal_error', 'Conversion failed. See server logs using the job ID.'
            if isinstance(error, HTTPException):
                code = (error.headers or {}).get('X-Error-Code', 'conversion_failed')
                message = str(error.detail)
            elif isinstance(error, TimeoutError):
                code, message = 'timeout', 'Conversion timed out. Try a shorter video.'
            logger.error('job_failed job_id=%s code=%s', job_id, code, exc_info=not isinstance(error, (HTTPException, TimeoutError)))
            self.store.finish(job_id, 'failed', code=code, message=message)
            shutil.rmtree(directory, ignore_errors=True)

    def task_done(self, job_id, task):
        self.state.active -= 1
        self.active.pop(job_id, None)
        if task.cancelled():  # Also handles cancellation before execute() first runs.
            self.store.finish(job_id, 'failed' if self.stopping else 'cancelled',
                              code='server_stopped' if self.stopping else 'cancelled',
                              message='Server stopped. Please retry.' if self.stopping else 'Conversion cancelled.')
            shutil.rmtree(self.store.root / job_id, ignore_errors=True)
        elif task.exception():
            logger.error('job_task_failed job_id=%s', job_id, exc_info=task.exception())

    async def cancel(self, job_id):
        job = self.store.get(job_id)
        if job['status'] == 'queued':
            self.store.finish(job_id, 'cancelled', code='cancelled', message='Conversion cancelled.')
        elif job['status'] == 'running':
            task = self.active.get(job_id)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return self.store.public(self.store.get(job_id))
