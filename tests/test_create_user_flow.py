"""create_user() 的 HTTP 调用序列（mock transport，不连真实 Keycloak）。

这里改成 POST /users + PUT groups 两步，是本地端到端测试对着真实 Keycloak
26.6.3 打的时候发现的：原来的 POST /partialImport 对 4.1 节配的最小权限
service account（即便有 manage-users/query-users/query-groups/view-users）也会返回 403——
partialImport 的权限检查粒度比 manage-users 粗，不是"能管用户"就能调这个
接口。这几个用例锁定新的调用序列，避免以后不小心又改回 partialImport。
"""

import httpx

from app.core.config import Settings
from app.services.keycloak_client import KeycloakClient
from app.services.keycloak_service import KeycloakService


def make_service(handler):
    settings = Settings(
        environment="development",
        keycloak_base_url="https://fake-keycloak.example.com",
        keycloak_realm="quick",
        keycloak_service_client_id="svc",
        keycloak_service_client_secret="secret",
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = KeycloakClient(settings, http_client=http)
    return KeycloakService(client)


def test_create_user_posts_to_users_not_partial_import():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if request.method == "POST" and request.url.path.endswith("/users"):
            return httpx.Response(
                201,
                headers={"Location": "https://fake-keycloak.example.com/admin/realms/quick/users/user-123"},
            )
        if request.method == "GET" and request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "group-abc", "path": "/quick-author"}])
        if request.method == "PUT" and "/groups/" in request.url.path:
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    result = service.create_user(email="a@example.com", first_name="A", last_name="B", role="作者版")

    assert result.created is True
    assert result.password is not None
    paths = [p for _, p in calls]
    assert not any("partialImport" in p for p in paths)
    assert any(p.endswith("/users") for p in paths)
    assert any("/groups/group-abc" in p for p in paths)


def test_create_user_sends_chinese_name_as_keycloak_attribute_when_given():
    """建号表单的"中文姓名"字段之前收了从来没用过，静默丢弃（code review
    发现的问题）。现在要写进 Keycloak 用户的 attributes.chineseName。"""
    posted_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if request.method == "POST" and request.url.path.endswith("/users"):
            posted_payloads.append(request.content)
            return httpx.Response(
                201, headers={"Location": "https://fake-keycloak.example.com/admin/realms/quick/users/user-123"}
            )
        if request.method == "GET" and request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "group-abc", "path": "/quick-author"}])
        if request.method == "PUT" and "/groups/" in request.url.path:
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    service.create_user(email="a@example.com", first_name="A", last_name="B", role="作者版", chinese_name="薛惠坤")

    import json

    body = json.loads(posted_payloads[0])
    assert body["attributes"] == {"chineseName": ["薛惠坤"]}


def test_create_user_omits_attributes_when_no_chinese_name_given():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if request.method == "POST" and request.url.path.endswith("/users"):
            import json

            body = json.loads(request.content)
            assert "attributes" not in body
            return httpx.Response(
                201, headers={"Location": "https://fake-keycloak.example.com/admin/realms/quick/users/user-123"}
            )
        if request.method == "GET" and request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "group-abc", "path": "/quick-author"}])
        if request.method == "PUT" and "/groups/" in request.url.path:
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    result = service.create_user(email="a@example.com", first_name="A", last_name="B", role="作者版")
    assert result.created is True


def test_create_user_sends_default_roles_quick_explicitly():
    """2026-08-21：不依赖 Keycloak 自动赋默认角色，显式声明 realmRoles，
    双保险防住现有命令行建号脚本踩过的漏赋坑。"""
    posted_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if request.method == "POST" and request.url.path.endswith("/users"):
            posted_payloads.append(request.content)
            return httpx.Response(
                201, headers={"Location": "https://fake-keycloak.example.com/admin/realms/quick/users/user-123"}
            )
        if request.method == "GET" and request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "group-abc", "path": "/quick-author"}])
        if request.method == "PUT" and "/groups/" in request.url.path:
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    service.create_user(email="a@example.com", first_name="A", last_name="B", role="作者版")

    import json

    body = json.loads(posted_payloads[0])
    assert body["realmRoles"] == ["default-roles-quick"]


def test_create_user_treats_409_as_skip_not_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if request.method == "POST" and request.url.path.endswith("/users"):
            return httpx.Response(409, text="User exists with same username")
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    result = service.create_user(email="dup@example.com", first_name="A", last_name="B", role="作者版")

    assert result.created is False
    assert result.password is None
