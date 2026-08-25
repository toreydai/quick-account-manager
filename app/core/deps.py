"""共享的单例依赖：KeycloakClient 自己管 token 缓存，不应该每个请求都新建一个
（那样每次都要重新换 token）；QuickSightStatusChecker 内部的 boto3 client 同理。
"""

from functools import lru_cache

from app.core.config import get_settings
from app.services.keycloak_client import KeycloakClient
from app.services.keycloak_service import KeycloakService
from app.services.quicksight_service import QuickSightStatusChecker


@lru_cache
def get_keycloak_client() -> KeycloakClient:
    return KeycloakClient(get_settings())


@lru_cache
def get_keycloak_service() -> KeycloakService:
    return KeycloakService(get_keycloak_client())


@lru_cache
def get_quicksight_checker() -> QuickSightStatusChecker:
    settings = get_settings()
    return QuickSightStatusChecker(settings.aws_account_id, settings.aws_region)
