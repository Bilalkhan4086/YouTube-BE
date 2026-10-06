from dataclasses import dataclass
import os


def positive(name, default):
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


@dataclass
class Settings:
    database_url: str
    redis_url: str
    signing_secret: str
    api_key: str
    allow_anonymous: bool
    s3_endpoint: str
    s3_public_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str
    conversion_base: str
    retention: int
    timeout: int
    max_duration: int
    max_queue_wait: int
    max_jobs: int
    max_attempts: int
    lease_seconds: int
    rate_per_minute: int
    auth_mode: str = "internal"
    issuer_secret: str = ""
    anonymous_jobs_per_hour: int = 5
    anonymous_max_active_jobs: int = 2

    @classmethod
    def load(cls, role: str = 'api'):
        secret = os.getenv('SIGNING_SECRET', '')
        anonymous = os.getenv('DEV_ALLOW_ANONYMOUS', 'false').lower() == 'true'
        api_key = os.getenv('CLIENT_API_KEY', '')
        auth_mode = os.getenv('AUTH_MODE', 'internal')
        issuer_secret = os.getenv('SESSION_ISSUER_SECRET', '')
        if auth_mode not in {'internal', 'identity', 'anonymous'}:
            raise ValueError('AUTH_MODE must be internal, identity or anonymous')
        if role == 'api':
            if len(secret) < 32:
                raise ValueError('SIGNING_SECRET must contain at least 32 characters')
            if auth_mode == 'identity':
                if anonymous or len(issuer_secret) < 32 or issuer_secret == secret:
                    raise ValueError('Identity mode needs a distinct issuer secret and no anonymous access')
            elif auth_mode == 'anonymous':
                if anonymous:
                    raise ValueError('Production anonymous mode must not use DEV_ALLOW_ANONYMOUS')
            elif not anonymous and len(api_key) < 32:
                raise ValueError('Internal CLIENT_API_KEY must contain at least 32 characters')
            if os.getenv('APP_ENV') == 'production' and auth_mode not in {'identity', 'anonymous'}:
                raise ValueError('Production requires AUTH_MODE=anonymous or identity')
        return cls(
            database_url=os.environ['DATABASE_URL'], redis_url=os.environ['REDIS_URL'],
            signing_secret=secret, api_key=api_key, allow_anonymous=anonymous,
            auth_mode=auth_mode, issuer_secret=issuer_secret,
            anonymous_jobs_per_hour=positive("ANONYMOUS_JOBS_PER_HOUR", 5),
            anonymous_max_active_jobs=positive("ANONYMOUS_MAX_ACTIVE_JOBS", 2),
            s3_endpoint=os.getenv('S3_ENDPOINT', ''), s3_public_endpoint=os.getenv('S3_PUBLIC_ENDPOINT', ''),
            s3_bucket=os.environ['S3_BUCKET'], s3_access_key=os.environ['S3_ACCESS_KEY'],
            s3_secret_key=os.environ['S3_SECRET_KEY'], s3_region=os.getenv('S3_REGION', 'us-east-1'),
            conversion_base=os.getenv('CONVERSION_API_BASE', '').rstrip('/'),
            retention=positive('JOB_RETENTION_SECONDS', 3600), timeout=positive('CONVERSION_TIMEOUT_SECONDS', 600),
            max_duration=positive('MAX_DURATION_SECONDS', 1800), max_queue_wait=positive('MAX_QUEUE_WAIT_SECONDS', 900),
            max_jobs=positive('MAX_STORED_JOBS', 100), max_attempts=positive('MAX_JOB_ATTEMPTS', 3),
            lease_seconds=positive('WORKER_LEASE_SECONDS', 60), rate_per_minute=positive('JOBS_PER_MINUTE', 10),
        )

    @property
    def lifecycle_days(self) -> int:
        # One extra day protects the longest upload/commit window; exact expiry is
        # still enforced by the application. Lifecycle is only an orphan backstop.
        import math
        return math.ceil((self.retention + self.timeout + 120) / 86400) + 1
