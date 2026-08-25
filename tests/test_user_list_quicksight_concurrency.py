"""生产实测踩过的坑，两轮：

1. QuickSight 订阅状态原来对每个作者档位用户串行查询，单次约 1.7 秒，30+
   个真实用户跑下来接近 60 秒，卡到 ALB 默认 idle timeout（60s）直接给
   前端返回 504。改成线程池并发查询解决了卡死，但用户列表页整体还是要
   4-8 秒（Keycloak 数据 + QuickSight 数据都查完才渲染），反馈"非常慢"。

2. 改成异步：user_list 路由现在只拉 Keycloak 数据（_fetch_users），不等
   QuickSight；订阅状态拆到单独的 _build_subscription_status_map，由前端
   页面加载完之后另发请求去 /users/subscription-status 取（见
   app/static/subscription-status.js）。这里测的是拆开之后的两个函数：
   _fetch_users 不该等 QuickSight，_build_subscription_status_map 的并发
   行为要保持（不能退化回串行）。
"""

import time

from app.api.users import _build_subscription_status_map, _fetch_users
from app.services.keycloak_service import QuickUser

SLOW_CALL_SECONDS = 0.2
USER_COUNT = 10


class FakeKeycloakService:
    def __init__(self, users):
        self._users = users

    def list_users(self):
        return self._users


class FakeSlowQuickSightChecker:
    """每次查询都模拟真实网络耗时，用来证明并发 vs 串行的墙钟时间差异。"""

    def is_available(self):
        return True

    def get_subscription_role(self, role_prefix, email):
        time.sleep(SLOW_CALL_SECONDS)
        return "AUTHOR"


def _make_users(n):
    return [
        QuickUser(
            id=f"u{i}",
            username=f"user{i}",
            email=f"user{i}@example.com",
            first_name="F",
            last_name="L",
            enabled=True,
            role="作者版",
        )
        for i in range(n)
    ]


def test_fetch_users_does_not_touch_quicksight():
    """页面首屏只查 Keycloak——这是这一轮异步化的核心诉求，_fetch_users 传进去
    的 kc 不应该有任何跟 QuickSight 相关的调用。"""
    kc = FakeKeycloakService(_make_users(USER_COUNT))

    start = time.monotonic()
    users, kc_error = _fetch_users(kc)
    elapsed = time.monotonic() - start

    assert kc_error is None
    assert len(users) == USER_COUNT
    # 没有传 qs 进去，函数签名上就不可能碰 QuickSight；这里主要确认它本身
    # 很快（没有意外的 sleep/阻塞），配合上面的说明用例保证语义。
    assert elapsed < 0.1


def test_subscription_status_lookups_run_concurrently_not_sequentially():
    users = _make_users(USER_COUNT)
    qs = FakeSlowQuickSightChecker()

    start = time.monotonic()
    status_map = _build_subscription_status_map(users, qs)
    elapsed = time.monotonic() - start

    assert len(status_map) == USER_COUNT
    assert all(v == "已生效" for v in status_map.values())

    # 串行的话至少要 USER_COUNT * SLOW_CALL_SECONDS = 2 秒；32 个并发 worker
    # 跑 10 个 0.2 秒的任务，理论上一批就完事。给足够宽松的上限（1 秒），
    # 既能容忍 CI 环境抖动，又能在回归到串行实现时必然失败。
    assert elapsed < 1.0, f"耗时 {elapsed:.2f}s，看起来是串行跑的，不是并发"


def test_subscription_status_map_keyed_by_user_id():
    users = _make_users(5)
    qs = FakeSlowQuickSightChecker()

    status_map = _build_subscription_status_map(users, qs)

    assert set(status_map.keys()) == {f"u{i}" for i in range(5)}


def test_subscription_status_skips_users_without_role():
    users = _make_users(3)
    users.append(
        QuickUser(
            id="u-no-role", username="x", email="x@example.com", first_name="F", last_name="L",
            enabled=True, role=None,
        )
    )
    qs = FakeSlowQuickSightChecker()

    status_map = _build_subscription_status_map(users, qs)

    assert "u-no-role" not in status_map
    assert len(status_map) == 3
