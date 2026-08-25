"""2026-08-20 把管理范围从"只管作者两档"扩到全部 6 档之后引入的真实风险：
`/quick-admin-pro` 组同时是登录 quick-account-manager 本身的门槛（见
app/core/config.py 的 admin_group_path），如果谁都能通过本应用把最后一个
admin-pro 降级/停用，会导致所有人（包括操作者自己）都进不去这个工具，也
没法再用工具自己修。这里测两条防护：不让最后一个 admin-pro 被降级/停用，
以及路由层不让管理员对自己动手（防呆，不是防坏人，见 app/api/users.py）。
"""

import httpx
import pytest

from app.core.config import Settings
from app.services.keycloak_client import KeycloakClient
from app.services.keycloak_service import KeycloakService, LastAdminGuardError


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


def _all_users_response():
    return httpx.Response(
        200,
        json=[
            {"id": "admin-1", "username": "onlyadmin", "email": "onlyadmin@example.com", "enabled": True},
            {"id": "author-1", "username": "someauthor", "email": "author@example.com", "enabled": True},
        ],
    )


def test_change_tier_blocks_demoting_the_last_admin_pro():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return _token_response(request)
        if path.endswith("/users") and request.method == "GET":
            return _all_users_response()
        if path.endswith("/users/admin-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-admin-pro"}])
        if path.endswith("/users/author-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author-pro"}])
        raise AssertionError(f"unexpected call: {request.method} {path}")

    service = make_service(handler)
    with pytest.raises(LastAdminGuardError) as exc_info:
        service.change_tier(user_id="admin-1", new_role="作者版")

    assert "只剩这一个人" in str(exc_info.value)


def test_change_tier_allows_demoting_when_another_admin_pro_remains():
    """有另一个 admin-pro 兜底，降级这个人不会清空这个组，应该放行。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return _token_response(request)
        if path.endswith("/users") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": "admin-1", "username": "a1", "email": "a1@example.com", "enabled": True},
                    {"id": "admin-2", "username": "a2", "email": "a2@example.com", "enabled": True},
                ],
            )
        if path.endswith("/users/admin-1/groups") or path.endswith("/users/admin-2/groups"):
            return httpx.Response(200, json=[{"path": "/quick-admin-pro"}])
        if path.endswith("/groups") and request.method == "GET":
            search = request.url.params.get("search")
            group_map = {
                "quick-author": {"id": "group-author", "path": "/quick-author"},
                "quick-admin-pro": {"id": "group-admin-pro", "path": "/quick-admin-pro"},
            }
            return httpx.Response(200, json=[group_map[search]])
        if request.method == "PUT" and "/groups/group-author" in path:
            return httpx.Response(204)
        if request.method == "DELETE" and "/groups/group-admin-pro" in path:
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {path}")

    service = make_service(handler)
    service.change_tier(user_id="admin-1", new_role="作者版")  # 不应该抛异常


def test_set_enabled_blocks_disabling_the_last_admin_pro():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return _token_response(request)
        if path.endswith("/users") and request.method == "GET":
            return _all_users_response()
        if path.endswith("/users/admin-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-admin-pro"}])
        if path.endswith("/users/author-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author-pro"}])
        raise AssertionError(f"unexpected call: {request.method} {path}")

    service = make_service(handler)
    with pytest.raises(LastAdminGuardError):
        service.set_enabled(user_id="admin-1", enabled=False)


def test_set_enabled_does_not_guard_non_admin_users():
    """普通作者档位用户被停用，不应该触发这条只保护 admin-pro 的检查
    （也就不会多打那个 list_users() 的开销）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/token"):
            return _token_response(request)
        if path.endswith("/users/author-1/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author-pro"}])
        if path.endswith("/users/author-1") and request.method == "PUT":
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {path}")

    service = make_service(handler)
    service.set_enabled(user_id="author-1", enabled=False)  # 不应该抛异常
    assert not any(p.endswith("/admin/realms/quick/users") for p in calls)  # 没有触发 list_users()
