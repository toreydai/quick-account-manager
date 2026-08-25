"""create_user / change_tier 中间步骤失败时的补偿/重试逻辑（code review 发现：
原来两步 Keycloak 调用之间没有任何失败处理，会留下孤儿账号或"两个组都在"的
不一致状态，且应用会报"失败"但 Keycloak 里其实已经发生了变化）。
"""

import httpx
import pytest

from app.core.config import Settings
from app.services.keycloak_client import KeycloakClient
from app.services.keycloak_service import KeycloakService, PartialFailureError


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


# ---------- create_user：加组失败触发回滚删除 ----------


def test_create_user_rolls_back_when_group_assignment_fails():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "POST" and request.url.path.endswith("/users"):
            return httpx.Response(
                201, headers={"Location": "https://fake-keycloak.example.com/admin/realms/quick/users/user-123"}
            )
        if request.method == "GET" and request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "group-abc", "path": "/quick-author"}])
        if request.method == "PUT" and "/groups/" in request.url.path:
            return httpx.Response(500, text="group service unavailable")
        if request.method == "DELETE" and request.url.path.endswith("/users/user-123"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    with pytest.raises(PartialFailureError) as exc_info:
        service.create_user(email="a@example.com", first_name="A", last_name="B", role="作者版")

    assert "已自动回滚删除" in str(exc_info.value)
    assert ("DELETE", "/admin/realms/quick/users/user-123") in calls


def test_create_user_reports_orphan_when_rollback_also_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "POST" and request.url.path.endswith("/users"):
            return httpx.Response(
                201, headers={"Location": "https://fake-keycloak.example.com/admin/realms/quick/users/user-123"}
            )
        if request.method == "GET" and request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[{"id": "group-abc", "path": "/quick-author"}])
        if request.method == "PUT" and "/groups/" in request.url.path:
            return httpx.Response(500, text="group service unavailable")
        if request.method == "DELETE" and request.url.path.endswith("/users/user-123"):
            return httpx.Response(500, text="delete also failed")
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    with pytest.raises(PartialFailureError) as exc_info:
        service.create_user(email="a@example.com", first_name="A", last_name="B", role="作者版")

    message = str(exc_info.value)
    assert "孤儿账号" in message
    assert "user-123" in message


# ---------- change_tier：删旧组失败先重试一次，再失败才报 PartialFailureError ----------


def test_change_tier_retries_delete_once_before_giving_up():
    delete_attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "GET" and request.url.path.endswith(f"/users/user-1/groups"):
            return httpx.Response(200, json=[{"id": "group-old", "path": "/quick-author"}])
        if request.method == "GET" and request.url.path.endswith("/groups"):
            search = request.url.params.get("search")
            if search == "quick-author-pro":
                return httpx.Response(200, json=[{"id": "group-new", "path": "/quick-author-pro"}])
            return httpx.Response(200, json=[{"id": "group-old", "path": "/quick-author"}])
        if request.method == "PUT" and request.url.path.endswith("/groups/group-new"):
            return httpx.Response(204)
        if request.method == "DELETE" and request.url.path.endswith("/groups/group-old"):
            delete_attempts["count"] += 1
            if delete_attempts["count"] == 1:
                return httpx.Response(500, text="transient")
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    service.change_tier(user_id="user-1", new_role="作者专业版")

    assert delete_attempts["count"] == 2  # 第一次失败，重试一次成功


def test_change_tier_raises_partial_failure_when_delete_fails_twice():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response(request)
        if request.method == "GET" and request.url.path.endswith("/users/user-1/groups"):
            return httpx.Response(200, json=[{"id": "group-old", "path": "/quick-author"}])
        if request.method == "GET" and request.url.path.endswith("/groups"):
            search = request.url.params.get("search")
            if search == "quick-author-pro":
                return httpx.Response(200, json=[{"id": "group-new", "path": "/quick-author-pro"}])
            return httpx.Response(200, json=[{"id": "group-old", "path": "/quick-author"}])
        if request.method == "PUT" and request.url.path.endswith("/groups/group-new"):
            return httpx.Response(204)
        if request.method == "DELETE" and request.url.path.endswith("/groups/group-old"):
            return httpx.Response(500, text="still down")
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    service = make_service(handler)
    with pytest.raises(PartialFailureError) as exc_info:
        service.change_tier(user_id="user-1", new_role="作者专业版")

    message = str(exc_info.value)
    assert "同时在两个组里" in message
    assert "/quick-author" in message  # 明确指出该去 Keycloak 手动删哪个组
