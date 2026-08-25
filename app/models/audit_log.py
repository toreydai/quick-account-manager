import datetime
import enum

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditAction(str, enum.Enum):
    """design.md 2 节：审计日志覆盖的操作类型。"""

    CREATE_USER = "create_user"
    RESET_PASSWORD = "reset_password"
    CHANGE_TIER = "change_tier"
    DISABLE_USER = "disable_user"
    ENABLE_USER = "enable_user"
    DELETE_USER = "delete_user"
    EXPORT_USERS = "export_users"


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction), nullable=False)
    target_email: Mapped[str] = mapped_column(String(255), nullable=False)
    # 建号表单里的"中文姓名"字段——之前收了从来没用过，静默丢弃（code review
    # 发现的问题）。只在 CREATE_USER 记录时填，其他操作类型留空。
    target_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    detail: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    success: Mapped[bool] = mapped_column(nullable=False, default=True)
    error_message: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
