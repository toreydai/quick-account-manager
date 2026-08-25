import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction, AuditLog

logger = logging.getLogger(__name__)


def record(
    db: Session,
    actor_email: str,
    action: AuditAction,
    target_email: str,
    target_name: str = "",
    detail: str = "",
    success: bool = True,
    error_message: str = "",
) -> Optional[AuditLog]:
    """写审计日志失败（DB 锁住/磁盘满/表结构对不上之类）不能往上抛——调用方
    在写这条审计记录之前，Keycloak 那边的操作（建号/改档位/停用/删除）大多
    已经真实生效了，如果这里抛异常捅穿路由，单用户场景下只是这次操作报错
    文案不准确，但批量路由（/users/batch-change-tier 等）是在一个 for 循环
    里逐个调用，未捕获的异常会直接打断整个循环——已经处理的行的 Keycloak
    状态已经生效但审计记录没写上，后面排队的行则完全没被处理，用户看到的
    是一个裸 500 页面，不知道哪些做了哪些没做。这是本地对着真实 Keycloak
    做批量场景端到端验证时意外触发（数据库表还没建）才发现的真实问题——
    调用方（41 处）都不使用这个函数的返回值，所以这里把返回类型从 AuditLog
    改成 Optional[AuditLog]（写失败时返回 None）不需要动任何调用点。
    """
    entry = AuditLog(
        actor_email=actor_email,
        action=action,
        target_email=target_email,
        target_name=target_name,
        detail=detail,
        success=success,
        error_message=error_message,
    )
    try:
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry
    except Exception:
        db.rollback()
        logger.error(
            "审计日志写入失败（不影响已经生效的 Keycloak 操作）：actor=%s action=%s target=%s",
            actor_email, action, target_email, exc_info=True,
        )
        return None


def list_recent(
    db: Session,
    limit: int = 200,
    actor_email: Optional[str] = None,
    target_email: Optional[str] = None,
    action: Optional[AuditAction] = None,
    success: Optional[bool] = None,
) -> List[AuditLog]:
    """不筛选时行为跟原来完全一样（最近 limit 条）。筛选字段全部是可选的，
    邮箱两个字段用 LIKE 模糊匹配（管理员不一定记得完整邮箱），action/success
    是精确匹配的下拉选项——跟 kiro-fleet「操作日志支持按账号/操作类型/状态
    过滤」对齐，量小的时候翻页找一条记录很难用，这几个字段筛选成本很低
    （本地 SQLite，不涉及外部调用）。
    """
    query = db.query(AuditLog)
    if actor_email:
        query = query.filter(AuditLog.actor_email.ilike(f"%{actor_email}%"))
    if target_email:
        query = query.filter(AuditLog.target_email.ilike(f"%{target_email}%"))
    if action is not None:
        query = query.filter(AuditLog.action == action)
    if success is not None:
        query = query.filter(AuditLog.success == success)
    # created_at 精度在极快的连续写入下可能撞车，加 id 做次要排序键，
    # 保证列表顺序稳定（不然测试和真实使用都可能在同一毫秒内写入多条记录）。
    return query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).all()
