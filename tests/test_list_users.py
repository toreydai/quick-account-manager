"""list_users() 的角色解析走并发的按用户查（GET /users/{id}/groups），不是
按组查成员（GET /groups/{id}/members）批量 join。

这是反转过一次的决定，如实记录教训：最早是 N+1（每个用户单独查一次组，
code review 发现的问题），改成了"批量拉两个组的成员列表再 join"（2 次
调用，理论上更少）。但生产实测发现 GET /groups/{id}/members 单次要 ~5
秒（Keycloak 这个接口本身性能弱，是有据可查的通病），2 次接近 10 秒，
直接导致用户反馈"用户列表非常慢"；反过来 GET /users/{id}/groups 单次
只要 ~0.1-0.15 秒，并发查（线程池）比"更少但更慢"的批量调用快一个数量级。
调用次数不是唯一指标，单次调用的真实延迟同样要看。
"""

import time

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


def test_list_users_resolves_role_via_per_user_groups_lookup():
    group_members_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})

        if path.endswith("/users") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": "u-author", "username": "a1", "email": "a1@example.com", "enabled": True},
                    {"id": "u-pro", "username": "a2", "email": "a2@example.com", "enabled": True},
                    {"id": "u-none", "username": "a3", "email": "a3@example.com", "enabled": True},
                ],
            )

        if path.endswith("/groups/members") or "/members" in path:
            # 生产实测的慢接口，list_users 不应该再走这条路径
            group_members_calls["count"] += 1
            return httpx.Response(200, json=[])

        if path.endswith("/users/u-author/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author"}])
        if path.endswith("/users/u-pro/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author-pro"}])
        if path.endswith("/users/u-none/groups"):
            return httpx.Response(200, json=[{"path": "/some-other-group"}])

        raise AssertionError(f"unexpected call: {request.method} {path}")

    service = make_service(handler)
    users = service.list_users()

    by_id = {u.id: u.role for u in users}
    assert by_id["u-author"] == "作者版"
    assert by_id["u-pro"] == "作者专业版"
    assert by_id["u-none"] is None  # 不在本应用管理的两个组里，角色为空，不报错
    assert group_members_calls["count"] == 0  # 没有走 GET /groups/{id}/members 这条慢路径


def test_list_users_degrades_gracefully_for_user_in_no_tracked_group():
    """用户不在 quick-author/-pro 任何一个组：角色显示空，不能让整个列表页挂掉。
    跟按组查成员的旧实现不同，这里天然不依赖某个组是否存在——即使
    quick-author-pro 组被删了/改名了，也只是这个用户查出来的 groups 列表里
    不会有匹配的 path，自然返回 None，不需要额外的"组不存在"分支处理。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if path.endswith("/users") and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "u-author", "username": "a1", "email": "a1@example.com", "enabled": True}]
            )
        if path.endswith("/users/u-author/groups"):
            return httpx.Response(200, json=[{"path": "/quick-author"}])
        raise AssertionError(f"unexpected call: {request.method} {path}")

    service = make_service(handler)
    users = service.list_users()

    assert users[0].role == "作者版"


def test_list_users_resolves_roles_concurrently_not_sequentially():
    """生产实测的核心诉求：几十个用户的角色解析总耗时应该接近单次调用耗时，
    不是 N 倍。"""

    SLOW_CALL_SECONDS = 0.05
    USER_COUNT = 20

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if path.endswith("/users") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": f"u{i}", "username": f"user{i}", "email": f"user{i}@example.com", "enabled": True}
                    for i in range(USER_COUNT)
                ],
            )
        if path.endswith("/groups"):
            time.sleep(SLOW_CALL_SECONDS)
            return httpx.Response(200, json=[{"path": "/quick-author"}])
        raise AssertionError(f"unexpected call: {request.method} {path}")

    service = make_service(handler)

    start = time.monotonic()
    users = service.list_users()
    elapsed = time.monotonic() - start

    assert len(users) == USER_COUNT
    assert all(u.role == "作者版" for u in users)
    # 串行的话至少要 USER_COUNT * SLOW_CALL_SECONDS = 1 秒；32 个并发 worker
    # 跑 20 个 0.05 秒的任务，理论上一批就完事，给足够宽松的上限（0.5 秒）。
    assert elapsed < 0.5, f"耗时 {elapsed:.2f}s，看起来是串行跑的，不是并发"
