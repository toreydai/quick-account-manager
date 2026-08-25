"""Keycloak OIDC 登录（docs/design.md 4.4 节）。这个 client 跟 4.1 节给后端调
Admin REST API 用的 service account client 是两个不同的 client，职责分开。
"""

import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.core.security import SESSION_USER_KEY, user_is_in_admin_group

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

settings = get_settings()

oauth = OAuth()
oauth.register(
    name="keycloak",
    client_id=settings.keycloak_oidc_client_id,
    client_secret=settings.keycloak_oidc_client_secret,
    server_metadata_url=settings.oidc_metadata_url,
    client_kwargs={"scope": "openid email profile"},
)


@router.get("/login")
async def login(request: Request):
    return await oauth.keycloak.authorize_redirect(request, settings.oidc_redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    token = await oauth.keycloak.authorize_access_token(request)
    userinfo = token.get("userinfo") or await oauth.keycloak.userinfo(token=token)

    groups = userinfo.get("groups", [])
    if not groups:
        # Keycloak 默认不会把 group membership 塞进 token/userinfo，需要给这个
        # OIDC client 配一个 Group Membership mapper 才会带 groups claim——
        # 这是部署前置依赖，不是代码 bug，见 README「部署前置依赖」一节。
        logger.warning(
            "登录用户 %s 的 token 里没有 groups claim，"
            "请确认 Keycloak OIDC client 已配置 Group Membership mapper",
            userinfo.get("email"),
        )

    if not user_is_in_admin_group(groups, settings.admin_group_path):
        return RedirectResponse(url="/auth/forbidden", status_code=303)

    request.session[SESSION_USER_KEY] = {
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name") or userinfo.get("preferred_username", ""),
    }
    return RedirectResponse(url="/", status_code=303)


@router.get("/forbidden")
async def forbidden():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(
        "<h1>没有权限</h1><p>你的账号不在 /quick-admin-pro 组里，无法使用这个管理后台。"
        "如果你应该有权限，找内部管理员在 Keycloak 里把你加进这个组。</p>",
        status_code=403,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
