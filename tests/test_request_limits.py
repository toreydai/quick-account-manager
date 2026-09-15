import httpx
import pytest
from starlette.responses import PlainTextResponse

from app.core.request_limits import RequestBodyLimitMiddleware
from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["&".join(f"f{i}=v" for i in range(1001)), "field=" + "x" * (1024 * 1024 + 1)])
async def test_urlencoded_limits_apply_before_login(payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/users/create", content=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, b"1", b"9"])
async def test_body_limit_rejects_before_downstream(declared_length):
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    messages = iter([
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56789", "more_body": False},
    ])
    async def receive():
        return next(messages)

    sent = []
    async def send(message):
        sent.append(message)

    scope = {"type": "http", "headers": [] if declared_length is None else [(b"content-length", declared_length)]}
    await RequestBodyLimitMiddleware(downstream, max_bytes=8)(scope, receive, send)
    assert sent[0]["status"] == 413
    assert not called


@pytest.mark.asyncio
async def test_valid_body_is_replayed():
    async def downstream(scope, receive, send):
        message = await receive()
        await PlainTextResponse(message["body"])(scope, receive, send)

    limited = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=limited), base_url="http://testserver") as client:
        response = await client.post("/", content=b"12345678")
    assert response.status_code == 200
    assert response.content == b"12345678"
