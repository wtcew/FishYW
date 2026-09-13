"""操作审计模型。

所有平台写操作与登录成败都应留痕；``user_id`` 可空（系统/agent 动作、
匿名登录失败），``username`` 冗余快照保证用户停用后审计仍可读。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.platform.models import Base


class AuditLog(Base):
    """操作审计日志（只追加）。

    Attributes:
        user_id: 操作者 FK；系统/agent/匿名失败为 NULL。
        username: 操作者名快照。
        action: 动作码（user.login / asset.create / event.transition / diagnosis.trigger 等）。
        resource_type: 资源类型（user/asset/event/...）。
        resource_id: 资源标识（字符串以兼容不同主键形态）。
        detail: 结构化明细（before/after、匹配规则等）。
        ip: 来源 IP。
        created_at: 时间（审计按倒序查询，建索引）。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    resource_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
