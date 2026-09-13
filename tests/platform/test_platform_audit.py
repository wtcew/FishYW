"""审计测试：同事务写入、各写端点留痕、审计查询过滤与分页契约。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_audit.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.platform.audit import write_audit
from src.platform.models import Asset, AuditLog


def _actions(session_factory: sessionmaker[Session]) -> list[str]:
    """取全部审计动作码（按写入顺序）。"""
    with session_factory() as session:
        return [row.action for row in session.query(AuditLog).order_by(AuditLog.id).all()]


# ═══════════ 同事务语义 ═══════════


def test_audit_rolls_back_with_business_change(
    session_factory: sessionmaker[Session], resources: dict[str, Any]
) -> None:
    """审计与业务同事务：回滚时审计行一并消失（不会留下"做过但没做成"的痕迹）。"""
    with session_factory() as session:
        session.add(
            Asset(
                environment_id=resources["environment_id"],
                name="回滚资产",
                asset_type="host",
                identifier="rollback-1",
            )
        )
        write_audit(session, action="probe.rollback", resource_type="asset", resource_id="x")
        session.rollback()
    with session_factory() as session:
        assert session.query(AuditLog).filter(AuditLog.action == "probe.rollback").count() == 0


def test_audit_commits_with_business_change(
    session_factory: sessionmaker[Session], resources: dict[str, Any]
) -> None:
    """审计与业务同事务：提交时两者同时可见（审计与状态永不脱节）。"""
    with session_factory() as session:
        session.add(
            Asset(
                environment_id=resources["environment_id"],
                name="提交资产",
                asset_type="host",
                identifier="commit-1",
            )
        )
        write_audit(session, action="probe.commit", resource_type="asset", resource_id="y")
        session.commit()
    with session_factory() as session:
        assert session.query(AuditLog).filter(AuditLog.action == "probe.commit").count() == 1


def test_write_audit_stringifies_resource_id(session_factory: sessionmaker[Session]) -> None:
    """resource_id 以字符串落库（兼容不同主键形态）。"""
    with session_factory() as session:
        entry = write_audit(session, action="x.y", resource_type="asset", resource_id=42)  # type: ignore[arg-type]
        session.commit()
    assert entry.resource_id == "42"


def test_write_audit_keeps_username_snapshot(session_factory: sessionmaker[Session]) -> None:
    """username 为冗余快照：用户停用后审计仍可读。"""
    with session_factory() as session:
        entry = write_audit(session, action="x.y", username="ghost")
        session.commit()
    assert (entry.username, entry.user_id) == ("ghost", None)


# ═══════════ 各写端点留痕（矩阵） ═══════════

WRITE_CASES: list[tuple[str, str, str, str]] = [
    ("POST", "/api/v1/assets/business-lines", "business_line", "asset.business_line.create"),
    ("POST", "/api/v1/assets/environments", "environment", "asset.environment.create"),
    ("POST", "/api/v1/assets", "asset", "asset.create"),
    ("POST", "/api/v1/events", "event", "event.create"),
    ("POST", "/api/v1/events/{event_id}/comments", "comment", "event.comment"),
    ("POST", "/api/v1/events/{event_id}/acknowledge", "none", "event.transition"),
    ("POST", "/api/v1/events/{event_id}/diagnose", "none", "diagnosis.trigger"),
    ("POST", "/api/v1/admin/users", "user", "admin.user.create"),
    ("POST", "/api/v1/admin/alert-rules", "alert_rule", "admin.alert_rule.create"),
]


def _body(kind: str, resources: dict[str, Any]) -> dict[str, Any] | None:
    """按种类生成唯一化请求体。"""
    uid = uuid4().hex[:8]
    builders: dict[str, dict[str, Any]] = {
        "business_line": {"code": f"bl-{uid}", "name": f"线-{uid}"},
        "environment": {"business_line_id": resources["business_line_id"], "name": f"env-{uid}"},
        "asset": {
            "environment_id": resources["environment_id"],
            "name": f"a-{uid}",
            "asset_type": "host",
            "identifier": f"host-{uid}",
        },
        "event": {"title": f"事件-{uid}"},
        "comment": {"content": f"评论-{uid}"},
        "user": {"username": f"u{uid}", "password": "User-Test-Pw-1", "role_code": "viewer"},
        "alert_rule": {
            "name": f"规则-{uid}",
            "match_field": "title",
            "match_op": "contains",
            "match_value": "x",
            "severity": "minor",
        },
        "none": None,
    }
    return builders[kind]


@pytest.mark.parametrize("method,path_template,body_kind,expected_action", WRITE_CASES)
def test_write_endpoint_leaves_audit_trail(
    method: str,
    path_template: str,
    body_kind: str,
    expected_action: str,
    client: TestClient,
    headers: dict[str, dict[str, str]],
    resources: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    """每个平台写操作都必须留痕（审计矩阵）。"""
    response = client.request(
        method,
        path_template.format(**resources),
        headers=headers["admin"],
        json=_body(body_kind, resources),
    )
    assert response.status_code < 400, response.text
    with session_factory() as session:
        assert session.query(AuditLog).filter(AuditLog.action == expected_action).count() >= 1


# ═══════════ 审计查询端点 ═══════════


def test_audit_logs_payload_is_camel_case(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """审计出参 camelCase（前端 TS 类型一一对应）。"""
    body = client.get("/api/v1/admin/audit-logs", headers=headers["admin"]).json()
    assert set(body["items"][0]) >= {"resourceType", "resourceId", "createdAt"}


def test_audit_logs_total_is_int(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """分页字段类型正确。"""
    body = client.get("/api/v1/admin/audit-logs", headers=headers["admin"]).json()
    assert isinstance(body["total"], int)


def test_audit_logs_filters_by_action(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """按动作码过滤。"""
    body = client.get(
        "/api/v1/admin/audit-logs?action=asset.create", headers=headers["admin"]
    ).json()
    assert {item["action"] for item in body["items"]} == {"asset.create"}


def test_audit_logs_filters_by_username(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """按操作人过滤（审计追责的基本入口）。"""
    body = client.get(
        "/api/v1/admin/audit-logs?username=admin", headers=headers["admin"]
    ).json()
    assert all(item["username"] == "admin" for item in body["items"])


def test_audit_logs_filters_by_resource_type(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """按资源类型过滤。"""
    body = client.get(
        "/api/v1/admin/audit-logs?resource_type=business_line", headers=headers["admin"]
    ).json()
    assert all(item["resourceType"] == "business_line" for item in body["items"])


def test_audit_logs_are_newest_first(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """审计按时间倒序（最近操作优先展示）。"""
    with session_factory() as session:
        session.add_all(
            [
                AuditLog(
                    action="probe.old", username="u",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
                AuditLog(
                    action="probe.new", username="u",
                    created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()
    body = client.get("/api/v1/admin/audit-logs", headers=headers["admin"]).json()
    assert [item["action"] for item in body["items"]][:2] == ["probe.new", "probe.old"]


def test_audit_logs_pagination(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """分页：page_size 限制条数，total 为全量。"""
    body = client.get(
        "/api/v1/admin/audit-logs?page=1&page_size=2", headers=headers["admin"]
    ).json()
    assert (len(body["items"]), body["total"] >= 2) == (2, True)


def test_audit_logs_requires_admin(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """审计查询仅 admin（业务数据之外的安全域）。"""
    assert client.get("/api/v1/admin/audit-logs", headers=headers["viewer"]).status_code == 403


def test_audit_logs_empty_shape(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """空库返回空数组与 0（不 500）。"""
    body = client.get("/api/v1/admin/audit-logs", headers=headers["admin"]).json()
    assert (body["items"], body["total"]) == ([], 0)
