"""认证测试：登录成败、JWT 校验、改密、自助注册与停用即时生效。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_auth.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import AuditLog, Role, User
from src.platform.security import hash_password
from tests.platform.constants import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    OPERATOR_PASSWORD,
    OPERATOR_USERNAME,
    VIEWER_USERNAME,
)


def _auth(token: str) -> dict[str, str]:
    """把原始 JWT 包成 Authorization 头。"""
    return {"Authorization": f"Bearer {token}"}


# ═══════════ 登录 ═══════════


def test_login_returns_bearer_token_and_profile(client: TestClient, seeded: dict[str, int]) -> None:
    """登录成功返回 JWT 与用户档案。"""
    response = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    body = response.json()
    assert (response.status_code, body["token_type"]) == (200, "bearer")


def test_login_payload_is_snake_case(client: TestClient, seeded: dict[str, int]) -> None:
    """登录出参键名（现状契约：UserOut 未做 camelCase 别名）。

    Note:
        与后端设计文档「出参字段统一 camelCase 别名」不符——资产/事件出参是
        camelCase，而 auth/admin 的用户出参是 snake_case（role_code 等）。
        前端若按 roleCode 读取会拿到 undefined。此处锁住现状契约，
        偏差已在交付报告中标为待修项。
    """
    body = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).json()
    assert set(body["user"]) >= {"role_code", "display_name", "is_active"}


def test_login_returns_positive_expires_in(client: TestClient, seeded: dict[str, int]) -> None:
    """expires_in 为正数（前端据此安排续期）。"""
    response = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.json()["expires_in"] > 0


def test_login_updates_last_login_at(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """登录成功后写入 last_login_at。"""
    client.post("/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    with session_factory() as session:
        admin = session.query(User).filter_by(username=ADMIN_USERNAME).one()
    assert admin.last_login_at is not None


def test_login_rejects_wrong_password(client: TestClient, seeded: dict[str, int]) -> None:
    """密码错误 401。"""
    response = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": "wrong-password"}
    )
    assert response.status_code == 401


def test_login_rejects_unknown_user(client: TestClient, seeded: dict[str, int]) -> None:
    """账号不存在 401（不泄露账号是否存在）。"""
    response = client.post(
        "/api/v1/auth/login", json={"username": "ghost", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 401


def test_login_rejects_inactive_user(
    client: TestClient, seeded: dict[str, int], session_factory: sessionmaker[Session]
) -> None:
    """停用账号即使密码正确也 401。"""
    with session_factory() as session:
        viewer = session.query(User).filter_by(username=VIEWER_USERNAME).one()
        viewer.is_active = False
        session.commit()
    response = client.post(
        "/api/v1/auth/login", json={"username": VIEWER_USERNAME, "password": "Viewer-Test-Pw-03"}
    )
    assert response.status_code == 401


def test_login_rejects_missing_password(client: TestClient) -> None:
    """缺少密码字段被 pydantic 拦截为 422。"""
    assert client.post("/api/v1/auth/login", json={"username": "admin"}).status_code == 422


# ═══════════ 登录审计（成败均留痕） ═══════════


def test_successful_login_writes_audit_with_user_id(
    client: TestClient, seeded: dict[str, int], session_factory: sessionmaker[Session]
) -> None:
    """成功登录写 user.login 审计，user_id 指向登录者。"""
    client.post("/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "user.login", AuditLog.username == ADMIN_USERNAME)
            .one()
        )
    assert (row.user_id is not None, row.detail["result"]) == (True, "success")


def test_failed_login_writes_audit_without_user_id(
    client: TestClient, seeded: dict[str, int], session_factory: sessionmaker[Session]
) -> None:
    """失败登录写审计：user_id 为空、username 记尝试值（匿名失败可追溯）。"""
    client.post("/api/v1/auth/login", json={"username": ADMIN_USERNAME, "password": "nope"})
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "user.login", AuditLog.username == ADMIN_USERNAME)
            .one()
        )
    assert (row.user_id, row.detail["result"]) == (None, "failed")


def test_failed_login_of_unknown_user_records_attempted_name(
    client: TestClient, seeded: dict[str, int], session_factory: sessionmaker[Session]
) -> None:
    """未知账号的失败也留痕（记尝试的用户名）。"""
    client.post("/api/v1/auth/login", json={"username": "ghost", "password": "nope"})
    with session_factory() as session:
        assert session.query(AuditLog).filter(AuditLog.username == "ghost").count() == 1


def test_login_audit_records_forwarded_ip(
    client: TestClient, seeded: dict[str, int], session_factory: sessionmaker[Session]
) -> None:
    """审计记录 X-Forwarded-For 的第一个地址。"""
    client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "user.login", AuditLog.detail["result"].as_string() == "success")
            .one()
        )
    assert row.ip == "203.0.113.9"


# ═══════════ /auth/me 与 JWT 校验 ═══════════


def test_me_returns_profile(client: TestClient, headers: dict[str, dict[str, str]]) -> None:
    """带令牌返回当前用户档案。"""
    response = client.get("/api/v1/auth/me", headers=headers["operator"])
    assert (response.status_code, response.json()["username"]) == (200, OPERATOR_USERNAME)


def test_me_rejects_missing_token(client: TestClient) -> None:
    """无令牌 401。"""
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_rejects_malformed_token(client: TestClient) -> None:
    """畸形令牌 401。"""
    assert client.get("/api/v1/auth/me", headers=_auth("not-a-jwt")).status_code == 401


def test_me_rejects_token_signed_with_other_secret(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """签名不匹配的令牌 401（防伪造）。"""
    forged = pyjwt.encode(
        {"sub": str(seeded["admin"]), "username": "admin", "role": "admin",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "another-secret-that-is-long-enough-0123",
        algorithm="HS256",
    )
    assert client.get("/api/v1/auth/me", headers=_auth(forged)).status_code == 401


def test_me_rejects_expired_token(
    client: TestClient, seeded: dict[str, int], platform_settings: Any
) -> None:
    """过期令牌 401。"""
    expired = pyjwt.encode(
        {"sub": str(seeded["admin"]), "username": "admin", "role": "admin",
         "iat": datetime.now(timezone.utc) - timedelta(hours=2),
         "exp": datetime.now(timezone.utc) - timedelta(hours=1)},
        platform_settings.JWT_SECRET,
        algorithm="HS256",
    )
    assert client.get("/api/v1/auth/me", headers=_auth(expired)).status_code == 401


def test_me_rejects_token_of_deactivated_user(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """回库校验：令牌未过期但账号被停用 → 401（停用即时生效）。"""
    with session_factory() as session:
        viewer = session.query(User).filter_by(username=VIEWER_USERNAME).one()
        viewer.is_active = False
        session.commit()
    assert client.get("/api/v1/auth/me", headers=headers["viewer"]).status_code == 401


def test_me_rejects_token_of_deleted_user(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """回库校验：账号被删除 → 401。"""
    with session_factory() as session:
        viewer = session.query(User).filter_by(username=VIEWER_USERNAME).one()
        session.delete(viewer)
        session.commit()
    assert client.get("/api/v1/auth/me", headers=headers["viewer"]).status_code == 401


def test_role_change_takes_effect_immediately(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    session_factory: sessionmaker[Session],
) -> None:
    """回库校验：令牌内 role 不作判定依据，改角色后同一令牌权限立即变化。"""
    with session_factory() as session:
        viewer = session.query(User).filter_by(username=VIEWER_USERNAME).one()
        viewer.role_id = session.query(Role).filter_by(code="admin").one().id
        session.commit()
    assert client.get("/api/v1/admin/users", headers=headers["viewer"]).status_code == 200


# ═══════════ 改密 ═══════════


def test_change_password_rejects_wrong_old_password(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """旧密码错误 400。"""
    response = client.post(
        "/api/v1/auth/change-password",
        headers=headers["viewer"],
        json={"old_password": "not-the-password", "new_password": "Brand-New-Pw-1"},
    )
    assert response.status_code == 400


def test_change_password_allows_login_with_new_password(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """改密后新密码可登录。"""
    client.post(
        "/api/v1/auth/change-password",
        headers=headers["viewer"],
        json={"old_password": "Viewer-Test-Pw-03", "new_password": "Brand-New-Pw-1"},
    )
    response = client.post(
        "/api/v1/auth/login", json={"username": VIEWER_USERNAME, "password": "Brand-New-Pw-1"}
    )
    assert response.status_code == 200


def test_change_password_invalidates_old_password(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """改密后旧密码失效。"""
    client.post(
        "/api/v1/auth/change-password",
        headers=headers["viewer"],
        json={"old_password": "Viewer-Test-Pw-03", "new_password": "Brand-New-Pw-1"},
    )
    response = client.post(
        "/api/v1/auth/login", json={"username": VIEWER_USERNAME, "password": "Viewer-Test-Pw-03"}
    )
    assert response.status_code == 401


def test_change_password_requires_login(client: TestClient) -> None:
    """未登录不能改密。"""
    response = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "a", "new_password": "Brand-New-Pw-1"},
    )
    assert response.status_code == 401


def test_change_password_rejects_short_new_password(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """新密码短于 8 位被 pydantic 拦截。"""
    response = client.post(
        "/api/v1/auth/change-password",
        headers=headers["viewer"],
        json={"old_password": "Viewer-Test-Pw-03", "new_password": "short"},
    )
    assert response.status_code == 422


def test_change_password_writes_audit(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """改密写 user.change_password 审计。"""
    client.post(
        "/api/v1/auth/change-password",
        headers=headers["viewer"],
        json={"old_password": "Viewer-Test-Pw-03", "new_password": "Brand-New-Pw-1"},
    )
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "user.change_password", AuditLog.username == VIEWER_USERNAME)
            .one()
        )
    assert row.user_id is not None


# ═══════════ 注册状态预检（公开端点，Q 裁决 2026-09-13） ═══════════


def test_registration_status_is_public_and_open_by_default(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无登录可访问，且默认报告开放注册（前端登录页据此显示注册入口）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    response = client.get("/api/v1/auth/registration-status")
    assert (response.status_code, response.json()) == (
        200, {"allow_registration": True, "auto_active": False},
    )


def test_registration_status_reports_closed_when_disabled(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式关闭时报告 allow_registration=False（前端隐藏注册入口的唯一依据）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", False)
    body = client.get("/api/v1/auth/registration-status").json()
    assert (body["allow_registration"], body["auto_active"]) == (False, False)


def test_registration_status_exposes_auto_active(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto_active 开启时透出（前端提示"注册后可直接登录"）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    monkeypatch.setattr(platform_settings, "PLATFORM_REGISTRATION_AUTO_ACTIVE", True)
    body = client.get("/api/v1/auth/registration-status").json()
    assert body["auto_active"] is True


# ═══════════ 自助注册（PLATFORM_ALLOW_REGISTRATION 开关） ═══════════


def test_register_is_rejected_when_disabled(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式关闭注册开关：403 且提示联系管理员。

    Note:
        代码默认值已改为开放注册（``PLATFORM_ALLOW_REGISTRATION=True``，
        见 test_settings 默认值用例）；测试夹具保守地置 False，这里再显式
        置 False 一次，锁定"关闭 → 403"的契约不依赖夹具默认。
    """
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", False)
    response = client.post(
        "/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"}
    )
    assert (response.status_code, response.json()["detail"]) == (
        403, "注册暂未开放，请联系管理员开通账号",
    )


def test_registration_is_open_by_code_default() -> None:
    """代码默认开放注册（Settings 类默认值，不受部署 .env 影响）。"""
    from src.settings import Settings

    defaults = Settings(_env_file=None)
    assert (
        defaults.PLATFORM_ALLOW_REGISTRATION,
        defaults.PLATFORM_REGISTRATION_AUTO_ACTIVE,
    ) == (True, False)


def test_register_succeeds_with_default_open_registration(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认开放的注册语义：201 + pending（注册 ≠ 立即可用）。

    Note:
        夹具保守置 False，这里显式还原代码默认（True）后走 HTTP 层，
        pending 语义依赖 ``PLATFORM_REGISTRATION_AUTO_ACTIVE`` 默认 False。
    """
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    response = client.post(
        "/api/v1/auth/register", json={"username": "default-open", "password": "Newbie-Pw-123"}
    )
    assert (response.status_code, response.json()["status"]) == (201, "pending")


def test_register_creates_pending_viewer_when_enabled(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关开启且未自动启用时：201 + status=pending。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    response = client.post(
        "/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"}
    )
    assert (response.status_code, response.json()["status"]) == (201, "pending")


def test_register_assigns_viewer_role(
    client: TestClient,
    platform_settings: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册账号一律只读角色（不得自助提权）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    client.post("/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"})
    with session_factory() as session:
        user = session.query(User).filter_by(username="newbie").one()
        role_code = user.role.code
    assert role_code == "viewer"


def test_registered_pending_user_cannot_login(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """待启用账号登录 401（注册 ≠ 立即可用）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    client.post("/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"})
    response = client.post(
        "/api/v1/auth/login", json={"username": "newbie", "password": "Newbie-Pw-123"}
    )
    assert response.status_code == 401


def test_register_auto_active_user_can_login(
    client: TestClient,
    platform_settings: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关 + AUTO_ACTIVE 全开时注册即可登录（真实可用路径）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    monkeypatch.setattr(platform_settings, "PLATFORM_REGISTRATION_AUTO_ACTIVE", True)
    client.post("/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"})
    response = client.post(
        "/api/v1/auth/login", json={"username": "newbie", "password": "Newbie-Pw-123"}
    )
    assert response.status_code == 200


def test_register_rejects_duplicate_username(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户名重复 409。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    response = client.post(
        "/api/v1/auth/register", json={"username": ADMIN_USERNAME, "password": "Newbie-Pw-123"}
    )
    assert response.status_code == 409


def test_register_writes_audit(
    client: TestClient,
    platform_settings: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册写 user.register 审计。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    client.post("/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"})
    with session_factory() as session:
        row = session.query(AuditLog).filter(AuditLog.action == "user.register").one()
    assert row.username == "newbie"


def test_register_hashes_password(
    client: TestClient,
    platform_settings: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册密码 bcrypt 落库（明文不入库）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    client.post("/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"})
    with session_factory() as session:
        user = session.query(User).filter_by(username="newbie").one()
    assert user.password_hash != "Newbie-Pw-123"


def test_register_rejects_short_password(
    client: TestClient, platform_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """密码短于 8 位被 pydantic 拦截。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    response = client.post(
        "/api/v1/auth/register", json={"username": "newbie", "password": "short"}
    )
    assert response.status_code == 422


def test_register_hash_matches_actual_password(
    client: TestClient,
    platform_settings: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册密码可被真实校验（用 hash_password 的对照验证）。"""
    from src.platform.security import verify_password

    monkeypatch.setattr(platform_settings, "PLATFORM_ALLOW_REGISTRATION", True)
    client.post("/api/v1/auth/register", json={"username": "newbie", "password": "Newbie-Pw-123"})
    with session_factory() as session:
        user = session.query(User).filter_by(username="newbie").one()
    assert verify_password("Newbie-Pw-123", user.password_hash) is True


def test_operator_password_fixture_matches_hash(
    seeded: dict[str, int], session_factory: sessionmaker[Session]
) -> None:
    """夹具账号的密码哈希真实可用（避免种子写错导致后续用例假绿）。"""
    from src.platform.security import verify_password

    with session_factory() as session:
        operator = session.query(User).filter_by(username=OPERATOR_USERNAME).one()
    assert verify_password(OPERATOR_PASSWORD, operator.password_hash) is True


def test_hash_password_is_salted(session_factory: sessionmaker[Session]) -> None:
    """bcrypt 加盐：同一明文两次哈希不同（防彩虹表）。"""
    assert hash_password("same-secret") != hash_password("same-secret")
