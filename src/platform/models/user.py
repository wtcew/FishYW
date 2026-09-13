"""用户与角色模型。

角色采用简化方案（XingCloud 的权限表在本期收敛为角色码矩阵）：``roles.code``
（admin/operator/viewer）即能力判定依据，``roles`` 表仅存显示名与描述；
权限矩阵见 ``src/platform/deps.py`` 的 ``ROLE_MATRIX``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.platform.models import Base


class Role(Base):
    """角色：能力判定的稳定标识。

    Attributes:
        code: 角色码（admin/operator/viewer），唯一且不可见地变更。
        name: 显示名。
        description: 用途说明。
        users: 该角色下的用户集合。
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")
    users: Mapped[list["User"]] = relationship(back_populates="role")


class User(Base):
    """平台用户。

    Attributes:
        username: 登录名，唯一。
        password_hash: bcrypt 哈希（禁止存明文）。
        display_name: 显示名。
        role_id: 角色 FK。
        is_active: 停用代替删除（保审计引用完整）。
        last_login_at: 最近登录时间。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    role: Mapped[Role] = relationship(back_populates="users")
