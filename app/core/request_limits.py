from starlette.responses import PlainTextResponse


class RequestBodyLimitMiddleware:
    """Bound the body before FastAPI can start parsing form parameters."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                if not value.isdigit():
                    await PlainTextResponse("Invalid Content-Length", status_code=400)(scope, receive, send)
                    return
                if len(value) > 20 or int(value) > self.max_bytes:
                    await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
                    return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
