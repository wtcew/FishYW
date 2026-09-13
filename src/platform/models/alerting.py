"""告警与事件模型：规则实例 → 告警事件 → 复盘时间线。

状态机（services/events.py 的 TRANSITIONS 为唯一权威）：
``open → acknowledged|closed``；``acknowledged → diagnosing|resolved|closed``；
``diagnosing → acknowledged|resolved|closed``；``resolved → closed``；``closed`` 终态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.platform.models import Base


class AlertRule(Base):
    """告警规则实例：把外部/手工告警映射为事件。

    Attributes:
        name: 规则名。
        enabled: 停用规则不参与匹配。
        match_field: 匹配字段（title / source / payload_key:<key> / asset.identifier）。
        match_op: 匹配算子（contains / eq / regex）。
        match_value: 匹配值。
        severity: 命中后事件默认严重度（critical/major/minor/info）。
        auto_diagnose: 命中后自动触发 AI 研判。
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    match_field: Mapped[str] = mapped_column(String(32), nullable=False)
    match_op: Mapped[str] = mapped_column(String(16), nullable=False)
    match_value: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    auto_diagnose: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Event(Base):
    """运维事件：异常的载体，挂资产、走状态机、聚时间线与研判。

    Attributes:
        event_no: 人可读编号（EV-YYYYMMDD-NNNN），唯一。
        title: 事件标题。
        source: 来源（webhook:<source> / manual / rule:<rule_id>）。
        severity: critical/major/minor/info。
        status: 状态机当前态（默认 open）。
        asset_id: 影响资产（手工事件可空）。
        environment_id / business_line_id: 由 asset 反查后冗余，供跨资产过滤。
        payload: 外部告警原文（JSON）。
        acknowledged_by_id / acknowledged_at: 认领人与时间。
        resolved_at / closed_at: 恢复与关闭时间。
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)
    asset_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assets.id"), nullable=True, index=True)
    environment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("environments.id"), nullable=True, index=True
    )
    business_line_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("business_lines.id"), nullable=True, index=True
    )
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    acknowledged_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    asset: Mapped[Optional["Asset"]] = relationship()  # noqa: F821
    timeline: Mapped[list["EventTimelineEntry"]] = relationship(
        back_populates="event", order_by="EventTimelineEntry.created_at"
    )
    diagnoses: Mapped[list["DiagnosisRecord"]] = relationship(back_populates="event")


class EventTimelineEntry(Base):
    """复盘时间线条目（只追加，不修改——审计互证的前提）。

    Attributes:
        event_id: 所属事件。
        entry_type: created/status_change/comment/webhook/rule_matched/
            diagnosis_started/diagnosis_finished。
        actor_type: user / system / agent。
        actor_id / actor_name: 操作者（冗余名保证可读）。
        detail: 结构化明细（如 {"from": "open", "to": "acknowledged"}）。
    """

    __tablename__ = "event_timeline_entries"
    __table_args__ = (Index("ix_timeline_event_created", "event_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    actor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event: Mapped[Event] = relationship(back_populates="timeline")
