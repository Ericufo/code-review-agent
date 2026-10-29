"""ASGI request limits/auth and safe errors, before JSON parsing or route work."""
import hmac
from uuid import uuid4

from starlette.responses import JSONResponse

MAX_BODY_BYTES = 16_384


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def error_response(scope, status, code, message):
    request_id = scope.get('state', {}).get('request_id', str(uuid4()))
    headers = {'X-Request-ID': request_id}
    if status == 401:
        headers['WWW-Authenticate'] = 'Bearer'
    if status == 429:
        headers['Retry-After'] = '1'
    return JSONResponse({'code': code, 'message': message, 'request_id': request_id}, status, headers)


class HTTPBoundary:
    def __init__(self, app, token):
        self.app, self.token = app, token.encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        scope.setdefault('state', {})['request_id'] = str(uuid4())
        started = False
        async def tracked_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                message['headers'] = [*(item for item in message.get('headers', []) if item[0].lower() != b'x-request-id'),
                    (b'x-request-id', scope['state']['request_id'].encode())]
            await send(message)
        try:
            if scope['path'].startswith('/api/'):
                headers = scope.get('headers', [])
                auth = [value for name, value in headers if name.lower() == b'authorization']
                parts = auth[0].split(b' ') if len(auth) == 1 else []
                if (len(parts) != 2 or parts[0].lower() != b'bearer'
                        or not hmac.compare_digest(parts[1], self.token)):
                    raise APIError(401, 'unauthorized', 'Valid bearer credentials are required')
                if scope['method'] == 'POST':
                    lengths = [value for name, value in headers if name.lower() == b'content-length']
                    if lengths:
                        if len(lengths) != 1 or not lengths[0].isdigit():
                            raise APIError(422, 'invalid_input', 'Invalid content length')
                        if int(lengths[0]) > MAX_BODY_BYTES:
                            raise APIError(413, 'body_too_large', 'Request body exceeds 16384 bytes')
                    body = bytearray()
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            return
                        body.extend(message.get('body', b''))
                        if len(body) > MAX_BODY_BYTES:
                            raise APIError(413, 'body_too_large', 'Request body exceeds 16384 bytes')
                        if not message.get('more_body', False):
                            break
                    delivered = False
                    original_receive = receive
                    async def buffered_receive():
                        nonlocal delivered
                        if delivered:
                            return await original_receive()
                        delivered = True
                        return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                    receive = buffered_receive
            await self.app(scope, receive, tracked_send)
        except APIError as exc:
            if not started:
                await error_response(scope, exc.status, exc.code, exc.message)(scope, receive, send)
        except Exception:
            # Never send/log exception bodies containing credentials or source.
            if not started:
                await error_response(scope, 500, 'internal_error', 'Request could not be completed')(scope, receive, send)
