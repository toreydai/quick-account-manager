"""登录态与授权检查（docs/design.md 4.4 节）：应用自身不维护账号密码表，
登录态就是 Keycloak OIDC 的会话结果，权限判断就是"是否在 /quick-admin-pro
组里"。
"""

import logging
from typing import Optional

from fastapi import Request

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "user"


class NotAuthenticated(Exception):
    """未登录，或登录了但不在允许的管理员组里——路由层捕获后重定向去登录页。"""


def get_current_user(request: Request) -> Optional[dict]:
    return request.session.get(SESSION_USER_KEY)


def require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user is None:
        raise NotAuthenticated("未登录")
    return user


def user_is_in_admin_group(groups: list, admin_group_path: str) -> bool:
    """groups 是 ID token / userinfo 里的 groups claim（Keycloak 需要给
    对应 client 配 Group Membership mapper 才会带这个 claim，见 README 前置依赖）。
    """
    return admin_group_path in (groups or [])
