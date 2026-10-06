"""Redis connections with explicit provider-specific TLS configuration."""

import os
from urllib.parse import urlsplit

from redis import Redis


def create_broker(url: str) -> Redis:
    options = {}
    certificate_mode = os.getenv('REDIS_SSL_CERT_REQS', 'required')
    if certificate_mode not in {'required', 'none'}:
        raise ValueError('REDIS_SSL_CERT_REQS must be required or none')
    if urlsplit(url).scheme == 'rediss':
        options['ssl_cert_reqs'] = certificate_mode
    elif certificate_mode != 'required':
        raise ValueError('Redis certificate override requires rediss TLS')
    return Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5, **options)
