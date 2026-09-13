"""操作审计写入。

两条通道（后端设计文档 §4.3）：service 层同事务审计（主通道，审计与业务
变更同一 commit 提交、永不脱节）与本模块的 :func:`write_audit`。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from src.platform.models import AuditLog


def write_audit(
    session: Session,
    *,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    user_id: Optional[int] = None,
    username: str = "",
    detail: Optional[dict[str, Any]] = None,
    ip: str = "",
) -> AuditLog:
    """向审计表追加一条记录（调用方负责 commit 以保证与业务同事务）。

    Args:
        session: 平台数据库会话。
        action: 动作码（如 user.login / asset.create / event.transition）。
        resource_type: 资源类型。
        resource_id: 资源标识（字符串）。
        user_id: 操作者 ID（系统/agent/匿名失败为 None）。
        username: 操作者名快照。
        detail: 结构化明细。
        ip: 来源 IP。

    Returns:
        已加入会话的审计行。
    """
    entry = AuditLog(
        user_id=user_id,
        username=username,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        detail=detail,
        ip=ip,
    )
    session.add(entry)
    return entry
