"""RBAC 权限矩阵测试：端点 × 角色 的允许/拒绝。

矩阵来源：后端设计文档 §4.2 角色矩阵（能力矩阵语义：viewer+=读、operator+=读写、
admin=全部）。`require_roles` 已改为按 ROLE_MATRIX 做能力子集判定，
因此 operator 对只读端点的 200 是**正式断言**，不再是已知偏差。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_rbac.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import AuditLog, Role, User
from tests.platform.constants import (
    ADMIN_USERNAME,
    OPERATOR_USERNAME,
)


def _body(kind: str | None, resources: dict[str, Any]) -> dict[str, Any] | None:
    """按“载荷种类”生成唯一化请求体（避免跨用例唯一约束冲突）。"""
    uid = uuid4().hex[:8]
    builders: dict[str, dict[str, Any]] = {
        "business_line": {"code": f"bl-{uid}", "name": f"业务线-{uid}"},
        "environment": {
            "business_line_id": resources["business_line_id"],
            "name": f"env-{uid}",
        },
        "asset": {
            "environment_id": resources["environment_id"],
            "name": f"asset-{uid}",
            "asset_type": "host",
            "identifier": f"host-{uid}",
        },
        "asset_update": {
            "environment_id": resources["environment_id"],
            "name": "web-01-renamed",
            "asset_type": "host",
            "identifier": resources["asset_identifier"],
        },
        "event": {"title": f"事件-{uid}", "severity": "major"},
        "comment": {"content": f"评论-{uid}"},
        "alert_rule": {
            "name": f"规则-{uid}",
            "match_field": "title",
            "match_op": "contains",
            "match_value": "磁盘",
            "severity": "major",
        },
        "user": {"username": f"u{uid}", "password": "User-Test-Pw-1", "role_code": "viewer"},
        "user_update": {"display_name": f"改名-{uid}"},
        "business_line_update": {"code": "bl-res", "name": "资源线"},
        "none": None,
    }
    return builders[kind] if kind else None


#: (角色, 方法, 路径模板, 载荷种类, 期望状态码)
MATRIX: list[Any] = [
    # ── CMDB 读（viewer+）──
    ("admin", "GET", "/api/v1/assets/business-lines", "none", 200),
    ("viewer", "GET", "/api/v1/assets/business-lines", "none", 200),
    ("operator", "GET", "/api/v1/assets/business-lines", "none", 200),
    ("admin", "GET", "/api/v1/assets", "none", 200),
    ("viewer", "GET", "/api/v1/assets", "none", 200),
    ("operator", "GET", "/api/v1/assets", "none", 200),
    ("viewer", "GET", "/api/v1/assets/{asset_id}", "none", 200),
    ("viewer", "GET", "/api/v1/assets/environments", "none", 200),
    # ── CMDB 写 ──
    ("admin", "POST", "/api/v1/assets/business-lines", "business_line", 201),
    ("operator", "POST", "/api/v1/assets/business-lines", "business_line", 403),
    ("viewer", "POST", "/api/v1/assets/business-lines", "business_line", 403),
    ("admin", "PUT", "/api/v1/assets/business-lines/{business_line_id}", "business_line_update", 200),
    ("operator", "PUT", "/api/v1/assets/business-lines/{business_line_id}", "business_line_update", 403),
    ("admin", "POST", "/api/v1/assets/environments", "environment", 201),
    ("operator", "POST", "/api/v1/assets/environments", "environment", 403),
    ("operator", "POST", "/api/v1/assets", "asset", 201),
    ("admin", "POST", "/api/v1/assets", "asset", 201),
    ("viewer", "POST", "/api/v1/assets", "asset", 403),
    ("operator", "PUT", "/api/v1/assets/{asset_id}", "asset_update", 200),
    ("viewer", "PUT", "/api/v1/assets/{asset_id}", "asset_update", 403),
    ("admin", "DELETE", "/api/v1/assets/{deletable_asset_id}", "none", 200),
    ("operator", "DELETE", "/api/v1/assets/{deletable_asset_id}", "none", 403),
    ("viewer", "DELETE", "/api/v1/assets/{deletable_asset_id}", "none", 403),
    # ── 事件读 ──
    ("admin", "GET", "/api/v1/events", "none", 200),
    ("viewer", "GET", "/api/v1/events", "none", 200),
    ("operator", "GET", "/api/v1/events", "none", 200),
    ("viewer", "GET", "/api/v1/events/{event_id}", "none", 200),
    ("operator", "GET", "/api/v1/events/{event_id}", "none", 200),
    ("admin", "GET", "/api/v1/events/diagnoses/{diagnosis_id}", "none", 200),
    ("viewer", "GET", "/api/v1/events/diagnoses/{diagnosis_id}", "none", 200),
    ("operator", "GET", "/api/v1/events/diagnoses/{diagnosis_id}", "none", 200),
    # ── 事件写与流转 ──
    ("operator", "POST", "/api/v1/events", "event", 201),
    ("admin", "POST", "/api/v1/events", "event", 201),
    ("viewer", "POST", "/api/v1/events", "event", 403),
    ("operator", "POST", "/api/v1/events/{event_id}/acknowledge", "none", 200),
    ("admin", "POST", "/api/v1/events/{event_id}/acknowledge", "none", 200),
    ("viewer", "POST", "/api/v1/events/{event_id}/acknowledge", "none", 403),
    ("operator", "POST", "/api/v1/events/{resolvable_event_id}/resolve", "none", 200),
    ("viewer", "POST", "/api/v1/events/{resolvable_event_id}/resolve", "none", 403),
    ("operator", "POST", "/api/v1/events/{event_id}/close", "none", 200),
    ("viewer", "POST", "/api/v1/events/{event_id}/close", "none", 403),
    ("operator", "POST", "/api/v1/events/{event_id}/comments", "comment", 201),
    ("viewer", "POST", "/api/v1/events/{event_id}/comments", "comment", 403),
    ("operator", "POST", "/api/v1/events/{event_id}/diagnose", "none", 202),
    ("admin", "POST", "/api/v1/events/{event_id}/diagnose", "none", 202),
    ("viewer", "POST", "/api/v1/events/{event_id}/diagnose", "none", 403),
    # ── 管理域（admin 专属，角色列表除外）──
    ("admin", "GET", "/api/v1/admin/users", "none", 200),
    ("operator", "GET", "/api/v1/admin/users", "none", 403),
    ("viewer", "GET", "/api/v1/admin/users", "none", 403),
    ("viewer", "GET", "/api/v1/admin/roles", "none", 200),
    ("operator", "GET", "/api/v1/admin/roles", "none", 200),
    ("admin", "POST", "/api/v1/admin/users", "user", 201),
    ("operator", "POST", "/api/v1/admin/users", "user", 403),
    ("admin", "PUT", "/api/v1/admin/users/{operator_id}", "user_update", 200),
    ("operator", "PUT", "/api/v1/admin/users/{operator_id}", "user_update", 403),
    ("admin", "POST", "/api/v1/admin/users/{operator_id}/reset-password", "none", 200),
    ("viewer", "POST", "/api/v1/admin/users/{operator_id}/reset-password", "none", 403),
    ("admin", "GET", "/api/v1/admin/audit-logs", "none", 200),
    ("operator", "GET", "/api/v1/admin/audit-logs", "none", 403),
    ("viewer", "GET", "/api/v1/admin/audit-logs", "none", 403),
    ("admin", "GET", "/api/v1/admin/system/health", "none", 200),
    ("operator", "GET", "/api/v1/admin/system/health", "none", 403),
    ("viewer", "GET", "/api/v1/admin/system/health", "none", 403),
    ("admin", "GET", "/api/v1/admin/alert-rules", "none", 200),
    ("viewer", "GET", "/api/v1/admin/alert-rules", "none", 200),
    ("operator", "GET", "/api/v1/admin/alert-rules", "none", 200),
    ("admin", "POST", "/api/v1/admin/alert-rules", "alert_rule", 201),
    ("operator", "POST", "/api/v1/admin/alert-rules", "alert_rule", 403),
    ("admin", "PUT", "/api/v1/admin/alert-rules/{alert_rule_id}", "alert_rule", 200),
    ("operator", "PUT", "/api/v1/admin/alert-rules/{alert_rule_id}", "alert_rule", 403),
    ("admin", "DELETE", "/api/v1/admin/alert-rules/{alert_rule_id}", "none", 200),
    ("viewer", "DELETE", "/api/v1/admin/alert-rules/{alert_rule_id}", "none", 403),
]


@pytest.mark.parametrize(
    "role,method,path_template,body_kind,expected",
    MATRIX,
)
def test_rbac_matrix(
    role: str,
    method: str,
    path_template: str,
    body_kind: str,
    expected: int,
    client: TestClient,
    headers: dict[str, dict[str, str]],
    resources: dict[str, Any],
) -> None:
    """端点 × 角色：允许的放行、越权的 403。"""
    response = client.request(
        method,
        path_template.format(**resources),
        headers=headers[role],
        json=_body(body_kind, resources),
    )
    assert response.status_code == expected, response.text


#: 无令牌访问需统一 401 的端点（含需 ID 的路径）。
NO_TOKEN_CASES = [
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/assets"),
    ("GET", "/api/v1/assets/business-lines"),
    ("GET", "/api/v1/assets/environments"),
    ("GET", "/api/v1/assets/{asset_id}"),
    ("POST", "/api/v1/assets"),
    ("GET", "/api/v1/events"),
    ("GET", "/api/v1/events/{event_id}"),
    ("POST", "/api/v1/events"),
    ("POST", "/api/v1/events/{event_id}/acknowledge"),
    ("GET", "/api/v1/events/diagnoses/{diagnosis_id}"),
    ("GET", "/api/v1/admin/users"),
    ("GET", "/api/v1/admin/roles"),
    ("GET", "/api/v1/admin/audit-logs"),
    ("GET", "/api/v1/admin/system/health"),
    ("GET", "/api/v1/admin/alert-rules"),
]


@pytest.mark.parametrize("method,path_template", NO_TOKEN_CASES)
def test_missing_token_is_401(
    method: str, path_template: str, client: TestClient, resources: dict[str, Any]
) -> None:
    """无令牌一律 401（不泄露资源是否存在）。"""
    response = client.request(method, path_template.format(**resources))
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_webhook_does_not_use_bearer_rbac(
    role: str, client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """webhook 只认 X-Webhook-Token：带普通 Bearer 但缺 token 仍 401。"""
    response = client.post(
        "/api/v1/events/webhook/zabbix", headers=headers[role], json={"title": "x"}
    )
    assert response.status_code == 401


# ═══════════ 管理域边界（自我保护） ═══════════


def test_admin_cannot_deactivate_self(
    client: TestClient, headers: dict[str, dict[str, str]], seeded: dict[str, int]
) -> None:
    """禁止停用自己（避免把自己锁在门外）。"""
    response = client.put(
        f"/api/v1/admin/users/{seeded['admin']}",
        headers=headers["admin"],
        json={"is_active": False},
    )
    assert response.status_code == 400


def test_admin_can_deactivate_other_active_admin(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """停用另一名活跃管理员：操作者仍是活跃 admin，系统未失去管理入口 → 允许。

    2026-09-13 修正：原用例构造的是"目标本身已停用"的场景并期望 400，
    而旧实现恰好因"全体活跃 admin（含操作者）<=1"恒假而没拦住——两边都
    不对。现在保护语义收敛为"除目标外仍有活跃 admin 即放行"。
    """
    with session_factory() as session:
        second = User(
            username="admin2",
            password_hash="x",
            role_id=session.query(Role).filter_by(code="admin").one().id,
            is_active=True,
        )
        session.add(second)
        session.commit()
        second_id = second.id
    response = client.put(
        f"/api/v1/admin/users/{second_id}",
        headers=headers["admin"],
        json={"is_active": False},
    )
    assert response.status_code == 200


def test_admin_cannot_demote_last_admin(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """禁止把最后一个活跃管理员降级为其它角色（原实现完全没检查该方向）。

    构造：库中仅有种子 admin 一个活跃管理员（目标=操作者自己），降级请求
    会抽走系统唯一的。

    注：deactivate 方向的"最后一个管理员"在 API 层不可达——操作者自己必须
    是活跃 admin 才能调用端点，故目标为他人时系统至少还剩操作者。真实防线
    是"不能停用自己"（见 test_admin_cannot_deactivate_self）。
    """
    with session_factory() as session:
        admin_id = session.query(User).filter(User.username == "admin").one().id
    response = client.put(
        f"/api/v1/admin/users/{admin_id}",
        headers=headers["admin"],
        json={"role_code": "viewer"},
    )
    assert response.status_code == 400
    assert "最后一个管理员" in response.json()["detail"]


def test_reset_password_returns_once_only_secret(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """重置密码返回一次性新口令，且该口令能真实登录。"""
    new_password = client.post(
        f"/api/v1/admin/users/{resources['operator_id']}/reset-password",
        headers=headers["admin"],
    ).json()["new_password"]
    response = client.post(
        "/api/v1/auth/login", json={"username": OPERATOR_USERNAME, "password": new_password}
    )
    assert response.status_code == 200


def test_reset_password_writes_audit(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    resources: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    """重置密码写审计（admin.user.reset_password）。"""
    client.post(
        f"/api/v1/admin/users/{resources['operator_id']}/reset-password", headers=headers["admin"]
    )
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "admin.user.reset_password")
            .one()
        )
    assert row.resource_id == str(resources["operator_id"])


def test_create_user_rejects_duplicate_username(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """重复用户名 409。"""
    response = client.post(
        "/api/v1/admin/users",
        headers=headers["admin"],
        json={"username": ADMIN_USERNAME, "password": "Another-Pw-123", "role_code": "viewer"},
    )
    assert response.status_code == 409


def test_create_user_unknown_role_is_rejected(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """未知角色码被 pydantic 拦截（不能造出无权限矩阵的角色）。"""
    response = client.post(
        "/api/v1/admin/users",
        headers=headers["admin"],
        json={"username": "weird", "password": "Another-Pw-123", "role_code": "root"},
    )
    assert response.status_code == 422


def test_created_user_can_login_and_is_viewer_limited(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """管理员建号后该账号可登录，且受 viewer 权限约束。"""
    client.post(
        "/api/v1/admin/users",
        headers=headers["admin"],
        json={"username": "newop", "password": "Another-Pw-123", "role_code": "viewer"},
    )
    token = client.post(
        "/api/v1/auth/login", json={"username": "newop", "password": "Another-Pw-123"}
    ).json()["access_token"]
    response = client.get(
        "/api/v1/admin/audit-logs", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_viewer_can_read_roles_for_dropdown(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """角色列表对登录用户开放（前端下拉需要）。"""
    body = client.get("/api/v1/admin/roles", headers=headers["viewer"]).json()
    assert {role["code"] for role in body} == {"admin", "operator", "viewer"}


def test_operator_token_carries_operator_role(tokens: dict[str, str]) -> None:
    """夹具令牌的角色声明与预期一致（防止常量漂移导致矩阵假绿）。"""
    from src.platform.security import decode_token

    assert decode_token(tokens["operator"])["role"] == "operator"


# ═══════════ require_roles 能力语义（单元级） ═══════════


def _fake_user(role_code: str) -> Any:
    """构造只带角色码的轻量用户替身（依赖只需 role.code）。"""
    return SimpleNamespace(role=SimpleNamespace(code=role_code))


def test_require_roles_viewer_allows_operator() -> None:
    """viewer+ 语义：require_roles('viewer') 放行 operator。"""
    from src.platform.deps import require_roles

    assert require_roles("viewer")(user=_fake_user("operator")).role.code == "operator"


def test_require_roles_viewer_allows_viewer() -> None:
    """viewer+ 语义：require_roles('viewer') 放行 viewer。"""
    from src.platform.deps import require_roles

    assert require_roles("viewer")(user=_fake_user("viewer")).role.code == "viewer"


def test_require_roles_operator_blocks_viewer() -> None:
    """写端点仍必须挡住 viewer（修复不得放宽写权限）。"""
    from fastapi import HTTPException

    from src.platform.deps import require_roles

    with pytest.raises(HTTPException) as excinfo:
        require_roles("operator")(user=_fake_user("viewer"))
    assert excinfo.value.status_code == 403


def test_require_roles_operator_allows_operator() -> None:
    """operator+ 语义：require_roles('operator') 放行 operator。"""
    from src.platform.deps import require_roles

    assert require_roles("operator")(user=_fake_user("operator")).role.code == "operator"


def test_require_roles_admin_blocks_operator() -> None:
    """管理端点仍仅限 admin（operator 不得越权）。"""
    from fastapi import HTTPException

    from src.platform.deps import require_roles

    with pytest.raises(HTTPException) as excinfo:
        require_roles("admin")(user=_fake_user("operator"))
    assert excinfo.value.status_code == 403


def test_require_roles_admin_allows_admin() -> None:
    """超管通过管理端点。"""
    from src.platform.deps import require_roles

    assert require_roles("admin")(user=_fake_user("admin")).role.code == "admin"


def test_require_roles_unknown_role_code_is_denied() -> None:
    """未知角色码不得通过（ROLE_MATRIX 无此能力，安全默认拒绝）。"""
    from fastapi import HTTPException

    from src.platform.deps import require_roles

    with pytest.raises(HTTPException) as excinfo:
        require_roles("viewer")(user=_fake_user("ghost"))
    assert excinfo.value.status_code == 403
