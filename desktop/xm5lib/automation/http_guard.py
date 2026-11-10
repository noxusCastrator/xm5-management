"""ASGI session guard and bounded JSON buffering using the public protocol."""
import secrets

from starlette.requests import Request


class LocalGuard:
    def __init__(self, app, *, token, authority, failure):
        self.app, self.token, self.authority, self.failure = app, token, authority, failure

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        error = None
        if request.headers.get('host') != self.authority:
            error = ('HOST_REJECTED', 'Unexpected local authority', 403)
        elif 'origin' in request.headers:
            error = ('ORIGIN_REJECTED', 'Browser clients are not enabled', 403)
        elif not secrets.compare_digest(request.headers.get('authorization', '').encode(),
                                        ('Bearer ' + self.token).encode()):
            error = ('UNAUTHORIZED', 'A valid session token is required', 401)
        elif request.method in ('POST', 'PUT', 'PATCH'):
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                error = ('INVALID_ARGUMENT', 'JSON body required', 415)
            else:
                chunks, count = [], 0
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    count += len(chunk)
                    if count > 1_048_576:
                        error = ('BODY_TOO_LARGE', 'Request exceeds 1 MiB', 413)
                        break
                    chunks.append(chunk)
                    if not message.get('more_body', False):
                        break
                if error is None:
                    body = b''.join(chunks)
                    sent = False
                    original_receive = receive
                    async def replay():
                        nonlocal sent
                        if not sent:
                            sent = True
                            return {'type': 'http.request', 'body': body, 'more_body': False}
                        return await original_receive()
                    receive = replay
        if error:
            return await self.failure(*error)(scope, receive, send)
        return await self.app(scope, receive, send)
