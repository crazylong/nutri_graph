"""请求标识。纯 ASGI，不用 BaseHTTPMiddleware。"""
from __future__ import annotations

import uuid

import config
from llm.usage import reset_rid, set_rid


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not config.RID_ENABLED:
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex[:8]
        token = set_rid(rid)

        async def send_with_rid(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-trace-id", rid.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_rid)
        finally:
            reset_rid(token)
