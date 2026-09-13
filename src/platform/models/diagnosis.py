"""AI 研判记录模型。

研判复用 AIOps 引擎（LangGraph 状态机 + 混合检索），本表只落**结论与证据**。
"无证据不出根因"：``evidence`` 数组的 ``seq`` 即答案文本中 ``[E n]`` 引用编号；
``confidence`` 受证据充分性钳制（见 services/diagnosis.py）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.platform.models import Base


class DiagnosisRecord(Base):
    """一次事件研判的完整记录。

    Attributes:
        event_id: 所属事件。
        status: running / completed / failed / timeout。
        trigger_type: manual（人工按钮） / rule（规则自动）。
        triggered_by_id: 触发人（规则自动为 NULL）。
        query: 实际送入 agent 的问题文本（含资产上下文，确定性部分全来自 DB）。
        root_cause / suggestion: 从模型答案抽取的根因与建议。
        confidence: 置信度 [0,1]；有 knowledge 证据时 clamp ≤0.9，仅资产证据时 ≤0.5；
            模型未给则为 NULL（前端显示"未知"，不臆造）。
        evidence: 证据数组 [{seq, kind: asset|knowledge, ref, title, snippet}]。
        evidence_sufficient: 复用 agent reflection.sufficient。
        answer_summary: 模型原始答案（含 [E n] 标注）。
        degraded / degraded_reason: 降级标志与原因
            （timeout / llm_unavailable / insufficient_evidence / unparsed）。
        trace_id: 关联现有内存链路（/api/v1/trace/{trace_id}）。
    """

    __tablename__ = "diagnosis_records"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_diagnosis_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", index=True)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    triggered_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggestion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_sufficient: Mapped[Optional[bool]] = mapped_column(nullable=True)
    answer_summary: Mapped[str] = mapped_column(Text, default="")
    agent_iterations: Mapped[Optional[int]] = mapped_column(nullable=True)
    degraded: Mapped[bool] = mapped_column(default=False, nullable=False)
    degraded_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[float]] = mapped_column(nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    event: Mapped["Event"] = relationship(back_populates="diagnoses")  # noqa: F821
