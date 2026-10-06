"""Explicit deployment initialization. Does not run inside API replica startup."""
from botocore.exceptions import ClientError

from app.distributed.config import Settings
from app.distributed.db import Database
from app.distributed.storage import Storage


def main():
    settings = Settings.load('setup')
    database = Database(settings.database_url)
    try:
        database.initialize()
    finally:
        database.engine.dispose()
    storage = Storage(settings)
    try:
        configure_bucket(storage, settings)
    finally:
        storage.close()
    print('Database and private temporary-media bucket initialized.')


def configure_bucket(storage: Storage, settings: Settings) -> None:
    try:
        storage.client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] != 404:
            raise
        args = {'Bucket': settings.s3_bucket}
        if settings.s3_region != 'us-east-1':
            args['CreateBucketConfiguration'] = {'LocationConstraint': settings.s3_region}
        storage.client.create_bucket(**args)
    # Keep objects private. Application cleanup uses exact job TTL; lifecycle is a
    # backstop for crash-orphaned objects and unfinished multipart uploads.
    storage.client.put_bucket_lifecycle_configuration(Bucket=settings.s3_bucket, LifecycleConfiguration={
        'Rules': [{'ID': 'temporary-media', 'Status': 'Enabled', 'Filter': {'Prefix': 'media/'},
                   'Expiration': {'Days': settings.lifecycle_days}, 'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 1}}]})


if __name__ == '__main__':
    main()
