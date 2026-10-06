"""Transactional-outbox relay, lease recovery, and TTL cleanup. Safe to replicate."""
import logging
import time

from redis import Redis
from sqlalchemy import delete, select

from app.distributed.config import Settings
from app.distributed.db import Admission, Database, Job
from app.distributed.storage import Storage

QUEUE = 'media:conversions'
logger = logging.getLogger(__name__)


def recover_leases(db: Database, settings: Settings) -> None:
    now = time.time()
    with db.sessions.begin() as session:
        jobs = session.scalars(select(Job).where(Job.status == 'running', Job.lease_until < now)
                               .with_for_update(skip_locked=True).limit(100)).all()
        for job in jobs:
            if job.cancel_requested:
                job.status = job.stage = 'cancelled'
                job.expires = now + settings.retention
            elif job.attempts >= settings.max_attempts:
                job.status = job.stage = 'failed'
                job.error_code, job.error = 'worker_lost', 'Worker repeatedly stopped. Submit the video again.'
                job.expires = now + settings.retention
            else:
                job.status = job.stage = 'queued'
                job.publish_after = 0
            job.run_token = None  # Fence off the old worker before another one can claim it.


def publish_jobs(db: Database, broker: Redis, settings: Settings) -> None:
    now = time.time()
    with db.sessions.begin() as session:
        jobs = session.scalars(select(Job).where(Job.status == 'queued', Job.publish_after <= now)
                               .order_by(Job.created).with_for_update(skip_locked=True).limit(100)).all()
        for job in jobs:
            if now - job.created > settings.max_queue_wait:
                job.status = job.stage = 'failed'
                job.error_code, job.error = 'queue_timeout', 'Queue wait exceeded the limit. Please retry later.'
                job.expires = now + settings.retention
                continue
            broker.rpush(QUEUE, job.id)
            job.publish_after = now + 30
            # A crash before commit can duplicate a message; worker claims are idempotent.
            # A lost Redis message is re-published while the durable job remains queued.


def cleanup_jobs(db: Database, storage: Storage) -> None:
    now = time.time()
    with db.sessions() as session:
        ids = session.scalars(select(Job.id).where(
            Job.expires <= now, Job.cleanup_after <= now,
            Job.status.in_(['completed', 'failed', 'cancelled']),
        ).order_by(Job.cleanup_after, Job.expires).limit(100)).all()
    for job_id in ids:
        with db.sessions.begin() as session:
            job = session.scalar(select(Job).where(Job.id == job_id)
                                 .with_for_update(skip_locked=True))
            if not job or job.cleanup_after > now or job.expires is None or job.expires > now:
                continue
            # Reserve cleanup across replicas, commit, then make the slow S3 call.
            # A crash leaves the row retryable after this reservation expires.
            reservation = time.time() + 300
            job.cleanup_after = reservation
            job.cleanup_attempts += 1
            object_key = job.object_key
        try:
            storage.delete(object_key)
        except Exception:
            # Do not include provider exception text: it may contain signed URLs.
            logger.warning('cleanup_failed job=%s; will retry', job_id)
            with db.sessions.begin() as session:
                job = session.get(Job, job_id, with_for_update=True)
                if job and job.cleanup_after == reservation:
                    job.cleanup_after = time.time() + min(300, 2 ** min(job.cleanup_attempts, 8))
            continue
        with db.sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job and job.cleanup_after == reservation and job.object_key == object_key:
                session.delete(job)
    with db.sessions.begin() as session:
        session.execute(delete(Admission).where(Admission.expires < now))


def dispatch_once(db: Database, broker: Redis, storage: Storage, settings: Settings) -> None:
    failures = []
    for operation in (
        lambda: recover_leases(db, settings),
        lambda: publish_jobs(db, broker, settings),
        lambda: cleanup_jobs(db, storage),
    ):
        try:
            operation()
        except Exception as error:
            failures.append(error)
    if failures:
        # Cleanup has already had its own chance to run, even when publishing failed.
        raise failures[0]


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load('dispatcher')
    db = Database(settings.database_url)
    broker = Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    storage = Storage(settings)
    while True:
        try:
            dispatch_once(db, broker, storage, settings)
        except Exception:
            logger.exception('Dispatcher iteration failed; will retry')
        time.sleep(2)


if __name__ == '__main__':
    main()
