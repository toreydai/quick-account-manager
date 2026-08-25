"""生产环境的密钥校验不能只挡"跟已知占位符一字不差"，换成空字符串/"123"这种
弱密钥也要被拒绝——见 code review 发现的问题：原来只做精确字符串匹配。
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _strong_secret(tag: str) -> str:
    return f"{tag}-" + "x" * 40  # 明显超过 32 位


def test_known_placeholder_rejected_outside_dev():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production")


def test_weak_but_not_placeholder_secret_still_rejected():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            session_secret_key="123",
            keycloak_service_client_secret="short",
            keycloak_oidc_client_secret="",
        )


def test_strong_secrets_pass_in_production():
    settings = Settings(
        _env_file=None,
        environment="production",
        session_secret_key=_strong_secret("session"),
        keycloak_service_client_secret=_strong_secret("svc"),
        keycloak_oidc_client_secret=_strong_secret("oidc"),
    )
    assert settings.environment == "production"


def test_development_environment_allows_placeholders():
    settings = Settings(_env_file=None, environment="development")
    assert settings.session_secret_key == "change-this-to-a-random-secret-in-production"
