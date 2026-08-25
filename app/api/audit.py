from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.audit_log import AuditAction
from app.services import audit_service

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/audit-log", response_class=HTMLResponse)
async def audit_log_list(
    request: Request,
    db: Session = Depends(get_db),
    actor_email: str = "",
    target_email: str = "",
    action: str = "",
    result: str = "",  # "", "success", "failed" —— 跟 AuditAction 的值域分开，避免和 action 参数混淆
):
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    action_filter: Optional[AuditAction] = None
    if action:
        try:
            action_filter = AuditAction[action]
        except KeyError:
            # URL 被手动改过传了个不存在的枚举名——当没筛选处理，不 500。
            action_filter = None

    success_filter: Optional[bool] = None
    if result == "success":
        success_filter = True
    elif result == "failed":
        success_filter = False

    entries = audit_service.list_recent(
        db,
        actor_email=actor_email or None,
        target_email=target_email or None,
        action=action_filter,
        success=success_filter,
    )
    return templates.TemplateResponse(
        "audit/list.html",
        {
            "request": request,
            "current_user": user,
            "entries": entries,
            "actions": list(AuditAction),
            "filters": {
                "actor_email": actor_email,
                "target_email": target_email,
                "action": action,
                "result": result,
            },
        },
    )
