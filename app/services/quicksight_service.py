"""只读核对 Quick 订阅是否已经生效（docs/design.md 4.3 节）。

建号/改档位只是把 Keycloak 组改了，订阅本身要等用户下次登录触发
QuickSubscriptionAssignFunction 才真正生效。这里尽力调 QuickSight
DescribeUser 去确认，但这是锦上添花的信息，不是核心功能——本地没配 AWS
凭证、或者账号还没首次登录过（QuickSight 里还查不到这个人）都是正常情况，
一律优雅降级成"未知"，不能因为这一步失败就把整个用户列表页面搞挂。
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError

    _BOTO_AVAILABLE = True
except ImportError:  # pragma: no cover - boto3 在 requirements.txt 里，这里只是防御
    _BOTO_AVAILABLE = False

# botocore 默认 max_pool_connections=10——生产实测踩过的坑：
# app/api/users.py 把并发从 8 个线程调到 32 个之后，用户列表页耗时只从
# 11 秒降到 9.5 秒，远低于线程数翻 4 倍应有的提升，因为真正的瓶颈根本不是
# 线程数，是这里默认只有 10 个连接的连接池——超过 10 个的线程照样在排队等
# 连接。这个值要 >= app/api/users.py 里 _QS_LOOKUP_WORKERS，不然线程池
# 扩容不会有实际效果，两边改的时候要一起看。
_MAX_POOL_CONNECTIONS = 40


class QuickSightStatusChecker:
    def __init__(self, aws_account_id: str, region: str):
        self._account_id = aws_account_id
        self._client = None
        if _BOTO_AVAILABLE:
            try:
                self._client = boto3.client(
                    "quicksight",
                    region_name=region,
                    config=BotoConfig(max_pool_connections=_MAX_POOL_CONNECTIONS),
                )
            except Exception:  # noqa: BLE001 - 本地没配凭证时构造 client 也可能抛错，一律降级
                logger.warning("QuickSight client 初始化失败，订阅状态将全部显示为未知", exc_info=True)
                self._client = None

    def is_available(self) -> bool:
        return self._client is not None

    def get_subscription_role(self, role_prefix: str, email: str) -> Optional[str]:
        """role_prefix 形如 QuickAuthorProRole，QuickSight 侧用户名是
        f"{role_prefix}/{email}"（现有生产环境端到端验证记录过的真实命名
        格式）。返回 None 表示查不到/未知，不代表订阅
        一定没生效。
        """
        if self._client is None:
            return None
        qs_username = f"{role_prefix}/{email}"
        try:
            resp = self._client.describe_user(
                AwsAccountId=self._account_id, Namespace="default", UserName=qs_username
            )
            return resp.get("User", {}).get("Role")
        except (ClientError, BotoCoreError):
            # 最常见的原因：用户还没首次登录过，QuickSight 侧压根没有这个用户记录，
            # 这是「已建号待激活」的正常状态，不是错误。
            return None
        except Exception:  # noqa: BLE001
            logger.warning("查询 QuickSight 订阅状态失败: %s", qs_username, exc_info=True)
            return None


# Keycloak 组路径 -> QuickSight IAM Role 前缀，用于拼 QuickSight 侧用户名。
# 覆盖全部 6 档（对齐 keycloak_service.ROLE_TO_GROUP，2026-08-20 扩大管理
# 范围之后不再只有作者两档），前缀命名沿用现有生产环境 IAM Role 的实际
# 命名（QuickAdminRole/QuickAdminProRole 等）。
GROUP_TO_ROLE_PREFIX = {
    "/quick-admin": "QuickAdminRole",
    "/quick-admin-pro": "QuickAdminProRole",
    "/quick-author": "QuickAuthorRole",
    "/quick-author-pro": "QuickAuthorProRole",
    "/quick-reader": "QuickReaderRole",
    "/quick-reader-pro": "QuickReaderProRole",
}
