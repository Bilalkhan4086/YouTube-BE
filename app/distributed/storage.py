import boto3
from botocore.config import Config
from boto3.s3.transfer import TransferConfig


class Storage:
    def __init__(self, settings):
        self.settings = settings
        common = dict(aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key,
                      region_name=settings.s3_region,
                      config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                                    connect_timeout=10, read_timeout=30, retries={'max_attempts': 2}))
        self.client = boto3.client('s3', endpoint_url=settings.s3_endpoint or None, **common)
        self.signer = boto3.client('s3', endpoint_url=settings.s3_public_endpoint or settings.s3_endpoint or None, **common)

    def upload(self, path, key, cancelled=None):
        def check_cancelled(bytes_transferred):
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError('Upload cancelled')

        self.client.upload_file(str(path), self.settings.s3_bucket, key,
                                ExtraArgs={'ContentType': 'video/mp4' if path.suffix == '.mp4' else 'audio/mpeg'},
                                Callback=check_cancelled, Config=TransferConfig(use_threads=False))

    def delete(self, key):
        if key:
            self.client.delete_object(Bucket=self.settings.s3_bucket, Key=key)

    def url(self, key, filename, download, ttl, method="GET"):
        return self.signer.generate_presigned_url('get_object', Params={
            'Bucket': self.settings.s3_bucket, 'Key': key,
            'ResponseContentDisposition': f'{"attachment" if download else "inline"}; filename="{filename}"',
            'ResponseCacheControl': 'private, no-store',
        }, ExpiresIn=ttl, HttpMethod=method)

    def close(self) -> None:
        self.client.close()
        self.signer.close()
