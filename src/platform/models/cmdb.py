"""资产登记（CMDB）模型：业务线 → 环境 → 资产 三级结构。

对应 XingCloud「资源中心」语义：统一资源、稳定标识、责任归属
（运维负责人 owner / 业务负责人在 BusinessLine 上）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.platform.models import Base


class BusinessLine(Base):
    """一级业务线（资产归属的顶层维度）。

    Attributes:
        code: 业务编码，唯一。
        name: 业务名称，唯一。
        owner_id: 业务负责人（用户）。
        environments: 下属环境集合。
    """

    __tablename__ = "business_lines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[Optional["User"]] = relationship()  # noqa: F821 - 字符串前向引用
    environments: Mapped[list["Environment"]] = relationship(back_populates="business_line")


class Environment(Base):
    """业务线下的运行环境（生产/预发/测试等）。

    Attributes:
        business_line_id: 所属业务线 FK。
        name: 环境名，同一业务线内唯一。
        assets: 环境内资产集合。
    """

    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("business_line_id", "name", name="uq_env_bl_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    business_line_id: Mapped[int] = mapped_column(
        ForeignKey("business_lines.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    business_line: Mapped[BusinessLine] = relationship(back_populates="environments")
    assets: Mapped[list["Asset"]] = relationship(back_populates="environment")


class Asset(Base):
    """资产：被运维的最小实体（主机/数据库/中间件/应用/网络设备）。

    Attributes:
        environment_id: 所属环境 FK。
        name: 资产名（展示用，如 api-gateway-01）。
        asset_type: 类型（host/db/middleware/app/network）。
        identifier: 稳定标识（IP/主机名/实例号），与环境+类型联合唯一。
        owner_id: 运维负责人。
        owner_contact: 联系方式（飞书/手机号等自由文本）。
        status: active / maintenance / decommissioned。
        tags: 标签数组（JSON）。
        remark: 备注。
    """

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("environment_id", "asset_type", "identifier", name="uq_asset_ident"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    environment_id: Mapped[int] = mapped_column(
        ForeignKey("environments.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    owner_contact: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    environment: Mapped[Environment] = relationship(back_populates="assets")
    owner: Mapped[Optional["User"]] = relationship()  # noqa: F821
