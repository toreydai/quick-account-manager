"""KeycloakClient 的 token 缓存/刷新逻辑（不连真实 Keycloak，httpx.Client 换成
mock transport）。"""

import httpx

from app.core.config import Settings
from app.services.keycloak_client import KeycloakAPIError, KeycloakAuthError, KeycloakClient


def make_client(handler):
    settings = Settings(
        environment="development",
        keycloak_base_url="https://fake-keycloak.example.com",
        keycloak_realm="quick",
        keycloak_service_client_id="svc",
        keycloak_service_client_secret="secret",
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return KeycloakClient(settings, http_client=http)


def test_fetches_token_and_reuses_it_across_requests():
    token_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            token_calls["count"] += 1
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 300})
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    client.request("GET", "/users")
    client.request("GET", "/users")
    client.request("GET", "/users")

    assert token_calls["count"] == 1  # 三次请求只换了一次 token


def test_auth_failure_raises_keycloak_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_client")

    client = make_client(handler)
    try:
        client.request("GET", "/users")
        assert False, "应该抛出 KeycloakAuthError"
    except KeycloakAuthError:
        pass


def test_api_error_raises_keycloak_api_error_with_status_code():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 300})
        return httpx.Response(404, text="not found")

    client = make_client(handler)
    try:
        client.request("GET", "/users/does-not-exist")
        assert False, "应该抛出 KeycloakAPIError"
    except KeycloakAPIError as exc:
        assert exc.status_code == 404


def test_401_on_api_call_triggers_one_token_refresh_retry():
    calls = {"token": 0, "users": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{calls['token']}", "expires_in": 300})
        calls["users"] += 1
        if calls["users"] == 1:
            return httpx.Response(401, text="token revoked")
        return httpx.Response(200, json=[])

    client = make_client(handler)
    resp = client.request("GET", "/users")
    assert resp.status_code == 200
    assert calls["token"] == 2  # 第一次 token 用过，401 后强制刷新重试了一次


def test_401_retry_preserves_caller_supplied_headers():
    """之前的写法在 401 重试递归调用时会把调用方自己传的 headers 弄丢——
    pop 出来只用在了第一次请求上，**kwargs 递归时 headers 已经不在里面了。
    """
    seen_custom_header = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        seen_custom_header.append(request.headers.get("X-Custom"))
        if len(seen_custom_header) == 1:
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    resp = client.request("GET", "/users", headers={"X-Custom": "value-1"})

    assert resp.status_code == 200
    assert seen_custom_header == ["value-1", "value-1"]  # 第一次和重试那次都带着
