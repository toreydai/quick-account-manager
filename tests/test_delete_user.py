"""KeycloakService.delete_user()：硬删除（design.md 4.5 节原来评估后决定留作
后续迭代，现已启用）。测 service 层的两条规则——必须先停用
（MustDisableFirstError，2026-08-24 加）、复用最后一个管理员保护；"不能删
自己"是路由层（app/api/users.py）的防呆检查，不在 service 层，见 tests 里
没有覆盖这条的原因说明（跟 change_tier/set_enabled 的自锁检查一样，都放在
路由测试的空白里，README 已经说明这几个接口靠对着本地真实 Keycloak 手工
端到端验证，不是长期自动化测试的一部分）。
"""

import httpx
import pytest

from app.core.config import Settings
from app.services.keycloak_client import KeycloakClient
from app.services.keycloak_service import KeycloakService, LastAdminGuardError, MustDisableFirstError


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


def _token_response(request: httpx.Request):
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})


def test_delete_user_calls_delete_endpoint_when_already_disabled():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "GET" and request.url.path.endswith("/users/author-1"):
            return httpx.Response(
                200, json={"id": "author-1", "username": "author1", "email": "author1@example.com", "enabled": False}
            )
        if request.url.path.endswith("/users/author-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author"}])
        if request.method == "DELETE" and request.url.path.endswith("/users/author-1"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    service.delete_user("author-1")

    assert ("DELETE", "/admin/realms/quick/users/author-1") in calls


def test_delete_user_blocks_when_target_still_enabled():
    """2026-08-24 加的第二层防呆：硬删除前必须先停用，账号还是启用状态就
    直接拒绝，不打 DELETE 请求。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "GET" and request.url.path.endswith("/users/author-1"):
            return httpx.Response(
                200, json={"id": "author-1", "username": "author1", "email": "author1@example.com", "enabled": True}
            )
        if request.url.path.endswith("/users/author-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author"}])
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    with pytest.raises(MustDisableFirstError) as exc_info:
        service.delete_user("author-1")

    assert "author1@example.com" in str(exc_info.value)
    assert not any(m == "DELETE" for m, _ in calls)  # 没有真的打过 DELETE


def test_delete_user_blocks_deleting_the_last_admin_pro():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "GET" and request.url.path.endswith("/users/admin-1"):
            return httpx.Response(
                200, json={"id": "admin-1", "username": "onlyadmin", "email": "onlyadmin@example.com", "enabled": False}
            )
        if request.url.path.endswith("/users") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": "admin-1", "username": "onlyadmin", "email": "onlyadmin@example.com", "enabled": False},
                ],
            )
        if request.url.path.endswith("/users/admin-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-admin-pro"}])
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    with pytest.raises(LastAdminGuardError):
        service.delete_user("admin-1")


def test_delete_user_allows_deleting_admin_pro_when_another_remains():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "GET" and request.url.path.endswith("/users/admin-1"):
            return httpx.Response(
                200, json={"id": "admin-1", "username": "a1", "email": "a1@example.com", "enabled": False}
            )
        if request.url.path.endswith("/users") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": "admin-1", "username": "a1", "email": "a1@example.com", "enabled": False},
                    {"id": "admin-2", "username": "a2", "email": "a2@example.com", "enabled": True},
                ],
            )
        if request.url.path.endswith("/users/admin-1/groups") or request.url.path.endswith("/users/admin-2/groups"):
            return httpx.Response(200, json=[{"path": "/quick-admin-pro"}])
        if request.method == "DELETE" and request.url.path.endswith("/users/admin-1"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    service.delete_user("admin-1")  # 不应该抛异常
