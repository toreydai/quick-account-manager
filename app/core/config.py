from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 这几个值是明显的占位符——本地开发图方便用默认值能跑起来，但绝不能带着这几个
# 值上生产：session_secret_key 泄露等于任何人都能伪造管理员登录态，两个 client
# secret 泄露等于拿到 Keycloak 建号/改密码的权限。
_KNOWN_PLACEHOLDER_VALUES = {
    "session_secret_key": "change-this-to-a-random-secret-in-production",
    "keycloak_service_client_secret": "changeme",
    "keycloak_oidc_client_secret": "changeme",
}
_SECRET_FIELDS = tuple(_KNOWN_PLACEHOLDER_VALUES.keys())
# 只挡"跟这几个已知占位符一字不差"太窄——换成空字符串或 "123" 这种弱密钥照样
# 会被判定成"不是占位符"而放过。改成同时校验长度：非 development 环境这几个
# 字段必须至少 32 位，不管是不是已知占位符。
_MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """从环境变量读配置，见 .env.example 的注释和 docs/design.md 对应章节。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # production（默认）会在启动时拒绝使用上面那几个占位密钥；本地开发把这个
    # 设成 development 才允许用占位值跑起来，避免"忘了改默认值就上线"。
    environment: str = "production"

    # Keycloak
    keycloak_base_url: str = "https://sso.example.com"
    keycloak_realm: str = "quick"

    # 4.1 节：service account client（manage-users），后端调 Admin REST API 用
    keycloak_service_client_id: str = "quick-account-manager-service"
    keycloak_service_client_secret: str = "changeme"

    # 4.4 节：应用自身登录用的 OIDC client
    keycloak_oidc_client_id: str = "quick-account-manager-web"
    keycloak_oidc_client_secret: str = "changeme"
    oidc_redirect_uri: str = "http://localhost:8000/auth/callback"

    # 4.4 节：允许登录本应用的 Keycloak 组
    admin_group_path: str = "/quick-admin-pro"

    session_secret_key: str = "change-this-to-a-random-secret-in-production"

    database_url: str = "sqlite:///./data/app.db"

    aws_region: str = "us-east-1"
    aws_account_id: str = "123456789012"

    @model_validator(mode="after")
    def _reject_weak_secrets_outside_dev(self) -> "Settings":
        if self.environment == "development":
            return self
        problems = []
        for field in _SECRET_FIELDS:
            value = getattr(self, field)
            if value == _KNOWN_PLACEHOLDER_VALUES[field]:
                problems.append(f"{field}：还是默认占位值")
            elif len(value) < _MIN_SECRET_LENGTH:
                problems.append(f"{field}：长度只有 {len(value)} 位，判定为弱密钥（至少要 {_MIN_SECRET_LENGTH} 位）")
        if problems:
            raise ValueError(
                "以下配置不能在非 development 环境启动：" + "；".join(problems) + "。"
                "在 .env / SSM Parameter Store 里覆盖成足够长的真随机值，"
                "或者本地调试时显式设置 ENVIRONMENT=development。"
            )
        return self

    @property
    def oidc_metadata_url(self) -> str:
        return f"{self.keycloak_base_url}/realms/{self.keycloak_realm}/.well-known/openid-configuration"

    @property
    def admin_api_base(self) -> str:
        return f"{self.keycloak_base_url}/admin/realms/{self.keycloak_realm}"

    @property
    def token_endpoint(self) -> str:
        return f"{self.keycloak_base_url}/realms/{self.keycloak_realm}/protocol/openid-connect/token"


@lru_cache
def get_settings() -> Settings:
    return Settings()
