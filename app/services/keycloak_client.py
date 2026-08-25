"""薄封装：管 client_credentials token 的获取/缓存/过期重试，以及对 Keycloak
Admin REST API 的通用请求方法。业务逻辑（建号/重置密码/改组/停用）不放在这里，
放在 keycloak_service.py，这层只管"怎么跟 Keycloak 说话"。

对应 docs/design.md 4.1 节：用 quick realm 内的 service account（manage-users
权限）取代人工输入 master realm 管理员密码。
"""

import threading
import time
from typing import Optional

import httpx

from app.core.config import Settings

# token 实际过期时间前提前刷新的秩余量，避免请求发出瞬间 token 刚好过期。
_TOKEN_REFRESH_MARGIN_SECONDS = 30


class KeycloakAuthError(Exception):
    """换 token 失败——多半是 service account client_id/secret 配错了。"""


class KeycloakAPIError(Exception):
    """Admin REST API 调用返回非 2xx。"""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Keycloak API error {status_code}: {body}")


class KeycloakClient:
    def __init__(self, settings: Settings, http_client: Optional[httpx.Client] = None):
        self._settings = settings
        self._http = http_client or httpx.Client(timeout=15)
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def _fetch_token(self) -> None:
        resp = self._http.post(
            self._settings.token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": self._settings.keycloak_service_client_id,
                "client_secret": self._settings.keycloak_service_client_secret,
            },
        )
        if resp.status_code != 200:
            raise KeycloakAuthError(
                f"取 service account token 失败（{resp.status_code}）："
                f"检查 KEYCLOAK_SERVICE_CLIENT_ID/SECRET 是否正确，以及这个 client "
                f"是否已经在 Keycloak 里开启了 Service Account。响应: {resp.text}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.monotonic() + payload.get("expires_in", 60)

    def _get_token(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() >= self._token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
                self._fetch_token()
            return self._token  # type: ignore[return-value]

    def request(self, method: str, path: str, retry_on_401: bool = True, **kwargs) -> httpx.Response:
        """path 是相对于 admin_api_base 的路径，例如 '/users'。"""
        url = f"{self._settings.admin_api_base}{path}"
        token = self._get_token()
        # 调用方自己传的 headers（目前没有调用方这么用，但保留原样以防万一）单独存一份，
        # 不要直接改掉再塞回 kwargs——之前的写法在 401 重试递归时会把这份 headers 弄丢，
        # 因为 pop 出来只用在了这一次请求上，kwargs 里已经没有它了。
        caller_headers = dict(kwargs.pop("headers", {}) or {})
        headers = dict(caller_headers)
        headers["Authorization"] = f"Bearer {token}"
        resp = self._http.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401 and retry_on_401:
            # token 可能被 Keycloak 侧提前吊销/realm 配置变过，强制刷新一次再试。
            with self._lock:
                self._token = None
            retry_kwargs = dict(kwargs)
            if caller_headers:
                retry_kwargs["headers"] = caller_headers
            return self.request(method, path, retry_on_401=False, **retry_kwargs)

        if resp.status_code >= 400:
            raise KeycloakAPIError(resp.status_code, resp.text)
        return resp
