"""Direct Heroku Common Runtime ingress; run with Uvicorn proxy headers disabled."""

import ipaddress
import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class HerokuRouter:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'http':
            values = [value for name, value in scope['headers'] if name == b'x-forwarded-for']
            try:
                # Heroku appends its observed peer at the right. Earlier entries are
                # client controlled. This boundary is valid only behind Heroku's router.
                if len(values) != 1:
                    raise ValueError('Ambiguous router address')
                address = str(ipaddress.ip_address(values[0].decode('ascii').split(',')[-1].strip()))
            except (ValueError, UnicodeDecodeError):
                response = JSONResponse({'detail': 'Invalid router address'}, status_code=400)
                await response(scope, receive, send)
                return
            scope = dict(scope, client=(address, 0))
        await self.app(scope, receive, send)


def create_app() -> ASGIApp:
    if not os.getenv('DYNO') or os.getenv('HEROKU_PROXY_MODE') != 'direct':
        raise RuntimeError('Heroku entry point requires DYNO and HEROKU_PROXY_MODE=direct')
    from app.distributed.api import create_app as api_app

    return HerokuRouter(api_app())
