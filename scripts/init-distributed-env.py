"""Create private local-development configuration without printing secrets."""
from pathlib import Path
import secrets

path = Path('.env.distributed')
if path.exists():
    print('Keeping existing .env.distributed')
else:
    password = secrets.token_urlsafe(32)
    storage_secret = secrets.token_urlsafe(32)
    values = {
        'POSTGRES_PASSWORD': password,
        'DATABASE_URL': f'postgresql+psycopg://media:{password}@postgres:5432/media',
        'REDIS_URL': 'redis://redis:6379/0',
        'SIGNING_SECRET': secrets.token_urlsafe(48),
        'CLIENT_API_KEY': secrets.token_urlsafe(48),
        'DEV_ALLOW_ANONYMOUS': 'true',
        'S3_ENDPOINT': 'http://storage:9000',
        'S3_PUBLIC_ENDPOINT': 'http://localhost:19000',
        'S3_BUCKET': 'media-temporary', 'S3_ACCESS_KEY': 'media-local', 'S3_SECRET_KEY': storage_secret,
        'S3_REGION': 'us-east-1',
        'JOB_RETENTION_SECONDS': '3600', 'MAX_STORED_JOBS': '100',
        'CONVERSION_TIMEOUT_SECONDS': '600', 'MAX_DURATION_SECONDS': '1800',
        'MAX_DOWNLOAD_MB': '512', 'MAX_OUTPUT_MB': '512',
        'MAX_QUEUE_WAIT_SECONDS': '900', 'MAX_JOB_ATTEMPTS': '3', 'WORKER_LEASE_SECONDS': '60',
        'JOBS_PER_MINUTE': '10',
    }
    with path.open('x') as file:
        path.chmod(0o600)
        file.write('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
    print('Created private .env.distributed for local testing; anonymous demo access is enabled.')
