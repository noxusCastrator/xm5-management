"""Transport guards use synthetic ASGI messages, with no socket or device."""
import asyncio
import unittest

from xm5lib.automation.http_guard import LocalGuard
from xm5lib.automation.server import failure


class HttpGuardTests(unittest.TestCase):
    def request(self, chunks=(), *, headers=None, method='POST'):
        messages = [{'type': 'http.request', 'body': chunk, 'more_body': i + 1 < len(chunks)}
                    for i, chunk in enumerate(chunks)] or [{'type': 'http.request', 'body': b''}]
        output, received = [], []
        async def receive():
            return messages.pop(0) if messages else {'type': 'http.disconnect'}
        async def send(message):
            output.append(message)
        async def downstream(scope, receive, send):
            from starlette.requests import Request
            request = Request(scope, receive)
            received.append(await request.body())
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        values = {'host': '127.0.0.1:1234', 'authorization': 'Bearer fixture', 'content-type': 'application/json'}
        values.update(headers or {})
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
                 'path': '/', 'query_string': b'', 'headers': [(k.encode(), v.encode()) for k, v in values.items()]}
        asyncio.run(LocalGuard(downstream, token='fixture', authority='127.0.0.1:1234', failure=failure)(scope, receive, send))
        return output[0]['status'], received, messages

    def test_chunked_json_replayed_through_public_receive(self):
        status, bodies, _ = self.request([b'{"a":', b'1}'])
        self.assertEqual((status, bodies), (200, [b'{"a":1}']))

    def test_stream_limit_without_content_length(self):
        self.assertEqual(self.request([b'a' * 1_048_576])[0], 200)
        status, bodies, _ = self.request([b'a' * 1_048_576, b'b'])
        self.assertEqual((status, bodies), (413, []))

    def test_session_guards_reject_before_reading_body(self):
        for headers, expected in (({'host': 'evil.test'}, 403), ({'origin': 'null'}, 403),
                                  ({'authorization': 'wrong'}, 401), ({'content-type': 'text/plain'}, 415)):
            status, bodies, unread = self.request([b'large'], headers=headers)
            self.assertEqual(status, expected)
            self.assertEqual(bodies, [])
            self.assertEqual(len(unread), 1)
