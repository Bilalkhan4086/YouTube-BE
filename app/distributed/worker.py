"""Independent conversion worker. Run replicas to increase conversion capacity."""
import asyncio
import json
import logging
import signal
from pathlib import Path
import tempfile
import threading
import time
import uuid

from fastapi import HTTPException
from redis import Redis

from app.processor import run_conversion
from app.schemas import ConversionRequest
from app.distributed.config import Settings
from app.distributed.db import Database, Job
from app.distributed.dispatcher import QUEUE
from app.distributed.storage import Storage

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    pass


class LeaseLost(Exception):
    pass


def claim(db, settings, job_id):
    with db.sessions.begin() as session:
        job = session.get(Job, job_id, with_for_update=True)
        if not job or job.status != 'queued' or job.cancel_requested:
            return None
        now = time.time()
        if now - job.created > settings.max_queue_wait:
            job.status = job.stage = 'failed'
            job.error_code, job.error = 'queue_timeout', 'Queue wait exceeded the limit. Please retry later.'
            job.expires = now + settings.retention
            return None
        job.status, job.stage = 'running', 'starting'
        job.run_token = uuid.uuid4().hex
        job.started = now
        job.lease_until = now + settings.lease_seconds
        job.attempts += 1
        return job


def heartbeat(db, settings, job, directory):
    with db.sessions.begin() as session:
        current = session.get(Job, job.id, with_for_update=True)
        if current and current.status == 'completed' and current.run_token == job.run_token:
            return  # Completion committed while the processing task is resuming.
        if not current or current.status != 'running' or current.run_token != job.run_token:
            raise LeaseLost()
        if current.cancel_requested:
            raise JobCancelled()
        current.lease_until = time.time() + settings.lease_seconds
        try:
            current.stage = json.loads((directory / 'progress.json').read_text())['stage']
        except (OSError, ValueError, KeyError):
            pass


def finish(db, settings, job, status, **fields):
    with db.sessions.begin() as session:
        current = session.get(Job, job.id, with_for_update=True)
        if not current or current.status != 'running' or current.run_token != job.run_token:
            return False
        if status == 'completed' and current.cancel_requested:
            raise JobCancelled()
        current.status = current.stage = status
        current.expires = time.time() + settings.retention
        current.lease_until = None
        for name, value in fields.items():
            setattr(current, name, value)
        return True


async def execute(db, storage, settings, job):
    started = time.perf_counter()
    object_key = None
    published = False
    with tempfile.TemporaryDirectory(prefix=f'media-{job.id}-') as temporary:
        directory = Path(temporary)

        async def monitor():
            while True:
                await asyncio.to_thread(heartbeat, db, settings, job, directory)
                await asyncio.sleep(min(2, settings.lease_seconds / 3))

        async def process():
            nonlocal object_key, published
            output = await run_conversion(ConversionRequest(**job.payload), directory, settings.timeout, settings.max_duration)
            stats = json.loads((directory / 'stats.json').read_text())
            (directory / 'progress.json').write_text(json.dumps({'stage': 'uploading'}))
            object_key = f'media/{job.id}/{job.run_token}/{output.name}'
            upload_started = time.perf_counter()
            stop_upload = threading.Event()
            upload = asyncio.create_task(asyncio.to_thread(storage.upload, output, object_key, stop_upload))
            try:
                await asyncio.shield(upload)
            except asyncio.CancelledError:
                stop_upload.set()
                await asyncio.gather(upload, return_exceptions=True)
                raise
            stats.update({
                'queue_seconds': max(0, job.started - job.created),
                'processing_wall_seconds': time.perf_counter() - started,
                'total_seconds': time.time() - job.created,
                'upload_seconds': time.perf_counter() - upload_started,
                'output_bytes': output.stat().st_size,
            })
            commit = asyncio.create_task(asyncio.to_thread(finish, db, settings, job, 'completed',
                object_key=object_key, filename=output.name, size=output.stat().st_size, stats=stats))
            try:
                published = await asyncio.shield(commit)
            except asyncio.CancelledError:
                result = await asyncio.gather(commit, return_exceptions=True)
                published = result[0] is True
                raise
            if not published:
                raise LeaseLost()

        work = asyncio.create_task(asyncio.wait_for(process(), timeout=settings.timeout + 120))
        watcher = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await watcher
        except JobCancelled:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await asyncio.to_thread(finish, db, settings, job, 'cancelled', error='Conversion cancelled.', error_code='cancelled')
        except LeaseLost:
            logger.warning('Lease lost for job %s; discarding this attempt', job.id)
        except HTTPException as error:
            await asyncio.to_thread(finish, db, settings, job, 'failed', error=str(error.detail),
                                    error_code=(error.headers or {}).get('X-Error-Code', 'conversion_failed'))
        except TimeoutError:
            await asyncio.to_thread(finish, db, settings, job, 'failed',
                                    error='Conversion timed out. Try a shorter video.', error_code='timeout')
        except Exception:
            # Infrastructure errors leave the leased job for bounded recovery/retry.
            logger.exception('Worker infrastructure failure job=%s', job.id)
        finally:
            work.cancel()
            watcher.cancel()
            await asyncio.gather(work, watcher, return_exceptions=True)
            if object_key and not published:
                try:
                    await asyncio.to_thread(storage.delete, object_key)
                except Exception:
                    logger.exception('Orphan cleanup deferred to storage lifecycle job=%s', job.id)


async def serve(db: Database, broker: Redis, storage: Storage, settings: Settings,
                stopping: asyncio.Event) -> None:
    """Stop claiming on termination and cancel active work through its cleanup path."""
    while not stopping.is_set():
        try:
            item = await asyncio.to_thread(broker.blpop, QUEUE, timeout=1)
            if stopping.is_set():
                break  # A popped notification is recovered from the durable outbox.
            if not item:
                continue
            job = await asyncio.to_thread(claim, db, settings, item[1].decode())
            if not job:
                continue
            work = asyncio.create_task(execute(db, storage, settings, job))
            shutdown = asyncio.create_task(stopping.wait())
            try:
                done, _ = await asyncio.wait({work, shutdown}, return_when=asyncio.FIRST_COMPLETED)
                if shutdown in done and not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            finally:
                shutdown.cancel()
                await asyncio.gather(shutdown, return_exceptions=True)
        except Exception:
            logger.exception('Worker loop failed; retrying')
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except TimeoutError:
                pass


async def run_worker(db: Database, broker: Redis, storage: Storage, settings: Settings) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    try:
        await serve(db, broker, storage, settings, stopping)
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load('worker')
    db = Database(settings.database_url)
    broker = Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    storage = Storage(settings)
    try:
        asyncio.run(run_worker(db, broker, storage, settings))
    finally:
        broker.close()
        storage.close()
        db.engine.dispose()


if __name__ == '__main__':
    main()
