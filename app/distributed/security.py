import hashlib
import hmac

from fastapi import HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer


class Tokens:
    def __init__(self, secret):
        self.serializer = URLSafeTimedSerializer(secret, signer_kwargs={'digest_method': hashlib.sha256})

    def issue(self, purpose, claims):
        return self.serializer.dumps(claims, salt=purpose)

    def read(self, purpose, token, lifetime):
        try:
            claims = self.serializer.loads(token, salt=purpose, max_age=lifetime)
            if not isinstance(claims, dict):
                raise BadSignature('Invalid claims')
            return claims
        except BadSignature:
            raise HTTPException(401, 'Authorization is invalid or expired.') from None


def subject(api_key):
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_key(supplied, configured):
    return bool(supplied and configured) and hmac.compare_digest(supplied.encode(), configured.encode())


def rate_limit(redis, key, limit, seconds=60):
    # Increment and expiry must be atomic so a crashed request cannot leave a permanent counter.
    count = redis.eval('''local n=redis.call('INCR',KEYS[1]);
        if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n''', 1, key, seconds)
    if count > limit:
        raise HTTPException(429, 'Rate limit reached. Try again shortly.', headers={'Retry-After': str(seconds)})


def identity_claims(state, assertion: str, purpose: str) -> dict[str, str]:
    """Accept one short-lived assertion from the trusted application backend."""
    import uuid

    if state.config.auth_mode != 'identity':
        raise HTTPException(404, 'Identity exchange is not enabled.')
    claims = Tokens(state.config.issuer_secret).read(purpose, assertion, 60)
    user_id, nonce = claims.get('sub'), claims.get('nonce')
    if not isinstance(user_id, str) or not 1 <= len(user_id) <= 128:
        raise HTTPException(401, 'Invalid identity assertion.')
    try:
        uuid.UUID(nonce)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, 'Invalid identity assertion.') from None
    if not state.redis.set('media:assertion:' + subject(assertion), 'used', ex=61, nx=True):
        raise HTTPException(401, 'Identity assertion was already exchanged.')
    return {'owner': subject('user:' + user_id)}


def exchange_identity(state, assertion: str) -> dict[str, str]:
    import uuid

    claims = identity_claims(state, assertion, 'identity-session')
    key = 'media:session-generation:' + claims['owner']
    state.redis.set(key, uuid.uuid4().hex, nx=True, ex=86400)
    state.redis.expire(key, 86400)
    generation = state.redis.get(key)
    if generation is None:
        raise HTTPException(503, 'Session service unavailable.')
    claims['generation'] = generation.decode() if isinstance(generation, bytes) else generation
    return claims


def verify_session(state, claims: dict) -> None:
    if state.config.auth_mode != 'identity':
        return
    expected = state.redis.get('media:session-generation:' + claims.get('owner', ''))
    if isinstance(expected, bytes):
        expected = expected.decode()
    if not expected or not verify_key(claims.get('generation', ''), expected):
        raise HTTPException(401, 'Session revoked or expired. Authenticate again.')


def revoke_identity(state, assertion: str) -> None:
    import uuid

    claims = identity_claims(state, assertion, 'identity-revoke')
    # Rotating the generation invalidates existing sessions and conversion grants.
    state.redis.set('media:session-generation:' + claims['owner'], uuid.uuid4().hex, ex=86400)


def anonymous_subject(address: str, secret: str) -> str:
    """Group IPv6 privacy addresses by /64; never put raw client IPs in tokens."""
    from ipaddress import ip_address, ip_network

    try:
        ip = ip_address(address)
    except ValueError:
        raise HTTPException(503, 'Client address unavailable.') from None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    network = str(ip_network(f'{ip}/64', strict=False)) if ip.version == 6 else str(ip)
    return hmac.new(secret.encode(), ('anonymous:' + network).encode(), hashlib.sha256).hexdigest()


def verify_anonymous_session(state, claims: dict, address: str) -> None:
    if state.config.auth_mode != 'anonymous':
        if claims.get('mode') == 'anonymous':
            raise HTTPException(401, 'Access mode changed. Start a new session.')
        return
    expected = anonymous_subject(address, state.config.signing_secret)
    if claims.get('mode') != 'anonymous' or not verify_key(claims.get('owner', ''), expected):
        raise HTTPException(401, 'Network changed or session invalid. Start a new session.')
