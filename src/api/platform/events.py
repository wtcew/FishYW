"""事件中心路由：列表/详情/手工登记/webhook/状态流转/评论/AI 研判。

webhook 端点不走 RBAC，走 ``X-Webhook-Token``（settings.WEBHOOK_TOKENS 解析）。
研判默认轮询 ``GET /events/diagnoses/{id}``；SSE 观察端点为增强。

Note:
    研判任务投递用 :class:`fastapi.BackgroundTasks` 而非
    ``asyncio.get_running_loop().create_task``：本模块的路由是 sync def，
    由 Starlette 放到 threadpool 执行、**没有运行中的事件循环**，直接取
    running loop 会抛 RuntimeError（500）。BackgroundTasks 由 Starlette 在
    事件循环里 await，且响应体先于研判送达调用方（202 语义不变）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import case
from sqlalchemy.orm import Session

from src.platform.audit import write_audit
from src.platform.db import get_db, get_session_factory
from src.platform.deps import require_roles
from src.platform.models import Asset, DiagnosisRecord, Event, User
from src.platform.schemas import (
    CommentIn,
    DiagnosisOut,
    EventCreateIn,
    EventOut,
    Page,
    TimelineEntryOut,
    WebhookIn,
)
from src.platform.services import events as event_service
from src.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


def _event_out(event: Event, db: Session) -> dict[str, Any]:
    """构造事件出参（展开资产/环境/业务线/认领人）。

    Args:
        event: 事件 ORM 对象。
        db: 平台会话（认领人名需回库查询——Event 未定义 acknowledged_by 关系）。

    Returns:
        camelCase 出参字典。
    """
    asset_name = event.asset.name if event.asset else ""
    env_name = event.asset.environment.name if event.asset and event.asset.environment else ""
    bl_name = (
        event.asset.environment.business_line.name
        if event.asset and event.asset.environment and event.asset.environment.business_line
        else ""
    )
    ack_name = ""
    if event.acknowledged_by_id:
        ack_user = db.get(User, event.acknowledged_by_id)
        ack_name = (ack_user.display_name or ack_user.username) if ack_user else ""
    return EventOut(
        id=event.id,
        eventNo=event.event_no,
        title=event.title,
        source=event.source,
        severity=event.severity,
        status=event.status,
        assetId=event.asset_id,
        assetName=asset_name,
        environmentName=env_name,
        businessLineName=bl_name,
        acknowledgedByName=ack_name,
        createdAt=event.created_at.isoformat() if event.created_at else None,
        resolvedAt=event.resolved_at.isoformat() if event.resolved_at else None,
    ).model_dump()


@router.get("")
def list_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    asset_id: int | None = Query(default=None),
    business_line_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None),
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> Page[dict[str, Any]]:
    """事件分页列表（状态/严重度/资产/业务线/关键字过滤，倒序）。"""
    query = db.query(Event)
    if status_filter:
        query = query.filter(Event.status == status_filter)
    if severity:
        query = query.filter(Event.severity == severity)
    if asset_id is not None:
        query = query.filter(Event.asset_id == asset_id)
    if business_line_id is not None:
        query = query.filter(Event.business_line_id == business_line_id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(Event.title.like(like) | Event.event_no.like(like))
    total = query.count()
    events = (
        query.order_by(Event.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return Page(
        items=[_event_out(event, db) for event in events],
        total=total, page=page, page_size=page_size,
    )


@router.get("/{event_id}")
def get_event(
    event_id: int,
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """事件详情（含时间线升序与最近研判概要）。"""
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    latest = (
        db.query(DiagnosisRecord)
        .filter_by(event_id=event_id)
        .order_by(DiagnosisRecord.started_at.desc())
        .first()
    )
    return {
        **_event_out(event, db),
        "timeline": [
            TimelineEntryOut(
                id=entry.id,
                entryType=entry.entry_type,
                actorType=entry.actor_type,
                actorName=entry.actor_name,
                content=entry.content,
                detail=entry.detail,
                createdAt=entry.created_at.isoformat() if entry.created_at else None,
            ).model_dump()
            for entry in event.timeline
        ],
        "latestDiagnosis": (
            DiagnosisOut(
                id=latest.id, eventId=latest.event_id, status=latest.status,
                triggerType=latest.trigger_type, rootCause=latest.root_cause,
                suggestion=latest.suggestion, confidence=latest.confidence,
                evidence=latest.evidence or [], evidenceSufficient=latest.evidence_sufficient,
                answerSummary=latest.answer_summary, degraded=latest.degraded,
                degradedReason=latest.degraded_reason, error=latest.error,
                latencyMs=latest.latency_ms, traceId=latest.trace_id,
                startedAt=latest.started_at.isoformat() if latest.started_at else None,
                finishedAt=latest.finished_at.isoformat() if latest.finished_at else None,
            ).model_dump()
            if latest is not None
            else None
        ),
    }


@router.post("", status_code=201)
def create_event(
    body: EventCreateIn,
    request: Request,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """手工登记事件（可关联资产；来源 manual）。"""
    asset = db.get(Asset, body.asset_id) if body.asset_id else None
    event = Event(
        event_no=event_service.generate_event_no(db),
        title=body.title,
        source="manual",
        severity=body.severity,
        status="open",
        asset_id=asset.id if asset else None,
        environment_id=asset.environment_id if asset else None,
        business_line_id=(
            asset.environment.business_line_id if asset and asset.environment else None
        ),
        payload=body.payload,
        created_by_id=user.id,
    )
    db.add(event)
    db.flush()
    event_service.append_timeline(
        db, event, entry_type="created", content="手工登记",
        actor_type="user", actor_id=user.id, actor_name=user.username,
    )
    write_audit(
        db, action="event.create", resource_type="event", resource_id=str(event.id),
        user_id=user.id, username=user.username, detail={"title": event.title},
        ip=_client_ip(request),
    )
    db.commit()
    return {"id": event.id, "eventNo": event.event_no, "status": "created"}


def _webhook_tokens() -> dict[str, str]:
    """解析 ``settings.WEBHOOK_TOKENS``（``source:token`` 逗号分隔）。

    Returns:
        来源名 → 期望 token 的映射；未配置的 source 不出现在结果中。
    """
    tokens: dict[str, str] = {}
    for pair in get_settings().WEBHOOK_TOKENS.split(","):
        name, _, token = pair.partition(":")
        if name.strip() and token.strip():
            tokens[name.strip()] = token.strip()
    return tokens


@router.post("/webhook/{source}", status_code=202)
def ingest_webhook(
    source: str,
    body: WebhookIn,
    request: Request,
    background: BackgroundTasks,
    x_webhook_token: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """外部告警 webhook 接入（X-Webhook-Token 鉴权 → 规则匹配 → 建事件）。"""
    expected = _webhook_tokens().get(source)
    if not expected or x_webhook_token != expected:
        raise HTTPException(status_code=401, detail="webhook token 无效")

    payload = body.payload or body.model_dump(exclude={"title", "severity", "asset_identifier"})
    title = body.title or str(payload.get("title") or payload.get("name") or "外部告警")
    identifier = body.asset_identifier or str(payload.get("asset_identifier") or "")
    matched, severity = event_service.evaluate_rules(
        db, title=title, source=source, payload=payload, asset_identifier=identifier
    )
    final_severity = body.severity if body.severity in event_service.SEVERITIES else severity
    # 标识非全局唯一（唯一约束是 环境+类型+标识 三元组），同一 identifier 可能命中
    # 多条资产；此处按 活跃优先 → id 升序 确定性选取，绝不因多命中抛 500
    # （2026-09-13 UI 穷举测试 BUG-03：原 one_or_none() 遇多命中抛 MultipleResultsFound）。
    asset: Asset | None = None
    if identifier:
        candidates = (
            db.query(Asset)
            .filter(Asset.identifier == identifier)
            # 活跃资产优先，其次按登记顺序（id 升序）——确定性、可复现。
            .order_by(case((Asset.status == "active", 0), else_=1), Asset.id.asc())
            .limit(2)
            .all()
        )
        asset = candidates[0] if candidates else None
        if len(candidates) > 1:
            logger.warning(
                "webhook 标识命中多条资产，按确定性策略选取: identifier=%s chosen_asset_id=%s",
                identifier, asset.id if asset else None,
            )
    event = Event(
        event_no=event_service.generate_event_no(db),
        title=title,
        source=f"webhook:{source}",
        severity=final_severity,
        status="open",
        asset_id=asset.id if asset else None,
        environment_id=asset.environment_id if asset else None,
        business_line_id=(
            asset.environment.business_line_id if asset and asset.environment else None
        ),
        payload=payload,
    )
    db.add(event)
    db.flush()
    event_service.append_timeline(
        db, event, entry_type="created", content=f"webhook 接入（{source}）",
        actor_type="system", actor_name=source,
    )
    if matched:
        event_service.append_timeline(
            db, event, entry_type="rule_matched",
            content=f"命中规则：{', '.join(rule.name for rule in matched)}",
            actor_type="system", actor_name="alert_rules",
            detail={"rule_ids": [rule.id for rule in matched]},
        )
    # webhook 接入本身即安全相关事件：无论是否命中规则都无条件审计。
    write_audit(
        db, action="event.webhook", resource_type="event", resource_id=str(event.id),
        username=source,
        detail={
            "matched": len(matched),
            "rule_ids": [rule.id for rule in matched],
            "severity": final_severity,
        },
        ip=_client_ip(request),
    )
    auto = any(rule.auto_diagnose for rule in matched)
    db.commit()

    queued = False
    if auto and get_settings().AUTO_DIAGNOSIS_ENABLED:
        record = _queue_diagnosis(db, event.id, trigger_type="rule", triggered_by_id=None)
        queued = _enqueue_diagnosis(background, record, event)
    return {
        "event_id": event.id,
        "event_no": event.event_no,
        "matched_rules": len(matched),
        "auto_diagnosis_queued": queued,
    }


def _client_ip(request: Request) -> str:
    """取来源 IP。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/{event_id}/acknowledge")
def acknowledge_event(
    event_id: int,
    request: Request,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """认领事件（open→acknowledged）。"""
    event = _get_or_404(db, event_id)
    try:
        event_service.transition_event(
            db, event, target="acknowledged", user_id=user.id, username=user.username,
            ip=_client_ip(request),
        )
    except event_service.TransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"status": event.status}


@router.post("/{event_id}/resolve")
def resolve_event(
    event_id: int,
    request: Request,
    body: dict[str, str] | None = None,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """恢复事件（需处置说明）。"""
    event = _get_or_404(db, event_id)
    try:
        event_service.transition_event(
            db, event, target="resolved", user_id=user.id, username=user.username,
            note=(body or {}).get("resolution_note", ""), ip=_client_ip(request),
        )
    except event_service.TransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"status": event.status}


@router.post("/{event_id}/close")
def close_event(
    event_id: int,
    request: Request,
    body: dict[str, str] | None = None,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """关闭事件（终态）。"""
    event = _get_or_404(db, event_id)
    try:
        event_service.transition_event(
            db, event, target="closed", user_id=user.id, username=user.username,
            note=(body or {}).get("comment", ""), ip=_client_ip(request),
        )
    except event_service.TransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"status": event.status}


@router.post("/{event_id}/comments", status_code=201)
def add_comment(
    event_id: int,
    body: CommentIn,
    request: Request,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """追加复盘评论（closed 终态除外，与状态流转的终态语义一致）。"""
    event = _get_or_404(db, event_id)
    if event.status == "closed":
        # 2026-09-13 UI 穷举测试 BUG-05：终态事件仍可经 API 追加评论（UI 已隐藏评论区），
        # 与 acknowledge/close 的 409 语义不一致——此处统一为 409。
        raise HTTPException(status_code=409, detail="事件已关闭，不可再追加评论")
    event_service.append_timeline(
        db, event, entry_type="comment", content=body.content,
        actor_type="user", actor_id=user.id, actor_name=user.username,
    )
    write_audit(
        db, action="event.comment", resource_type="event", resource_id=str(event_id),
        user_id=user.id, username=user.username, ip=_client_ip(request),
    )
    db.commit()
    return {"status": "created"}


def _prepare_event_for_diagnosis(
    db: Session,
    event: Event,
    *,
    user_id: int | None,
    username: str,
    actor_type: str = "user",
) -> None:
    """研判前的状态准备（Q11 裁决 2026-09-13：研判期间事件真实进入 diagnosing）。

    规则：
    - ``closed`` → 409（终态，不可研判）；
    - ``resolved`` → 409（已恢复，无需研判）；
    - ``open`` → 先自动认领（open→acknowledged，时间线/审计同事务留痕），
      再流转 acknowledged→diagnosing——两条均为状态机合法路径，矩阵不变；
    - ``acknowledged`` → 直接流转到 diagnosing；
    - ``diagnosing`` → 保持（允许并发追加研判记录，不重复流转）。

    Args:
        db: 平台会话（与调用方同一事务，由调用方 commit）。
        event: 目标事件。
        user_id: 触发人（系统自动路径为 None）。
        username: 触发人名（系统路径传告警源/系统名）。
        actor_type: 时间线操作者类型。

    Raises:
        HTTPException: 409（终态/已恢复/非法流转）。
    """
    if event.status == "closed":
        raise HTTPException(status_code=409, detail="事件已关闭，无法触发研判")
    if event.status == "resolved":
        raise HTTPException(status_code=409, detail="事件已恢复，无需研判")
    try:
        if event.status == "open":
            # 触发研判意味着有人接管：系统自动认领，避免"open 直接研判"绕过认领语义。
            event_service.transition_event(
                db, event, target="acknowledged", user_id=user_id, username=username,
                note="触发研判，系统自动认领", actor_type=actor_type,
            )
        if event.status == "acknowledged":
            event_service.transition_event(
                db, event, target="diagnosing", user_id=user_id, username=username,
                note="AI 研判进行中", actor_type=actor_type,
            )
    except event_service.TransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{event_id}/diagnose", status_code=202)
def trigger_diagnosis(
    event_id: int,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """触发 AI 研判（202 受理；后台执行，前端轮询 GET /events/diagnoses/{id}）。"""
    event = _get_or_404(db, event_id)
    _prepare_event_for_diagnosis(
        db, event, user_id=user.id, username=user.username, actor_type="user",
    )
    record = DiagnosisRecord(
        event_id=event_id, status="running", trigger_type="manual",
        triggered_by_id=user.id, query="",
    )
    db.add(record)
    db.flush()
    event_service.append_timeline(
        db, event, entry_type="diagnosis_started", content="人工触发 AI 研判",
        actor_type="user", actor_id=user.id, actor_name=user.username,
        detail={"diagnosis_id": record.id},
    )
    write_audit(
        db, action="diagnosis.trigger", resource_type="event", resource_id=str(event_id),
        user_id=user.id, username=user.username, detail={"diagnosis_id": record.id},
        ip=_client_ip(request),
    )
    db.commit()

    _enqueue_diagnosis(background, record, event)
    return {"diagnosis_id": record.id, "status": "running"}


@router.get("/diagnoses/{diagnosis_id}")
def get_diagnosis(
    diagnosis_id: int,
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """研判记录详情（前端轮询用）。"""
    record = db.get(DiagnosisRecord, diagnosis_id)
    if record is None:
        raise HTTPException(status_code=404, detail="研判记录不存在")
    return DiagnosisOut(
        id=record.id, eventId=record.event_id, status=record.status,
        triggerType=record.trigger_type, rootCause=record.root_cause,
        suggestion=record.suggestion, confidence=record.confidence,
        evidence=record.evidence or [], evidenceSufficient=record.evidence_sufficient,
        answerSummary=record.answer_summary, degraded=record.degraded,
        degradedReason=record.degraded_reason, error=record.error,
        latencyMs=record.latency_ms, traceId=record.trace_id,
        startedAt=record.started_at.isoformat() if record.started_at else None,
        finishedAt=record.finished_at.isoformat() if record.finished_at else None,
    ).model_dump()


@router.get("/{event_id}/diagnoses")
def list_event_diagnoses(
    event_id: int,
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """该事件的研判记录列表（Q10 裁决：按 PRD §3.3 补齐，开始时间倒序）。

    前端"历史研判"用本端点，替代早前经时间线回填的过渡实现。
    """
    if db.get(Event, event_id) is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    records = (
        db.query(DiagnosisRecord)
        .filter(DiagnosisRecord.event_id == event_id)
        .order_by(DiagnosisRecord.started_at.desc(), DiagnosisRecord.id.desc())
        .all()
    )
    items = [
        DiagnosisOut(
            id=r.id, eventId=r.event_id, status=r.status,
            triggerType=r.trigger_type, rootCause=r.root_cause,
            suggestion=r.suggestion, confidence=r.confidence,
            evidence=r.evidence or [], evidenceSufficient=r.evidence_sufficient,
            answerSummary=r.answer_summary, degraded=r.degraded,
            degradedReason=r.degraded_reason, error=r.error,
            latencyMs=r.latency_ms, traceId=r.trace_id,
            startedAt=r.started_at.isoformat() if r.started_at else None,
            finishedAt=r.finished_at.isoformat() if r.finished_at else None,
        ).model_dump()
        for r in records
    ]
    return {"items": items, "total": len(items)}


def _node_event_name(node: str) -> str:
    """SSE 事件名映射：与既有 /diagnose 契约一致（hallucination_check → hallucination）。"""
    return "hallucination" if node == "hallucination_check" else node


def _sse(event: str, payload: dict[str, Any]) -> str:
    """构造一条 SSE 帧（与 routes.py 的 /diagnose 同格式）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/{event_id}/diagnose/stream")
async def stream_diagnosis(
    event_id: int,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """研判观察窗口（SSE，Q10 裁决按 PRD §5.4 落地）。

    纯观察：不影响研判落库。事件名复用既有 /diagnose 契约
    （decompose/retrieve/reflect/generate/hallucination/done/error）。

    行为：
    - 事件存在但有进行中研判 → 推送 running 状态 + 逐节点事件 + done；
    - 无进行中研判 → 立即推送 status=idle + done 后结束；
    - 断开连接只影响观察，研判照常完成。
    """
    if db.get(Event, event_id) is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    record = (
        db.query(DiagnosisRecord)
        .filter(DiagnosisRecord.event_id == event_id, DiagnosisRecord.status == "running")
        .order_by(DiagnosisRecord.started_at.desc())
        .first()
    )

    async def generator():  # noqa: ANN202 - StreamingResponse 生成器
        if record is None:
            yield _sse("status", {"status": "idle", "eventId": event_id})
            yield _sse("done", {"status": "idle"})
            return
        yield _sse("status", {"status": "running", "diagnosisId": record.id, "eventId": event_id})
        queue = await event_service.register_observer(record.id)
        try:
            # 注册后复查：研判可能在注册前已完成（完成时订阅表即被清空，
            # 这里再查一次库，避免等待一个永远不会到来的 done）。
            with get_session_factory() as check:
                fresh = check.get(DiagnosisRecord, record.id)
                if fresh is None or fresh.status != "running":
                    yield _sse("done", {"status": fresh.status if fresh else "unknown",
                                        "diagnosisId": record.id})
                    return
            settings = get_settings()
            deadline = time.time() + settings.DIAGNOSIS_TIMEOUT_SECONDS + 30
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    yield _sse("error", {"detail": "观察窗口超时（研判仍在后台进行）"})
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=min(remaining, 15.0))
                except asyncio.TimeoutError:
                    yield _sse("ping", {"ts": time.time()})
                    continue
                ptype = str(payload.get("type") or "")
                if ptype == "node":
                    yield _sse(_node_event_name(str(payload.get("node") or "")), payload)
                elif ptype == "diagnosis_finished":
                    yield _sse("done", {"status": payload.get("status"),
                                        "diagnosisId": record.id})
                    break
        finally:
            event_service.unregister_observer(record.id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _get_or_404(db: Session, event_id: int) -> Event:
    """取事件或 404。

    Args:
        db: 平台会话。
        event_id: 事件 ID。

    Returns:
        事件 ORM 对象。
    """
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return event


def _resolve_app_state() -> Any | None:
    """取当前 App 的 AIOps 状态容器（``app.state.rag``）。

    Returns:
        AppState；无法导入 App 或无该属性时返回 None（调用方降级跳过研判，
        而不是把异常抛给已受理的 webhook 调用方）。
    """
    try:
        from src.api.routes import app
    except Exception:  # noqa: BLE001 - 独立导入服务层（无 App）的场景
        logger.warning("无法导入 src.api.routes.app，跳过自动研判投递")
        return None
    return getattr(app.state, "rag", None)


async def _run_diagnosis_guarded(session_factory, app_state, record, event) -> None:
    """后台任务薄包装：研判内部异常不外溢到 ASGI 层（响应已发出）。"""
    try:
        await event_service.run_diagnosis(session_factory, app_state, record, event)
    except Exception:  # noqa: BLE001 - 后台任务失败不应影响已完成的请求
        logger.exception("后台研判任务异常（diagnosis_id=%s）", getattr(record, "id", None))


def _enqueue_diagnosis(background: BackgroundTasks, record, event) -> bool:
    """把已落库的 running 态研判登记为响应后台任务。

    Args:
        background: FastAPI 注入的后台任务收集器（Starlette 在事件循环中 await）。
        record: 已提交的研判记录（running）。
        event: 目标事件。

    Returns:
        True=已登记后台任务；False=无 AppState（降级：只留 running 记录不执行）。
    """
    app_state = _resolve_app_state()
    if app_state is None:
        logger.warning("AppState 不可用，研判 diagnosis_id=%s 不投递", record.id)
        return False
    background.add_task(
        _run_diagnosis_guarded, get_session_factory(), app_state, record, event
    )
    return True


def _queue_diagnosis(db: Session, event_id: int, *, trigger_type: str,
                     triggered_by_id: int | None) -> DiagnosisRecord:
    """为事件创建自动研判记录（webhook auto_diagnose 路径）。

    只负责落库（status=running）与时间线；投递由 :func:`_enqueue_diagnosis` 完成。

    Args:
        db: 平台会话。
        event_id: 事件 ID。
        trigger_type: 触发类型（rule）。
        triggered_by_id: 触发人（系统为 None）。

    Returns:
        已提交的研判记录。
    """
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    # Q11：webhook 自动研判同样走状态准备（open → 自动认领 → diagnosing）。
    _prepare_event_for_diagnosis(
        db, event, user_id=triggered_by_id, username="alert_rules", actor_type="system",
    )
    event_service.append_timeline(
        db, event, entry_type="diagnosis_started", content="规则命中，自动触发 AI 研判",
        actor_type="system", actor_name="alert_rules",
    )
    record = DiagnosisRecord(
        event_id=event_id, status="running", trigger_type=trigger_type,
        triggered_by_id=triggered_by_id, query="",
    )
    db.add(record)
    db.commit()
    return record
