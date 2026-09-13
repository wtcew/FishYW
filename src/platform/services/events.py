"""事件服务：状态机、时间线、规则匹配与研判编排。

写入口唯一原则：事件状态流转、时间线追加、审计写入全部收敛在本包，
路由层不得直改状态。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.platform.audit import write_audit
from src.platform.models import AlertRule, Asset, Event, EventTimelineEntry
from src.settings import get_settings

logger = logging.getLogger(__name__)

#: 状态机唯一权威：非法流转一律 409。
TRANSITIONS: dict[str, set[str]] = {
    "open": {"acknowledged", "closed"},
    "acknowledged": {"diagnosing", "resolved", "closed"},
    "diagnosing": {"acknowledged", "resolved", "closed"},
    "resolved": {"closed"},
    "closed": set(),
}

#: 事件严重度合法集合。
SEVERITIES = {"critical", "major", "minor", "info"}

# 研判观察者注册表：diagnosis_id -> 订阅者 asyncio.Queue 列表（SSE 观察窗口用）。
_observers: dict[int, list[asyncio.Queue]] = {}
_observers_lock = asyncio.Lock()


class TransitionError(ValueError):
    """非法状态流转。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def append_timeline(
    session: Session,
    event: Event,
    *,
    entry_type: str,
    content: str = "",
    actor_type: str = "user",
    actor_id: int | None = None,
    actor_name: str = "",
    detail: dict[str, Any] | None = None,
) -> EventTimelineEntry:
    """向事件追加一条时间线（调用方负责 commit）。

    Args:
        session: 平台会话。
        event: 目标事件。
        entry_type: 条目类型。
        content: 文本内容。
        actor_type: user/system/agent。
        actor_id: 操作者 ID。
        actor_name: 操作者名快照。
        detail: 结构化明细。

    Returns:
        已加入会话的时间线行。
    """
    entry = EventTimelineEntry(
        event_id=event.id,
        entry_type=entry_type,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_name=actor_name,
        content=content,
        detail=detail,
    )
    session.add(entry)
    return entry


def transition_event(
    session: Session,
    event: Event,
    *,
    target: str,
    user_id: int | None,
    username: str,
    note: str = "",
    ip: str = "",
    actor_type: str = "user",
) -> Event:
    """把事件流转到目标状态（含时间线与审计，同一事务）。

    Args:
        session: 平台会话。
        event: 目标事件。
        target: 目标状态。
        user_id: 操作者 ID。
        username: 操作者名。
        note: 处置说明（resolve/close 时应提供）。
        ip: 来源 IP。
        actor_type: 时间线操作者类型（user/system；系统自动流转时传 system）。

    Returns:
        更新后的事件。

    Raises:
        TransitionError: 非法流转。
    """
    allowed = TRANSITIONS.get(event.status, set())
    if target not in allowed:
        raise TransitionError(f"非法状态流转: {event.status} -> {target}")

    previous = event.status
    now = _now()
    event.status = target
    event.updated_at = now
    if target == "acknowledged":
        event.acknowledged_by_id = user_id
        event.acknowledged_at = now
    elif target == "resolved":
        event.resolved_at = now
    elif target == "closed":
        event.closed_at = now

    append_timeline(
        session,
        event,
        entry_type="status_change",
        content=note or f"{previous} -> {target}",
        actor_type=actor_type,
        actor_id=user_id,
        actor_name=username,
        detail={"from": previous, "to": target},
    )
    write_audit(
        session,
        action="event.transition",
        resource_type="event",
        resource_id=str(event.id),
        user_id=user_id,
        username=username,
        detail={"from": previous, "to": target, "note": note},
        ip=ip,
    )
    return event


def generate_event_no(session: Session) -> str:
    """生成当日人可读事件编号（事务内查当日最大序号递增）。

    Args:
        session: 平台会话。

    Returns:
        形如 ``EV-YYYYMMDD-0001`` 的编号。
    """
    today = _now().strftime("%Y%m%d")
    prefix = f"EV-{today}-"
    rows = (
        session.query(Event.event_no)
        .filter(Event.event_no.like(prefix + "%"))
        .all()
    )
    max_seq = 0
    for (no,) in rows:
        try:
            max_seq = max(max_seq, int(no.rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return f"{prefix}{max_seq + 1:04d}"


def evaluate_rules(session: Session, *, title: str, source: str, payload: dict[str, Any] | None,
                   asset_identifier: str | None) -> tuple[list[AlertRule], str]:
    """用启用规则匹配告警，返回（命中规则, 建议严重度）。

    Args:
        session: 平台会话。
        title: 告警标题。
        source: 告警来源。
        payload: 原始载荷。
        asset_identifier: 资产标识（可选）。

    Returns:
        (命中规则列表, 严重度)。未命中时严重度取告警自带或 major。
    """
    rules = session.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
    matched: list[AlertRule] = []
    severity = None
    for rule in rules:
        field_value = {
            "title": title,
            "source": source,
            "asset.identifier": asset_identifier or "",
        }.get(rule.match_field)
        if field_value is None and rule.match_field.startswith("payload_key:"):
            key = rule.match_field.split(":", 1)[1]
            field_value = str((payload or {}).get(key, ""))
        if field_value is None:
            continue
        hit = False
        try:
            if rule.match_op == "contains":
                hit = rule.match_value.lower() in field_value.lower()
            elif rule.match_op == "eq":
                hit = field_value == rule.match_value
            elif rule.match_op == "regex":
                import re

                hit = re.search(rule.match_value, field_value) is not None
        except Exception as exc:  # noqa: BLE001 - 规则配置错误不中断摄取
            logger.warning("规则 %s 匹配异常: %s", rule.id, exc)
        if hit:
            matched.append(rule)
            if severity is None or rule.severity == "critical":
                severity = rule.severity
    return matched, severity or "major"


# ── 研判编排 ──────────────────────────────────────────────────────


def build_query(event: Event, asset: Asset | None, payload: dict[str, Any] | None) -> str:
    """装配确定性研判问题：事实全部来自 DB，不由模型生成。

    Args:
        event: 事件。
        asset: 影响资产（可空）。
        payload: 告警载荷。

    Returns:
        送入 agent 的问题文本。
    """
    lines = [f"运维事件 {event.event_no}：{event.title}（严重度 {event.severity}）"]
    if asset is not None:
        lines.append(
            f"影响资产：{asset.name}（类型 {asset.asset_type}，标识 {asset.identifier}，"
            f"状态 {asset.status}）"
        )
    if payload:
        try:
            lines.append("告警载荷：" + json.dumps(payload, ensure_ascii=False)[:600])
        except (TypeError, ValueError):
            pass
    # 输出格式必须与 _extract_sections / _extract_confidence 的解析约定一致：
    # 三个中文标记一旦缺失，根因与置信度会解析为 None（研判核心产出丢失）。
    lines.append(
        "请依据参考文档研判根因并给出可执行的建议动作，引用证据请标注 [E n]。\n"
        "严格按以下三行格式输出，标记不可省略、不可改写：\n"
        "根因：<一句话根因；证据不足时写「证据不足，无法确定根因」>\n"
        "建议：<可执行的处置步骤>\n"
        "置信度：<0 到 1 之间的小数>"
    )
    return "\n".join(lines)


def _clamp_confidence(
    confidence: float | None, evidence: list[dict[str, Any]]
) -> tuple[float | None, str | None]:
    """按证据充分性钳制置信度（"无证据不出根因"约束的实现）。

    Args:
        confidence: 模型给的置信度。
        evidence: 证据数组。

    Returns:
        (钳制后置信度, 降级原因或 None)。
    """
    has_knowledge = any(item.get("kind") == "knowledge" for item in evidence)
    if confidence is None:
        if not has_knowledge:
            return None, "insufficient_evidence"
        return None, None
    if not has_knowledge:
        return min(confidence, 0.5), "insufficient_evidence"
    return min(confidence, 0.9), None


def _extract_sections(answer: str) -> tuple[str | None, str | None]:
    """从模型答案抽取根因/建议三段式；失败返回 (None, None) 由原文兜底。

    兼容两种常见写法：``根因：内容``（同行）与 ``根因`` 换行后跟内容；
    内容取到下一个标记之前（``置信度`` 仅作为切段边界、不产出字段），最长 1000 字符。

    Args:
        answer: 模型答案文本。

    Returns:
        (根因, 建议)。
    """
    targets = {"根因": "root", "建议": "suggestion"}
    boundaries = ("根因", "建议", "置信度")
    positions = sorted(
        (answer.find(marker), targets.get(marker), marker)
        for marker in boundaries
        if answer.find(marker) >= 0
    )
    found: dict[str, str] = {}
    for order, (index, target, marker) in enumerate(positions):
        if target is None:  # 置信度：只用于切段
            continue
        start = index + len(marker)
        end = positions[order + 1][0] if order + 1 < len(positions) else len(answer)
        segment = answer[start:end].lstrip("：:=\t ").strip()
        if segment:
            found[target] = segment[:1000]
    return found.get("root"), found.get("suggestion")


async def run_diagnosis(session_factory, graph_holder, record, event: Event) -> None:
    """执行研判（在后台任务中运行）：证据装配→调 agent→约束落库→状态联动。

    Args:
        session_factory: 会话工厂（后台任务用新会话）。
        graph_holder: 提供 ``agent_graph`` 属性的容器（AppState）。
        record: 已创建的 running 态研判记录（本函数更新它）。
        event: 目标事件（本函数同步其状态与时间线）。
    """
    started = time.perf_counter()
    settings = get_settings()
    status = "completed"
    degraded_reason: str | None = None
    error: str | None = None
    result_state: dict[str, Any] = {}
    query = ""  # 落库段引用（异常发生在 build_query 之前时也要有定义）
    try:
        graph = getattr(graph_holder, "agent_graph", None)
        if graph is None:
            raise RuntimeError("llm_unavailable")

        from src.agent.state_machine import run_agent

        asset = record_event_asset(session_factory, event)
        query = build_query(event, asset, event.payload)

        def _on_node(node: str) -> None:
            """节点级观察通知（Q10）：投递给 SSE 观察者，不阻塞研判主流程。"""
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover - 恒在事件循环内调用
                return
            loop.create_task(
                _notify_observers(
                    record.id,
                    {"type": "node", "node": str(node), "diagnosisId": record.id},
                )
            )

        result_state = await asyncio.wait_for(
            run_agent(query, graph, on_node=_on_node),
            timeout=settings.DIAGNOSIS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        status = "timeout"
        degraded_reason = "timeout"
        error = f"研判超过 {settings.DIAGNOSIS_TIMEOUT_SECONDS}s 超时"
    except Exception as exc:  # noqa: BLE001 - 失败落库而非抛给前端
        status = "failed"
        degraded_reason = "llm_unavailable"
        error = str(exc)

    finished = time.perf_counter()
    with session_factory() as session:
        merged_event = session.merge(event)
        record = session.merge(record)
        record.status = status
        record.finished_at = _now()
        record.latency_ms = round((finished - started) * 1000, 2)
        record.error = error
        # 研判链路此前 trace_id 恒为空（agent 图不产出该字段），导致「Agent 链路」
        # 页无法按研判追查（2026-09-13 UI 穷举测试 P2）。优先沿用 agent 返回值
        # （兼容未来 agent 自产 trace），缺失时本地生成并登记进 AppState.traces
        # （结构对齐 routes.py 的 /diagnose record），供 /trace/{id} 查询。
        trace_id = str(result_state.get("trace_id") or "") or f"{int(time.time() * 1000)}-d{record.id}"
        record.trace_id = trace_id
        record.query = query

        answer = str(result_state.get("answer") or "")
        record.answer_summary = answer
        record.agent_iterations = int(result_state.get("iterations") or 0)
        sources = list(result_state.get("sources") or [])
        evidence: list[dict[str, Any]] = [
            {"seq": index + 1, "kind": "asset", "ref": str(event.asset_id or ""),
             "title": merged_event.title, "snippet": "资产与告警上下文（来自平台 DB）"}
            for index in (0,) if event.asset_id
        ]
        for source in sources:
            evidence.append(
                {
                    "seq": len(evidence) + 1,
                    "kind": "knowledge",
                    "ref": str(source.get("chunk_id") or ""),
                    "title": str(source.get("filename") or ""),
                    "snippet": str(source.get("content") or "")[:200],
                }
            )
        record.evidence = evidence
        reflection = result_state.get("reflection") or {}
        record.evidence_sufficient = bool(reflection.get("sufficient")) or bool(sources)
        confidence = _extract_confidence(answer)
        clamped, reason = _clamp_confidence(confidence, evidence)
        record.confidence = clamped
        if reason and degraded_reason is None:
            degraded_reason = reason
        root_cause, suggestion = _extract_sections(answer)
        record.root_cause = root_cause
        record.suggestion = suggestion
        # 有答案却抽不出三段式 → 显式标 unparsed（否则空字段会被当成"模型没给根因"，
        # 而 degraded=False 会把解析失败掩盖成正常完成——2026-09-12 真实踩坑）。
        if answer.strip() and root_cause is None and degraded_reason is None:
            degraded_reason = "unparsed"
        record.degraded = degraded_reason is not None
        record.degraded_reason = degraded_reason

        timeline_status = "diagnosis_finished"
        if status == "completed":
            merged_event.status = "acknowledged" if merged_event.status == "diagnosing" else merged_event.status
            append_timeline(
                session, merged_event, entry_type=timeline_status,
                content=f"研判完成：置信度 {record.confidence if record.confidence is not None else '未知'}，"
                        f"证据 {len(evidence)} 条",
                actor_type="agent", actor_name="FishCloud AIOps",
                detail={"diagnosis_id": record.id, "degraded": record.degraded},
            )
        else:
            if merged_event.status == "diagnosing":
                merged_event.status = "acknowledged"  # 超时/失败回退，避免卡死中间态
            append_timeline(
                session, merged_event, entry_type=timeline_status,
                content=f"研判未完成（{status}）：{error or degraded_reason or ''}",
                actor_type="agent", actor_name="FishCloud AIOps",
                detail={"diagnosis_id": record.id},
            )
        session.commit()

    # 登记研判链路到 AppState.traces（结构对齐 routes.py 的 /diagnose record），
    # 使「Agent 链路」页可按研判记录的 traceId 追查（2026-09-13 P2 修复）。
    _register_trace(graph_holder, {
        "trace_id": trace_id,
        "query": query,
        "answer": answer,
        "sources": sources,
        "iterations": int(result_state.get("iterations") or 0),
        "elapsed_ms": round((finished - started) * 1000, 2),
        "error": error,
    })

    await _notify_observers(record.id, {"type": "diagnosis_finished", "status": status})


def _register_trace(graph_holder: Any, trace: dict[str, Any]) -> None:
    """把一次研判的链路明细登记进 AppState.traces（有界双端队列）。

    失败（无 traces 属性/队列异常）只记 warning——链路追查属可观测性增强，
    绝不影响研判结果落库。

    Args:
        graph_holder: 提供 ``traces`` 属性的容器（AppState）。
        trace: 链路明细（字段与 routes.py 的 /diagnose 一致）。
    """
    traces = getattr(graph_holder, "traces", None)
    if traces is None:
        return
    try:
        traces.append(trace)
    except Exception:  # noqa: BLE001 - 观察性功能不得影响主流程
        logger.warning("研判链路登记失败（已忽略）: trace_id=%s", trace.get("trace_id"))


def record_event_asset(session_factory, event: Event) -> Asset | None:
    """加载事件关联资产（后台任务独立会话）。

    Args:
        session_factory: 会话工厂。
        event: 事件。

    Returns:
        资产或 None。
    """
    if event.asset_id is None:
        return None
    with session_factory() as session:
        return session.get(Asset, event.asset_id)


def _extract_confidence(answer: str) -> float | None:
    """从答案中抽取置信度（约定 ``置信度 0.xx`` 模式）；无则 None。

    Args:
        answer: 模型答案。

    Returns:
        [0,1] 置信度或 None。
    """
    import re

    match = re.search(r"置信度[：:=\s]*([01](?:\.\d+)?)", answer)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


async def _notify_observers(diagnosis_id: int, payload: dict[str, Any]) -> None:
    """向该研判的全部 SSE 观察者推送通知。

    节点级事件只投递、**不移除订阅**（Q10 裁决：观察端点需流式接收多个
    节点事件）；仅当收到 ``diagnosis_finished`` 时才清空该研判的全部订阅。

    Args:
        diagnosis_id: 研判 ID。
        payload: 通知载荷（type=node/diagnosis_finished）。
    """
    async with _observers_lock:
        queues = list(_observers.get(diagnosis_id, []))
        if payload.get("type") == "diagnosis_finished":
            _observers.pop(diagnosis_id, None)
    for queue in queues:
        try:
            await queue.put(payload)
        except Exception:  # noqa: BLE001 - 观察者断开不影响研判
            pass


async def register_observer(diagnosis_id: int) -> asyncio.Queue:
    """注册 SSE 观察者队列。

    Args:
        diagnosis_id: 研判 ID。

    Returns:
        订阅队列。
    """
    queue: asyncio.Queue = asyncio.Queue()
    async with _observers_lock:
        _observers.setdefault(diagnosis_id, []).append(queue)
    return queue


def unregister_observer(diagnosis_id: int, queue: asyncio.Queue) -> None:
    """注销观察者。

    Args:
        diagnosis_id: 研判 ID。
        queue: 订阅队列。
    """
    # 同步上下文下的尽力清理；队列随进程退出自然回收。
    queues = _observers.get(diagnosis_id)
    if queues and queue in queues:
        queues.remove(queue)
