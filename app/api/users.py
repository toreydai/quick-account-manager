"""用户管理路由：建号、重置密码、订阅档位变更、账号停用/启用、用户列表。
对应 docs/design.md 2 节 MVP 范围 + 4.2/4.5 节的具体 API 调用规则。

所有调用 KeycloakService/QuickSightStatusChecker 的地方都过一层
run_in_threadpool——这两个 client 内部是同步 httpx/boto3 调用，路由是
async def，直接调用会在请求期间独占事件循环线程，Keycloak 响应慢时会连
/health 这种无关请求都一起卡住（code review 发现的问题）。
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_keycloak_service, get_quicksight_checker
from app.core.security import get_current_user
from app.models.audit_log import AuditAction
from app.services import audit_service
from app.services.batch_import import build_template_xlsx, parse_xlsx
from app.services.export_service import build_user_list_xlsx
from app.services.keycloak_client import KeycloakAPIError
from app.services.keycloak_service import (
    CREATE_ALLOWED_ROLES,
    ROLE_TO_GROUP,
    KeycloakService,
    LastAdminGuardError,
    MustDisableFirstError,
    PartialFailureError,
    RoleNotAllowedError,
    SelfLockoutError,
    UserNotFoundError,
    VALID_ROLES,
)
from app.services.quicksight_service import GROUP_TO_ROLE_PREFIX, QuickSightStatusChecker

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _require_login(request: Request):
    """不是 FastAPI Depends 异常式的写法，而是每个路由手动调用——原因见
    app/core/security.py：保持简单，不引入全局异常处理器的额外间接层，
    3 个人用的内部工具没必要为这个加复杂度。
    """
    return get_current_user(request)


# 8 个一批时实测 30 多个真实用户还是要 11 秒（用户反馈"非常慢"）——用户
# 规模现在只有几十人，一次性全并发也不会对 QuickSight 只读接口造成有意义的
# 压力（DescribeUser 是轻量只读调用，几十次并发、一天就发生几次的页面加载，
# 远够不到任何有意义的限流阈值）。调大到 32，基本覆盖现有用户量一次批完；
# 后续用户量涨到远超 32 再考虑分批/异步加载这类更复杂的方案。
# 这个值必须 <= quicksight_service.py 里 boto3 client 的 max_pool_connections
# （已同步调到 40），不然线程数再多也会卡在 botocore 默认 10 个连接的连接池上
# ——这是第一次调大到 32 时踩过的坑，光调线程数没用，两边要一起改。
_QS_LOOKUP_WORKERS = 32


def _fetch_users(kc: KeycloakService):
    """只拉 Keycloak 数据，不碰 QuickSight——用户反馈"用户列表非常慢"，就算
    Keycloak 那边已经优化到 ~2 秒，加上 QuickSight 那部分（正常情况下也要
    ~3 秒，真实网络波动下能到 6-8 秒）整个页面还是感觉卡。改成页面先秒开
    Keycloak 数据，订阅状态单独一个异步接口（见下面 subscription_status_json），
    前端拿到页面后再发一次后台请求补上，见 app/static/subscription-status.js。
    """
    try:
        users = kc.list_users()
        return users, None
    except KeycloakAPIError as exc:
        # 用户列表实时查 Keycloak（design.md 2 节），查不到就是查不到，不要用
        # 本地缓存假装有数据，直接把错误显示出来让管理员知道该去查 Keycloak 那边。
        logger.error("拉取用户列表失败: %s", exc)
        return [], str(exc)


def _build_subscription_status_map(users, qs: QuickSightStatusChecker) -> dict:
    """并发查每个有角色的用户的 QuickSight 订阅状态，返回 {user_id: 状态文案}。

    生产实测踩过的坑：QuickSight 查询原来是逐个用户串行调用，单次约 1.7 秒，
    真实 30+ 个作者档位用户跑下来接近 60 秒，卡到 ALB 默认 idle timeout
    (60s) 直接给前端返回 504——不是网络或权限问题（单次调用测过是正常的），
    纯粹是没并发。改成线程池并发查询，boto3 client 并发调用是线程安全的
    （官方文档保证），几十个人的查询墙钟时间从"逐个相加"降到"跟最慢那一个
    差不多"。也实测过用 quicksight:ListUsers 一次拿全部人的方案，反而比
    33 个并发的 DescribeUser 更慢（单次 4.3-4.8s vs 并发约 2.8s），没有
    改成那个看起来更"批量"但实测更慢的方案。
    """
    targets = [u for u in users if u.role and qs.is_available()]
    if not targets:
        return {}

    def _lookup(u):
        group_path = ROLE_TO_GROUP.get(u.role)
        role_prefix = GROUP_TO_ROLE_PREFIX.get(group_path) if group_path else None
        if not role_prefix:
            return u.id, "未知"
        qs_role = qs.get_subscription_role(role_prefix, u.email)
        return u.id, ("已生效" if qs_role else "待首次登录激活")

    with ThreadPoolExecutor(max_workers=_QS_LOOKUP_WORKERS) as pool:
        results = list(pool.map(_lookup, targets))

    return dict(results)


@router.get("/", response_class=HTMLResponse)
async def user_list(
    request: Request,
    kc: KeycloakService = Depends(get_keycloak_service),
):
    """页面本身不查 QuickSight——只拉 Keycloak 数据，秒开。订阅状态那一列
    先渲染成"查询中…"占位，前端加载完页面后另发一个后台请求去
    subscription_status_json 补上，见 app/static/subscription-status.js。
    """
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    users, kc_error = await run_in_threadpool(_fetch_users, kc)
    rows = [{"user": u} for u in users]

    return templates.TemplateResponse(
        "users/list.html",
        {
            "request": request,
            "current_user": user,
            "rows": rows,
            "kc_error": kc_error,
            "valid_roles": VALID_ROLES,
            "create_roles": CREATE_ALLOWED_ROLES,
            "flash": request.query_params.get("flash"),
        },
    )


@router.get("/users/subscription-status")
async def subscription_status_json(
    request: Request,
    kc: KeycloakService = Depends(get_keycloak_service),
    qs: QuickSightStatusChecker = Depends(get_quicksight_checker),
):
    """页面加载完之后前端异步调这个接口补订阅状态，不阻塞首屏渲染。返回
    {user_id: 状态文案}，只包含有角色（作者版/作者专业版）的用户——前端按
    user_id 匹配不到的行保持"—"（不在本应用管理范围内，本来就没有订阅状态
    这回事）。
    """
    user = _require_login(request)
    if user is None:
        return {"error": "unauthorized"}, 401

    users, kc_error = await run_in_threadpool(_fetch_users, kc)
    if kc_error:
        return {"error": kc_error}, 502

    status_map = await run_in_threadpool(_build_subscription_status_map, users, qs)
    return status_map


@router.get("/users/export")
async def export_users(
    request: Request,
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
    qs: QuickSightStatusChecker = Depends(get_quicksight_checker),
):
    """一键导出当前用户清单（xlsx）。列表页首屏不等订阅状态（见
    subscription_status_json 的说明），但导出是管理员主动点一下、本来就
    预期要等的低频操作，这里直接把订阅状态查完整再生成文件，导出一份
    就是完整的一份，不用像页面那样分两次请求拼。
    """
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    users, kc_error = await run_in_threadpool(_fetch_users, kc)
    if kc_error:
        return RedirectResponse(url=f"/?flash=导出失败：{kc_error}", status_code=303)

    status_map = await run_in_threadpool(_build_subscription_status_map, users, qs)
    content = await run_in_threadpool(build_user_list_xlsx, users, status_map)

    # 导出的是全体用户的邮箱/角色档位，不算高敏感（跟建号密码不是一个量级），
    # 但也是批量个人信息，跟其它操作一样记一笔审计——谁在什么时候导出过。
    audit_service.record(
        db, user["email"], AuditAction.EXPORT_USERS, "（全部用户）", detail=f"共 {len(users)} 人", success=True
    )

    filename = f"quick-sso-用户清单-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                "attachment; filename=quick-sso-user-list.xlsx; "
                "filename*=UTF-8''" + quote(filename)
            )
        },
    )


@router.post("/users/create", response_class=HTMLResponse)
async def create_user(
    request: Request,
    email: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    role: str = Form(...),
    name: str = Form(""),
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    try:
        result = await run_in_threadpool(
            kc.create_user,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=role,
            chinese_name=name,
        )
    except RoleNotAllowedError as exc:
        audit_service.record(
            db,
            user["email"],
            AuditAction.CREATE_USER,
            email,
            target_name=name,
            detail=role,
            success=False,
            error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash=建号失败：{exc}", status_code=303)
    except PartialFailureError as exc:
        # 账号在 Keycloak 里已经处于不一致状态（孤儿账号，或回滚也失败），
        # 这个必须让管理员当场看到，不能只藏在审计日志里。
        audit_service.record(
            db,
            user["email"],
            AuditAction.CREATE_USER,
            email,
            target_name=name,
            detail=role,
            success=False,
            error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash=建号出现异常状态，需要人工处理：{exc}", status_code=303)
    except (KeycloakAPIError, UserNotFoundError) as exc:
        audit_service.record(
            db,
            user["email"],
            AuditAction.CREATE_USER,
            email,
            target_name=name,
            detail=role,
            success=False,
            error_message=str(exc),
        )
        return RedirectResponse(url="/?flash=建号失败，请检查 Keycloak 连接或联系管理员", status_code=303)

    if not result.created:
        audit_service.record(
            db,
            user["email"],
            AuditAction.CREATE_USER,
            email,
            target_name=name,
            detail=role,
            success=False,
            error_message="username 已存在，被 Keycloak SKIP",
        )
        return RedirectResponse(url=f"/?flash=建号失败：{email} 对应的用户名已存在", status_code=303)

    audit_service.record(
        db, user["email"], AuditAction.CREATE_USER, email, target_name=name, detail=role, success=True
    )

    return templates.TemplateResponse(
        "users/secret_result.html",
        {
            "request": request,
            "current_user": user,
            "title": "建号成功",
            "target_email": email,
            "password": result.password,
        },
    )


@router.get("/users/batch-create", response_class=HTMLResponse)
async def batch_create_form(request: Request):
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    return templates.TemplateResponse(
        "users/batch_upload.html",
        {"request": request, "current_user": user, "flash": request.query_params.get("flash")},
    )


@router.get("/users/batch-create/template")
async def batch_create_template(request: Request):
    """给管理员下载的批量建号 xlsx 模板，表头 + 示例行 + role 列下拉限定
    CREATE_ALLOWED_ROLES，减少手填出错。不要求登录态之外的额外权限——
    跟看 batch_create_form 页面本身是同一个门槛。
    """
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    content = await run_in_threadpool(build_template_xlsx)
    filename = "quick-sso-批量建号模板.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            # 中文文件名走 RFC 5987 的 filename*，同时给一个 ASCII 兜底
            # filename，避免个别老旧客户端不认 filename* 时文件名变成乱码。
            "Content-Disposition": (
                "attachment; filename=quick-sso-batch-template.xlsx; "
                "filename*=UTF-8''" + quote(filename)
            )
        },
    )


@router.post("/users/batch-create", response_class=HTMLResponse)
async def batch_create_preview(request: Request, file: UploadFile = File(...)):
    """上传 xlsx，只解析和校验，不建号——预览页确认无误后再提交到
    /users/batch-create/confirm 才真正调用 Keycloak，跟
    create_users_from_xlsx.py 的 dry-run/--apply 两步习惯保持一致。
    """
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    content = await file.read()
    try:
        rows = await run_in_threadpool(parse_xlsx, content)
    except ValueError as exc:
        return RedirectResponse(url=f"/users/batch-create?flash=解析失败：{exc}", status_code=303)

    if not rows:
        return RedirectResponse(url="/users/batch-create?flash=xlsx 里没有数据行", status_code=303)

    valid_rows = [r for r in rows if r.valid]
    invalid_rows = [r for r in rows if not r.valid]

    return templates.TemplateResponse(
        "users/batch_preview.html",
        {
            "request": request,
            "current_user": user,
            "valid_rows": valid_rows,
            "invalid_rows": invalid_rows,
            "total": len(rows),
        },
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/users/batch-create/confirm")
async def batch_create_confirm(
    request: Request,
    emails: List[str] = Form(...),
    first_names: List[str] = Form(...),
    last_names: List[str] = Form(...),
    usernames: List[str] = Form(...),
    roles: List[str] = Form(...),
    names: List[str] = Form(...),
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    """预览页确认后真正逐个建号，走 SSE（text/event-stream）逐行推送进度——
    人数多的时候（几十人，每人两次 Keycloak 写调用）同步表单提交会让前端
    long-blocking 卡住没有任何反馈，改成建一个成功/失败就推一条事件，前端
    （batch_preview.html 的内联 JS）边收边渲染表格行。刻意仍然不并发（跟
    改造前一样，每次两次 Keycloak 写调用 + 失败自动回滚），批量场景量本来
    就小，顺序调用更容易保证每一行的结果、进度事件、审计日志三者对得上号。

    登录检查放在生成器外面——生成器一旦开始 yield，HTTP 状态码和 header
    已经发出去了，这时候才发现没登录也没法再改成 303 跳转。
    """
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    rows = list(zip(emails, first_names, last_names, usernames, roles, names))
    total = len(rows)

    async def event_stream():
        for index, (email, first_name, last_name, username, role, name) in enumerate(rows, start=1):
            detail = f"{role}（批量导入）"
            base = {"index": index, "total": total, "email": email, "username": username, "role": role}
            try:
                result = await run_in_threadpool(
                    kc.create_user,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    role=role,
                    username=username,
                    chinese_name=name,
                )
            except RoleNotAllowedError as exc:
                audit_service.record(
                    db, user["email"], AuditAction.CREATE_USER, email,
                    target_name=name, detail=detail, success=False, error_message=str(exc),
                )
                yield _sse({**base, "status": "failed", "message": str(exc)})
                continue
            except PartialFailureError as exc:
                audit_service.record(
                    db, user["email"], AuditAction.CREATE_USER, email,
                    target_name=name, detail=detail, success=False, error_message=str(exc),
                )
                yield _sse({**base, "status": "partial_failure", "message": str(exc)})
                continue
            except (KeycloakAPIError, UserNotFoundError) as exc:
                audit_service.record(
                    db, user["email"], AuditAction.CREATE_USER, email,
                    target_name=name, detail=detail, success=False, error_message=str(exc),
                )
                yield _sse({
                    **base, "status": "failed",
                    "message": "建号失败，请检查 Keycloak 连接或联系管理员",
                })
                continue

            if not result.created:
                audit_service.record(
                    db, user["email"], AuditAction.CREATE_USER, email,
                    target_name=name, detail=detail, success=False,
                    error_message="username 已存在，被 Keycloak SKIP",
                )
                yield _sse({**base, "status": "skipped", "message": "用户名已存在，已跳过"})
                continue

            audit_service.record(
                db, user["email"], AuditAction.CREATE_USER, email,
                target_name=name, detail=detail, success=True,
            )
            yield _sse({**base, "status": "created", "password": result.password})

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
async def reset_password(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    # username/email 不再信任表单隐藏字段——那两个字段可以被篡改/来自过期页面，
    # 跟 user_id 对不上就会把审计日志记到错的人身上。改成拿 user_id 去 Keycloak
    # 反查真实的 username/email（code review 发现的问题）。
    try:
        target = await run_in_threadpool(kc.get_user_by_id, user_id)
    except KeycloakAPIError as exc:
        audit_service.record(
            db,
            user["email"],
            AuditAction.RESET_PASSWORD,
            f"user_id={user_id}",
            success=False,
            error_message=f"查不到这个用户：{exc}",
        )
        return RedirectResponse(url="/?flash=重置密码失败：找不到这个用户，可能已被删除", status_code=303)

    try:
        password = await run_in_threadpool(kc.reset_password, user_id, target.username, target.email)
    except KeycloakAPIError as exc:
        audit_service.record(
            db, user["email"], AuditAction.RESET_PASSWORD, target.email, success=False, error_message=str(exc)
        )
        return RedirectResponse(url="/?flash=重置密码失败，请检查 Keycloak 连接或联系管理员", status_code=303)

    audit_service.record(db, user["email"], AuditAction.RESET_PASSWORD, target.email, success=True)

    return templates.TemplateResponse(
        "users/secret_result.html",
        {
            "request": request,
            "current_user": user,
            "title": "密码已重置",
            "target_email": target.email,
            "password": password,
        },
    )


@router.post("/users/{user_id}/change-tier")
async def change_tier(
    request: Request,
    user_id: str,
    new_role: str = Form(...),
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    try:
        target = await run_in_threadpool(kc.get_user_by_id, user_id)
    except KeycloakAPIError as exc:
        audit_service.record(
            db,
            user["email"],
            AuditAction.CHANGE_TIER,
            f"user_id={user_id}",
            detail=new_role,
            success=False,
            error_message=f"查不到这个用户：{exc}",
        )
        return RedirectResponse(url="/?flash=档位变更失败：找不到这个用户，可能已被删除", status_code=303)

    if target.email == user["email"]:
        # 防呆，不是防坏人：不让管理员通过本应用改自己的档位，避免误操作把
        # 自己降出 quick-admin-pro 之后连本应用都登不进去了（这个组同时是
        # 登录本应用的门槛，见 keycloak_service.ADMIN_PRO_GROUP_PATH）。
        exc = SelfLockoutError(
            f"不能通过本应用修改自己（{target.email}）的档位，避免误操作把自己锁死——"
            "找另一位管理员帮忙操作，或去 Keycloak 控制台。"
        )
        audit_service.record(
            db, user["email"], AuditAction.CHANGE_TIER, target.email, detail=new_role,
            success=False, error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash={exc}", status_code=303)

    try:
        await run_in_threadpool(kc.change_tier, user_id, new_role)
    except (RoleNotAllowedError, LastAdminGuardError) as exc:
        audit_service.record(
            db,
            user["email"],
            AuditAction.CHANGE_TIER,
            target.email,
            detail=new_role,
            success=False,
            error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash=档位变更失败：{exc}", status_code=303)
    except PartialFailureError as exc:
        # 用户现在可能同时在新旧两个组里——必须让管理员当场看到，不能只藏进审计日志。
        audit_service.record(
            db,
            user["email"],
            AuditAction.CHANGE_TIER,
            target.email,
            detail=new_role,
            success=False,
            error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash=档位变更出现异常状态，需要人工处理：{exc}", status_code=303)
    except (KeycloakAPIError, UserNotFoundError) as exc:
        audit_service.record(
            db,
            user["email"],
            AuditAction.CHANGE_TIER,
            target.email,
            detail=new_role,
            success=False,
            error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash=档位变更失败：{exc}", status_code=303)

    audit_service.record(db, user["email"], AuditAction.CHANGE_TIER, target.email, detail=new_role, success=True)
    return RedirectResponse(
        url=f"/?flash=已将 {target.email} 切换到「{new_role}」，实际生效要等对方下次登录", status_code=303
    )


@router.post("/users/{user_id}/set-enabled")
async def set_enabled(
    request: Request,
    user_id: str,
    enabled: bool = Form(...),
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    action = AuditAction.ENABLE_USER if enabled else AuditAction.DISABLE_USER

    try:
        target = await run_in_threadpool(kc.get_user_by_id, user_id)
    except KeycloakAPIError as exc:
        audit_service.record(
            db, user["email"], action, f"user_id={user_id}", success=False, error_message=f"查不到这个用户：{exc}"
        )
        return RedirectResponse(url="/?flash=操作失败：找不到这个用户，可能已被删除", status_code=303)

    if not enabled and target.email == user["email"]:
        # 同 change_tier：防呆，不让管理员通过本应用停用自己的账号。
        exc = SelfLockoutError(
            f"不能通过本应用停用自己（{target.email}）的账号——找另一位管理员帮忙操作，或去 Keycloak 控制台。"
        )
        audit_service.record(db, user["email"], action, target.email, success=False, error_message=str(exc))
        return RedirectResponse(url=f"/?flash={exc}", status_code=303)

    try:
        await run_in_threadpool(kc.set_enabled, user_id, enabled)
    except LastAdminGuardError as exc:
        audit_service.record(db, user["email"], action, target.email, success=False, error_message=str(exc))
        return RedirectResponse(url=f"/?flash=操作失败：{exc}", status_code=303)
    except KeycloakAPIError as exc:
        audit_service.record(db, user["email"], action, target.email, success=False, error_message=str(exc))
        return RedirectResponse(url="/?flash=操作失败，请检查 Keycloak 连接或联系管理员", status_code=303)

    audit_service.record(db, user["email"], action, target.email, success=True)
    verb = "启用" if enabled else "停用"
    return RedirectResponse(url=f"/?flash=已{verb} {target.email}", status_code=303)


# ---------- 批量改档位 / 批量停用启用（对已建号用户批量操作，不同于批量建号）----------
#
# 跟批量建号一样刻意不并发：都是低频、人工触发一次点几十下按钮的操作，量小
# （几十人），顺序调用更容易保证每一行结果和审计日志对得上号。跟单用户版本
# （change_tier / set_enabled 路由）分开实现而不是抽公共 helper——单用户路由
# 已经有测试覆盖过的错误处理分支，批量版本每行的失败不能中断其它行，返回
# 结构（每行一个 status/message）也和单用户版本的"直接 redirect 带 flash"
# 不一样，硬抽共用函数反而要在两种调用形态之间来回适配，得不偿失。


@router.post("/users/batch-change-tier", response_class=HTMLResponse)
async def batch_change_tier(
    request: Request,
    user_ids: List[str] = Form(...),
    new_role: str = Form(...),
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    detail = f"{new_role}（批量操作）"
    results = []
    for user_id in user_ids:
        try:
            target = await run_in_threadpool(kc.get_user_by_id, user_id)
        except KeycloakAPIError as exc:
            audit_service.record(
                db, user["email"], AuditAction.CHANGE_TIER, f"user_id={user_id}",
                detail=detail, success=False, error_message=f"查不到这个用户：{exc}",
            )
            results.append({"email": f"user_id={user_id}", "status": "failed", "message": f"查不到这个用户：{exc}"})
            continue

        if target.email == user["email"]:
            exc = SelfLockoutError(
                f"不能通过本应用修改自己（{target.email}）的档位——批量操作里已跳过这一行，"
                "找另一位管理员帮忙操作，或去 Keycloak 控制台。"
            )
            audit_service.record(
                db, user["email"], AuditAction.CHANGE_TIER, target.email,
                detail=detail, success=False, error_message=str(exc),
            )
            results.append({"email": target.email, "status": "skipped", "message": str(exc)})
            continue

        try:
            await run_in_threadpool(kc.change_tier, user_id, new_role)
        except (RoleNotAllowedError, LastAdminGuardError) as exc:
            audit_service.record(
                db, user["email"], AuditAction.CHANGE_TIER, target.email,
                detail=detail, success=False, error_message=str(exc),
            )
            results.append({"email": target.email, "status": "failed", "message": str(exc)})
            continue
        except PartialFailureError as exc:
            audit_service.record(
                db, user["email"], AuditAction.CHANGE_TIER, target.email,
                detail=detail, success=False, error_message=str(exc),
            )
            results.append({"email": target.email, "status": "partial_failure", "message": str(exc)})
            continue
        except (KeycloakAPIError, UserNotFoundError) as exc:
            audit_service.record(
                db, user["email"], AuditAction.CHANGE_TIER, target.email,
                detail=detail, success=False, error_message=str(exc),
            )
            results.append({"email": target.email, "status": "failed", "message": str(exc)})
            continue

        audit_service.record(db, user["email"], AuditAction.CHANGE_TIER, target.email, detail=detail, success=True)
        results.append({"email": target.email, "status": "success", "message": f"已切换到「{new_role}」，实际生效要等对方下次登录"})

    return templates.TemplateResponse(
        "users/batch_action_result.html",
        {"request": request, "current_user": user, "results": results, "title": f"批量改档位结果（{new_role}）"},
    )


@router.post("/users/batch-set-enabled", response_class=HTMLResponse)
async def batch_set_enabled(
    request: Request,
    user_ids: List[str] = Form(...),
    enabled: bool = Form(...),
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    action = AuditAction.ENABLE_USER if enabled else AuditAction.DISABLE_USER
    verb = "启用" if enabled else "停用"
    results = []
    for user_id in user_ids:
        try:
            target = await run_in_threadpool(kc.get_user_by_id, user_id)
        except KeycloakAPIError as exc:
            audit_service.record(
                db, user["email"], action, f"user_id={user_id}", success=False, error_message=f"查不到这个用户：{exc}"
            )
            results.append({"email": f"user_id={user_id}", "status": "failed", "message": f"查不到这个用户：{exc}"})
            continue

        if not enabled and target.email == user["email"]:
            exc = SelfLockoutError(
                f"不能通过本应用停用自己（{target.email}）——批量操作里已跳过这一行。"
            )
            audit_service.record(db, user["email"], action, target.email, success=False, error_message=str(exc))
            results.append({"email": target.email, "status": "skipped", "message": str(exc)})
            continue

        try:
            await run_in_threadpool(kc.set_enabled, user_id, enabled)
        except LastAdminGuardError as exc:
            audit_service.record(db, user["email"], action, target.email, success=False, error_message=str(exc))
            results.append({"email": target.email, "status": "failed", "message": str(exc)})
            continue
        except KeycloakAPIError as exc:
            audit_service.record(db, user["email"], action, target.email, success=False, error_message=str(exc))
            results.append({"email": target.email, "status": "failed", "message": str(exc)})
            continue

        audit_service.record(db, user["email"], action, target.email, success=True)
        results.append({"email": target.email, "status": "success", "message": f"已{verb}"})

    return templates.TemplateResponse(
        "users/batch_action_result.html",
        {"request": request, "current_user": user, "results": results, "title": f"批量{verb}结果"},
    )


# ---------- 硬删除（design.md 4.5 节原来评估后决定留作后续迭代，现已启用）----------


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(
    request: Request,
    user_id: str,
    confirm_email: str = Form(...),
    db: Session = Depends(get_db),
    kc: KeycloakService = Depends(get_keycloak_service),
):
    """不可逆操作，比停用（enabled: false）风险高一个量级——浏览器的 confirm()
    弹窗很容易被无意识点掉，所以除了弹窗确认之外，还要求管理员在表单里手打
    一遍目标邮箱（见 users/list.html），后端在这里逐字核对，打错/不填一律
    拒绝执行，不静默纠正、不模糊匹配。
    """
    user = _require_login(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    try:
        target = await run_in_threadpool(kc.get_user_by_id, user_id)
    except KeycloakAPIError as exc:
        audit_service.record(
            db, user["email"], AuditAction.DELETE_USER, f"user_id={user_id}",
            success=False, error_message=f"查不到这个用户：{exc}",
        )
        return RedirectResponse(url="/?flash=删除失败：找不到这个用户，可能已被删除", status_code=303)

    # 被删用户当时的角色档位——审计日志之前没记这个，之前发现的一个真实缺口：
    # 万一真的删错了人，只知道"谁在什么时候删了谁"，不知道该重建成什么档位，
    # 事后恢复基本无从下手。现在每条 DELETE_USER 记录都带上这个信息。
    role_detail = target.role or "（未分配档位）"

    if target.email == user["email"]:
        exc = SelfLockoutError(
            f"不能通过本应用删除自己（{target.email}）的账号——找另一位管理员帮忙操作，或去 Keycloak 控制台。"
        )
        audit_service.record(
            db, user["email"], AuditAction.DELETE_USER, target.email,
            detail=role_detail, success=False, error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash={exc}", status_code=303)

    if confirm_email.strip().lower() != target.email.lower():
        error_message = f"确认邮箱输入不匹配（输入的是 {confirm_email!r}），已拒绝执行，未删除任何数据"
        audit_service.record(
            db, user["email"], AuditAction.DELETE_USER, target.email,
            detail=role_detail, success=False, error_message=error_message,
        )
        return RedirectResponse(url=f"/?flash=删除失败：确认邮箱没有输对，操作已取消，{target.email} 未被删除", status_code=303)

    try:
        await run_in_threadpool(kc.delete_user, user_id)
    except MustDisableFirstError as exc:
        audit_service.record(
            db, user["email"], AuditAction.DELETE_USER, target.email,
            detail=role_detail, success=False, error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash=删除失败：{exc}", status_code=303)
    except LastAdminGuardError as exc:
        audit_service.record(
            db, user["email"], AuditAction.DELETE_USER, target.email,
            detail=role_detail, success=False, error_message=str(exc),
        )
        return RedirectResponse(url=f"/?flash=删除失败：{exc}", status_code=303)
    except KeycloakAPIError as exc:
        audit_service.record(
            db, user["email"], AuditAction.DELETE_USER, target.email,
            detail=role_detail, success=False, error_message=str(exc),
        )
        return RedirectResponse(url="/?flash=删除失败，请检查 Keycloak 连接或联系管理员", status_code=303)

    audit_service.record(db, user["email"], AuditAction.DELETE_USER, target.email, detail=role_detail, success=True)
    return RedirectResponse(url=f"/?flash=已彻底删除 {target.email}，此操作不可撤销", status_code=303)
