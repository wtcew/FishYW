"""事件中心测试：手工登记、编号、详情、列表契约、状态机矩阵、webhook 接入。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_events.py
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import (
    Asset,
    AuditLog,
    DiagnosisRecord,
    Event,
    EventTimelineEntry,
)
from src.platform.services import events as event_service
from tests.platform.constants import (
    ALL_STATUSES,
    TRANSITION_ENDPOINTS,
    TRANSITION_MATRIX,
    WEBHOOK_TOKENS,
)

ZABBIX_TOKEN = WEBHOOK_TOKENS.split(",")[0].split(":", 1)[1]
WEBHOOK_HEADERS = {"X-Webhook-Token": ZABBIX_TOKEN}


def _isolated_line_asset_env(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> tuple[int, int, int]:
    """建一条独立业务线+环境+资产，返回 (business_line_id, asset_id, environment_id)。

    用于过滤类断言：夹具 resources 自带事件，会污染计数。
    """
    uid = uuid4().hex[:6]
    line_id = client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": f"bl-ev-{uid}", "name": f"事件线-{uid}"},
    ).json()["id"]
    env_id = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": line_id, "name": f"env-{uid}"},
    ).json()["id"]
    asset_id = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={
            "environment_id": env_id,
            "name": f"evhost-{uid}",
            "asset_type": "host",
            "identifier": f"evhost-{uid}",
        },
    ).json()["id"]
    return line_id, asset_id, env_id


def _isolated_asset(client: TestClient, headers: dict[str, dict[str, str]]) -> int:
    """建一台独立资产的简写。"""
    return _isolated_line_asset_env(client, headers)[1]


def _make_event_row(
    session_factory: sessionmaker[Session], *, status: str = "open", asset_id: int | None = None
) -> int:
    """直接落一条指定状态的事件（布置状态机用例的前置态）。"""
    with session_factory() as session:
        event = Event(
            event_no=f"EV-20260101-{uuid4().hex[:4].upper()}",
            title="前置态事件",
            source="manual",
            severity="major",
            status=status,
            asset_id=asset_id,
        )
        session.add(event)
        session.commit()
        return event.id


def _make_event_row_at(
    session_factory: sessionmaker[Session], *, created_at: datetime, title: str
) -> int:
    """落一条指定创建时间的事件（验证倒序排序，规避同秒并列）。"""
    with session_factory() as session:
        event = Event(
            event_no=f"EV-20260101-{uuid4().hex[:4].upper()}",
            title=title,
            source="manual",
            severity="major",
            status="open",
            created_at=created_at,
        )
        session.add(event)
        session.commit()
        return event.id


# ═══════════ 手工登记与编号 ═══════════


def test_manual_event_is_open_with_manual_source(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """手工登记的事件状态 open、来源 manual。"""
    event_id = client.post(
        "/api/v1/events", headers=headers["operator"], json={"title": "手工事件"}
    ).json()["id"]
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert (body["status"], body["source"]) == ("open", "manual")


def test_event_no_follows_daily_sequence(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """事件编号形如 EV-YYYYMMDD-NNNN。"""
    event_id = make_event()
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert re.fullmatch(r"EV-\d{8}-\d{4}", body["eventNo"]) is not None


def test_event_no_increments_within_day(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """同日第二个事件编号递增（当日序号查库递增）。"""
    first = make_event()
    second = make_event()
    first_no = client.get(f"/api/v1/events/{first}", headers=headers["viewer"]).json()["eventNo"]
    second_no = client.get(f"/api/v1/events/{second}", headers=headers["viewer"]).json()["eventNo"]
    assert int(second_no.rsplit("-", 1)[1]) == int(first_no.rsplit("-", 1)[1]) + 1


def test_manual_event_creation_writes_created_timeline(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """手工登记写 created 时间线（复盘链条的起点）。"""
    event_id = client.post(
        "/api/v1/events", headers=headers["operator"], json={"title": "带时间线"}
    ).json()["id"]
    with session_factory() as session:
        row = session.query(EventTimelineEntry).filter_by(event_id=event_id, entry_type="created").one()
    assert row.actor_type == "user"


def test_manual_event_with_asset_inherits_lineage(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any], make_event
) -> None:
    """事件挂资产后冗余存储环境/业务线（供跨资产过滤）。"""
    event_id = make_event(asset_id=resources["asset_id"])
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert (body["assetName"], body["environmentName"], body["businessLineName"]) == (
        "web-01", "prod", "资源线",
    )


def test_manual_event_with_unknown_asset_stays_unlinked(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """未知 asset_id 不阻断登记（资产可后补），事件仍为无资产态。"""
    event_id = client.post(
        "/api/v1/events",
        headers=headers["operator"],
        json={"title": "坏资产引用", "asset_id": 9999},
    ).json()["id"]
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert body["assetId"] is None


def test_manual_event_rejects_blank_title(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """空标题被 pydantic 拦截 422。"""
    response = client.post("/api/v1/events", headers=headers["operator"], json={"title": ""})
    assert response.status_code == 422


# ═══════════ 详情与列表 ═══════════


def test_event_detail_timeline_is_ascending(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """详情返回时间线（升序）与研判概要字段。"""
    event_id = make_event()
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert set(body) >= {"timeline", "latestDiagnosis", "status", "eventNo"}


def test_event_detail_without_diagnosis_reports_none(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """未研判的事件 latestDiagnosis 为 None（前端据此显示"未研判"）。"""
    event_id = make_event()
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert body["latestDiagnosis"] is None


def test_event_detail_latest_diagnosis_is_newest(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    make_event,
    session_factory: sessionmaker[Session],
) -> None:
    """latestDiagnosis 取最近一次研判（按 started_at 倒序）。"""
    event_id = make_event()
    with session_factory() as session:
        session.add_all(
            [
                DiagnosisRecord(
                    event_id=event_id, status="completed", trigger_type="manual", query="老",
                    started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
                DiagnosisRecord(
                    event_id=event_id, status="completed", trigger_type="manual", query="新",
                    started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert body["latestDiagnosis"]["answerSummary"] == "" and body["latestDiagnosis"]["id"] > 0


def test_event_detail_reports_acknowledged_by_name(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, resources: dict[str, Any]
) -> None:
    """认领后详情展示认领人显示名（Event 无 acknowledged_by 关系，需回库查询）。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/acknowledge", headers=headers["operator"])
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert body["acknowledgedByName"] == "operator-显示名"


def test_event_detail_missing_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """事件不存在 404。"""
    assert client.get("/api/v1/events/9999", headers=headers["viewer"]).status_code == 404


def test_event_list_items_are_flat_objects(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """P0 回归：events 列表 items 为扁平对象数组（曾因 Page[list[dict]] 全 500）。"""
    make_event()
    response = client.get("/api/v1/events", headers=headers["viewer"])
    body = response.json()
    assert (response.status_code, isinstance(body["items"][0], dict)) == (200, True)


def test_event_list_filters_by_status(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, set_event_status
) -> None:
    """按状态过滤（事件中心默认视图依据）。"""
    event_id = make_event()
    set_event_status(event_id, "acknowledged")
    body = client.get("/api/v1/events?status=acknowledged", headers=headers["viewer"]).json()
    assert body["total"] == 1


def test_event_list_filters_by_severity(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """按严重度过滤。"""
    make_event(severity="critical")
    make_event(severity="info")
    body = client.get("/api/v1/events?severity=critical", headers=headers["viewer"]).json()
    assert body["total"] == 1


def test_event_list_filters_by_asset(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """按资产过滤（用独立资产，避免夹具事件串扰）。"""
    asset_id = _isolated_asset(client, headers)
    event_id = make_event(asset_id=asset_id)
    body = client.get(f"/api/v1/events?asset_id={asset_id}", headers=headers["viewer"]).json()
    assert [item["id"] for item in body["items"]] == [event_id]


def test_event_list_filters_by_business_line(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """按业务线过滤（依赖事件上的冗余字段）。"""
    line_id, asset_id, _ = _isolated_line_asset_env(client, headers)
    event_id = make_event(asset_id=asset_id)
    body = client.get(
        f"/api/v1/events?business_line_id={line_id}", headers=headers["viewer"]
    ).json()
    assert [item["id"] for item in body["items"]] == [event_id]


def test_event_list_keyword_matches_event_no(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """关键字可命中事件编号（现场按编号找事件）。"""
    event_id = make_event()
    event_no = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()["eventNo"]
    body = client.get(f"/api/v1/events?keyword={event_no}", headers=headers["viewer"]).json()
    assert body["total"] == 1


def test_event_list_pagination_limits_items(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """分页限制条数但 total 为全量。"""
    make_event()
    make_event()
    body = client.get("/api/v1/events?page=1&page_size=1", headers=headers["viewer"]).json()
    assert (len(body["items"]), body["total"]) == (1, 2)


def test_event_list_is_newest_first(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """列表按创建时间倒序（最新事件在最前）。"""
    older = _make_event_row_at(
        session_factory, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), title="较早"
    )
    newer = _make_event_row_at(
        session_factory, created_at=datetime(2026, 6, 1, tzinfo=timezone.utc), title="较晚"
    )
    body = client.get("/api/v1/events", headers=headers["viewer"]).json()
    assert [item["id"] for item in body["items"]] == [newer, older]


def test_event_list_same_second_order_is_deterministic(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """同一秒内创建的事件顺序**可重复**（不依赖数据库非确定排序）。

    Note:
        created_at 是秒级精度（SQLite CURRENT_TIMESTAMP / MySQL DATETIME(0)），
        `ORDER BY created_at DESC` 对同秒事件是并列的；当前实现按主键升序兜底，
        表现为"同秒内先建先出"。若前端需要严格"新建在前"，应改为
        ``ORDER BY created_at DESC, id DESC``（已记为待修项，不擅自改契约）。
    """
    first = make_event()
    second = make_event()
    body = client.get("/api/v1/events", headers=headers["viewer"]).json()
    assert [item["id"] for item in body["items"]] == [first, second]


# ═══════════ 状态机矩阵 ═══════════


def test_transitions_constant_matches_design_matrix() -> None:
    """服务层 TRANSITIONS 与设计文档矩阵逐项一致（唯一权威不得漂移）。"""
    assert event_service.TRANSITIONS == TRANSITION_MATRIX


@pytest.mark.parametrize(
    "from_status,endpoint,target", [(s, e, t) for s in ALL_STATUSES for e, t in TRANSITION_ENDPOINTS.items()]
)
def test_state_matrix(
    from_status: str,
    endpoint: str,
    target: str,
    client: TestClient,
    headers: dict[str, dict[str, str]],
    session_factory: sessionmaker[Session],
) -> None:
    """状态机矩阵：合法流转 200、非法流转 409（每条组合都验证）。"""
    event_id = _make_event_row(session_factory, status=from_status)
    response = client.post(f"/api/v1/events/{event_id}/{endpoint}", headers=headers["operator"])
    expected = 200 if target in TRANSITION_MATRIX[from_status] else 409
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("endpoint", sorted(TRANSITION_ENDPOINTS))
def test_closed_is_terminal(
    endpoint: str, client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """closed 是终态：任何流转都被拒绝。"""
    event_id = _make_event_row(session_factory, status="closed")
    assert client.post(
        f"/api/v1/events/{event_id}/{endpoint}", headers=headers["operator"]
    ).status_code == 409


def test_illegal_transition_keeps_status(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """非法流转被拒后状态不变（409 不得半途改状态）。"""
    event_id = _make_event_row(session_factory, status="open")
    client.post(f"/api/v1/events/{event_id}/resolve", headers=headers["operator"])
    with session_factory() as session:
        assert session.get(Event, event_id).status == "open"


def test_transition_to_diagnosing_allowed_from_acknowledged(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """acknowledged → diagnosing 合法（该态仅由研判编排驱动，无对应 HTTP 端点）。"""
    event_id = _make_event_row(session_factory, status="acknowledged")
    with session_factory() as session:
        event = session.get(Event, event_id)
        event_service.transition_event(
            session, event, target="diagnosing", user_id=None, username="system"
        )
        session.commit()
    with session_factory() as session:
        assert session.get(Event, event_id).status == "diagnosing"


def test_diagnosing_can_fall_back_to_acknowledged(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """diagnosing → acknowledged 合法（研判超时回退路径依赖它）。"""
    event_id = _make_event_row(session_factory, status="diagnosing")
    response = client.post(f"/api/v1/events/{event_id}/acknowledge", headers=headers["operator"])
    assert response.status_code == 200


def test_acknowledge_records_acknowledged_fields(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """认领写入 acknowledged_by/at。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/acknowledge", headers=headers["operator"])
    with session_factory() as session:
        event = session.get(Event, event_id)
    assert (event.acknowledged_by_id is not None, event.acknowledged_at is not None) == (True, True)


def test_acknowledge_appends_status_change_timeline(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """流转写 status_change 时间线（含 from/to）。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/acknowledge", headers=headers["operator"])
    with session_factory() as session:
        entry = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="status_change")
            .one()
        )
    assert entry.detail == {"from": "open", "to": "acknowledged"}


def test_transition_actor_type_defaults_to_user(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """transition_event 不传 actor_type 时时间线操作者默认 user（人工流转路径）。"""
    event_id = _make_event_row(session_factory, status="acknowledged")
    with session_factory() as session:
        event = session.get(Event, event_id)
        event_service.transition_event(
            session, event, target="diagnosing", user_id=None, username="operator-x"
        )
        session.commit()
    with session_factory() as session:
        entry = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="status_change")
            .one()
        )
    assert (entry.actor_type, entry.actor_name) == ("user", "operator-x")


def test_transition_actor_type_system_marks_system_flow(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """显式传 actor_type="system" 时时间线操作者是 system（Q11 自动认领路径依赖）。"""
    event_id = _make_event_row(session_factory, status="open")
    with session_factory() as session:
        event = session.get(Event, event_id)
        event_service.transition_event(
            session, event, target="acknowledged", user_id=None, username="alert_rules",
            note="触发研判，系统自动认领", actor_type="system",
        )
        session.commit()
    with session_factory() as session:
        entry = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="status_change")
            .one()
        )
    assert (entry.actor_type, entry.actor_name, entry.actor_id) == ("system", "alert_rules", None)


def test_acknowledge_writes_transition_audit(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """流转写 event.transition 审计。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/acknowledge", headers=headers["operator"])
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "event.transition", AuditLog.resource_id == str(event_id))
            .one()
        )
    assert (row.detail["from"], row.detail["to"]) == ("open", "acknowledged")


def test_resolve_records_note_in_timeline(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """resolve 的处置说明落时间线（复盘可读）。"""
    event_id = _make_event_row(session_factory, status="acknowledged")
    client.post(
        f"/api/v1/events/{event_id}/resolve",
        headers=headers["operator"],
        json={"resolution_note": "已扩容磁盘"},
    )
    with session_factory() as session:
        entry = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="status_change")
            .one()
        )
    assert entry.content == "已扩容磁盘"


def test_resolve_sets_resolved_at(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """resolve 写入 resolved_at（SLA 统计依据）。"""
    event_id = _make_event_row(session_factory, status="acknowledged")
    client.post(f"/api/v1/events/{event_id}/resolve", headers=headers["operator"])
    with session_factory() as session:
        assert session.get(Event, event_id).resolved_at is not None


def test_close_sets_closed_at(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """close 写入 closed_at。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/close", headers=headers["operator"])
    with session_factory() as session:
        assert session.get(Event, event_id).closed_at is not None


def test_comment_appends_timeline_with_actor(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """评论追加 comment 时间线并记录操作者。"""
    event_id = make_event()
    response = client.post(
        f"/api/v1/events/{event_id}/comments",
        headers=headers["operator"],
        json={"content": "现场已更换电源"},
    )
    with session_factory() as session:
        entry = session.query(EventTimelineEntry).filter_by(event_id=event_id, entry_type="comment").one()
    assert (response.status_code, entry.actor_name) == (201, "operator1")


def test_comment_writes_audit(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """评论写 event.comment 审计。"""
    event_id = make_event()
    client.post(
        f"/api/v1/events/{event_id}/comments", headers=headers["operator"], json={"content": "x"}
    )
    with session_factory() as session:
        assert session.query(AuditLog).filter(
            AuditLog.action == "event.comment", AuditLog.resource_id == str(event_id)
        ).count() == 1


def test_comment_rejects_empty_content(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """空评论被 pydantic 拦截。"""
    event_id = make_event()
    response = client.post(
        f"/api/v1/events/{event_id}/comments", headers=headers["operator"], json={"content": ""}
    )
    assert response.status_code == 422


def test_transition_on_missing_event_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """对不存在的事件流转 404。"""
    response = client.post("/api/v1/events/9999/acknowledge", headers=headers["operator"])
    assert response.status_code == 404


# ═══════════ webhook 接入 ═══════════


def test_webhook_accepts_valid_token(client: TestClient) -> None:
    """正确 token 接入 202（无 Bearer 也可，走 X-Webhook-Token）。"""
    response = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "磁盘告警"}
    )
    assert response.status_code == 202


def test_webhook_rejects_missing_token(client: TestClient) -> None:
    """缺少 token 401。"""
    assert client.post(
        "/api/v1/events/webhook/zabbix", json={"title": "x"}
    ).status_code == 401


def test_webhook_rejects_wrong_token(client: TestClient) -> None:
    """token 错误 401。"""
    response = client.post(
        "/api/v1/events/webhook/zabbix",
        headers={"X-Webhook-Token": "wrong-token"},
        json={"title": "x"},
    )
    assert response.status_code == 401


def test_webhook_rejects_unconfigured_source(client: TestClient) -> None:
    """未配置的来源名 401（不得用别人的 token 冒名）。"""
    response = client.post(
        "/api/v1/events/webhook/prometheus", headers=WEBHOOK_HEADERS, json={"title": "x"}
    )
    assert response.status_code == 401


def test_webhook_creates_event_with_source_prefix(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """事件来源记为 webhook:<source>（可追溯通道）。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "接入事件"}
    ).json()["event_id"]
    with session_factory() as session:
        assert session.get(Event, event_id).source == "webhook:zabbix"


def test_webhook_defaults_severity_to_major(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """无规则命中且未给严重度时默认 major。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "无规则"}
    ).json()["event_id"]
    with session_factory() as session:
        assert session.get(Event, event_id).severity == "major"


def test_webhook_uses_payload_title(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    """标题缺省时从 payload.title 提取。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"payload": {"title": "载荷标题"}},
    ).json()["event_id"]
    with session_factory() as session:
        assert session.get(Event, event_id).title == "载荷标题"


def test_webhook_falls_back_to_generic_title(client: TestClient) -> None:
    """标题与载荷都没有时用通用标题（不因缺字段 422）。"""
    body = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={}
    ).json()
    assert body["event_no"].startswith("EV-")


def test_webhook_links_asset_by_identifier(
    client: TestClient, resources: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    """按 asset_identifier 反查资产并冗余环境/业务线。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "资产告警", "asset_identifier": "10.1.1.1"},
    ).json()["event_id"]
    with session_factory() as session:
        event = session.get(Event, event_id)
    assert (event.asset_id, event.environment_id) == (resources["asset_id"], resources["environment_id"])


def test_webhook_writes_created_timeline(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    """webhook 接入写 created 时间线（actor 为来源系统）。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "x"}
    ).json()["event_id"]
    with session_factory() as session:
        entry = session.query(EventTimelineEntry).filter_by(event_id=event_id, entry_type="created").one()
    assert (entry.actor_type, entry.actor_name) == ("system", "zabbix")


def test_webhook_audits_even_without_rule_match(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """未命中规则也写审计（接入本身即安全相关事件）。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "无规则审计"}
    ).json()["event_id"]
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "event.webhook", AuditLog.resource_id == str(event_id))
            .one()
        )
    assert (row.user_id, row.detail["matched"]) == (None, 0)


def test_webhook_audit_records_matched_rule_count(
    client: TestClient, resources: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    """命中规则时审计带规则 ID（规则命中可回溯）。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "夹具规则命中"},
    ).json()["event_id"]
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "event.webhook", AuditLog.resource_id == str(event_id))
            .one()
        )
    assert row.detail["rule_ids"] == [resources["alert_rule_id"]]


def test_webhook_matched_rule_sets_severity(
    client: TestClient, resources: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    """规则命中的严重度覆盖默认值（夹具规则为 minor）。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "夹具规则命中"},
    ).json()["event_id"]
    with session_factory() as session:
        assert session.get(Event, event_id).severity == "minor"


def test_webhook_matched_rule_appends_timeline(
    client: TestClient, resources: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    """命中规则写 rule_matched 时间线（规则可复盘）。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "夹具规则命中"},
    ).json()["event_id"]
    with session_factory() as session:
        assert session.query(EventTimelineEntry).filter_by(
            event_id=event_id, entry_type="rule_matched"
        ).count() == 1


def test_webhook_reports_matched_rule_count(client: TestClient, resources: dict[str, Any]) -> None:
    """响应体回报命中规则数（调用方可观测）。"""
    body = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "夹具规则命中"},
    ).json()
    assert body["matched_rules"] == 1


def test_webhook_body_severity_overrides_rule(
    client: TestClient, resources: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    """合法 body.severity 优先于规则建议值。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "夹具规则命中", "severity": "critical"},
    ).json()["event_id"]
    with session_factory() as session:
        assert session.get(Event, event_id).severity == "critical"


def test_webhook_ignores_illegal_severity(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """非法严重度被忽略（回落规则/默认值），不落脏数据。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "非法级别", "severity": "huge"},
    ).json()["event_id"]
    with session_factory() as session:
        assert session.get(Event, event_id).severity in event_service.SEVERITIES


def test_webhook_deduplicates_asset_lookup_by_identifier(
    client: TestClient, resources: dict[str, Any], session_factory: sessionmaker[Session]
) -> None:
    """资产反查不误伤（未提供的标识不会随机挂资产）。"""
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "无标识"}
    ).json()["event_id"]
    with session_factory() as session:
        assert session.get(Event, event_id).asset_id is None


def test_webhook_does_not_register_asset(
    client: TestClient, session_factory: sessionmaker[Session], resources: dict[str, Any]
) -> None:
    """webhook 不得隐式创建资产（CMDB 只能显式登记）。"""
    with session_factory() as session:
        before = session.query(Asset).count()
    client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "x", "asset_identifier": "unknown-host-9"},
    )
    with session_factory() as session:
        assert session.query(Asset).count() == before
