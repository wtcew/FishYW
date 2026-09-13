"""平台测试包（tests/platform/）。

夹具与用例遵循后端设计文档 §7：目录级 conftest（不新建全局 tests/conftest.py），
运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform

硬约束（对应用户的零磁盘/零副作用要求）：

* 平台库一律落在 ``tmp_path`` 下的 SQLite；``PLATFORM_DB`` 被改写、
  MySQL 四要素被清空——**绝不**连接真实 MySQL，**绝不**触碰
  ``D:\\xingzhi-platform\\platform.db``；
* 真实模型永不加载：``AppState``/agent 图用假替身，``_init_components`` 被
  替身接管，``run_agent`` 逐用例 mock（2.2GB 权重不进测试进程）；
* 平台库惰性初始化的模块级全局状态逐用例重置（``reset_for_tests``），
  避免用例间顺序依赖。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.api import routes
from src.platform import db as platform_db
from src.platform.db import get_db
from src.platform.models import DiagnosisRecord, Event, Role, User
from src.platform.security import create_access_token, hash_password
from src.settings import get_settings
from tests.platform.constants import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    JWT_TEST_SECRET,
    OPERATOR_PASSWORD,
    OPERATOR_USERNAME,
    VIEWER_PASSWORD,
    VIEWER_USERNAME,
    WEBHOOK_TOKENS,
)

__all__ = [
    "ADMIN_PASSWORD",
    "OPERATOR_PASSWORD",
    "VIEWER_PASSWORD",
    "ADMIN_USERNAME",
    "OPERATOR_USERNAME",
    "VIEWER_USERNAME",
]


@pytest.fixture(autouse=True, scope="session")
def _fast_bcrypt() -> Iterator[None]:
    """把 bcrypt 轮次降到 4：种子与登录用例不必付 ×12 的 CPU 成本。"""
    import src.platform.security as security

    original = security._BCRYPT_ROUNDS
    security._BCRYPT_ROUNDS = 4
    yield
    security._BCRYPT_ROUNDS = original


@pytest.fixture()
def platform_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """把平台配置指向 tmp_path 的 SQLite，并清空 MySQL 四要素。

    Yields:
        被改写的平台配置单例（用例内可继续 monkeypatch）。
    """
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setattr(settings, "MYSQL_HOST", "")
    monkeypatch.setattr(settings, "MYSQL_USER", "")
    monkeypatch.setattr(settings, "MYSQL_PASSWORD", "")
    monkeypatch.setattr(settings, "MYSQL_DB", "")
    monkeypatch.setattr(settings, "JWT_SECRET", JWT_TEST_SECRET)
    monkeypatch.setattr(settings, "PLATFORM_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "WEBHOOK_TOKENS", WEBHOOK_TOKENS)
    monkeypatch.setattr(settings, "PLATFORM_ALLOW_REGISTRATION", False)
    monkeypatch.setattr(settings, "PLATFORM_REGISTRATION_AUTO_ACTIVE", False)
    monkeypatch.setattr(settings, "DIAGNOSIS_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(settings, "AUTO_DIAGNOSIS_ENABLED", True)
    platform_db.reset_for_tests()
    yield settings
    platform_db.reset_for_tests()
    get_settings.cache_clear()


@pytest.fixture()
def engine(platform_settings: Any) -> Iterator[Engine]:
    """tmp_path 上的平台引擎（走真实惰性初始化 + 建表 + 种子路径）。"""
    eng = platform_db.get_engine()
    platform_db.init_platform_db()
    yield eng
    eng.dispose()
    platform_db.reset_for_tests()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """测试会话工厂（与路由依赖同一个引擎/文件）。"""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def platform_db_path(platform_settings: Any) -> Path:
    """当前用例的平台库文件路径。"""
    return Path(platform_settings.PLATFORM_DB)


@pytest.fixture()
def seeded(session_factory: sessionmaker[Session]) -> dict[str, int]:
    """在内置种子（admin + 三角色）之上追加 operator/viewer 账号。

    Returns:
        ``{"admin": id, "operator": id, "viewer": id}``。
    """
    ids: dict[str, int] = {}
    with session_factory() as session:
        admin = session.query(User).filter_by(username=ADMIN_USERNAME).one()
        ids["admin"] = admin.id
        for username, password, role_code in (
            (OPERATOR_USERNAME, OPERATOR_PASSWORD, "operator"),
            (VIEWER_USERNAME, VIEWER_PASSWORD, "viewer"),
        ):
            role = session.query(Role).filter_by(code=role_code).one()
            user = User(
                username=username,
                password_hash=hash_password(password),
                display_name=f"{role_code}-显示名",
                role_id=role.id,
            )
            session.add(user)
            session.flush()
            ids[role_code] = user.id
        session.commit()
    return ids


@pytest.fixture()
def tokens(seeded: dict[str, int], session_factory: sessionmaker[Session]) -> dict[str, str]:
    """角色 → 已签发 JWT（直接签发，省去登录往返）。"""
    tokens: dict[str, str] = {}
    with session_factory() as session:
        for key, username in (
            ("admin", ADMIN_USERNAME),
            ("operator", OPERATOR_USERNAME),
            ("viewer", VIEWER_USERNAME),
        ):
            user = session.query(User).filter_by(username=username).one()
            tokens[key] = create_access_token(user.id, user.username, user.role.code)[0]
    return tokens


@pytest.fixture()
def headers(tokens: dict[str, str]) -> dict[str, dict[str, str]]:
    """角色 → Authorization 请求头。"""
    return {role: {"Authorization": f"Bearer {token}"} for role, token in tokens.items()}


@pytest.fixture()
def fake_agent() -> Any:
    """假 AppState 的 agent 图替身（不加载任何真实模型/权重）。"""
    return SimpleNamespace(agent_graph=SimpleNamespace(name="fake-graph"), ready=True)


@pytest.fixture()
def client(
    engine: Engine,
    session_factory: sessionmaker[Session],
    fake_agent: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """注入替身组件的测试客户端。

    平台会话经 ``dependency_overrides`` 指向测试 session factory；后台研判
    用的 ``get_session_factory`` 也被指向同一工厂（绝不让后台任务碰到
    MySQL / D 盘真实库）。RAG 组件由 ``_init_components`` 替身接管。
    """
    from src.api.platform import events as events_router

    def _override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    async def fake_init(state: Any) -> None:
        state.agent_graph = fake_agent.agent_graph
        state.ready = True

    routes.app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setattr(events_router, "get_session_factory", lambda: session_factory)
    try:
        with monkeypatch.context() as patch_ctx:
            patch_ctx.setattr(routes, "_init_components", fake_init)
            with TestClient(routes.app) as test_client:
                yield test_client
    finally:
        routes.app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def fake_run_agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """把所有用例的 ``run_agent`` 换成进程内假替身（绝不打真实 LLM）。

    用例可用自己的 monkeypatch 覆盖为慢速/抛错/特定答案版本（后挂载者生效）。
    """
    calls: list[tuple[str, Any]] = []

    async def _fake(
        query: str, graph: Any, history: Any = None, on_node: Any = None
    ) -> dict[str, Any]:
        calls.append((query, graph))
        return {
            "answer": "根因：磁盘写满\n建议：清理 /var/log 并扩容\n置信度 0.8",
            "sources": [{"chunk_id": "chunk-1", "filename": "ops.md", "content": "磁盘处置手册"}],
            "reflection": {"sufficient": True},
            "iterations": 2,
            "trace_id": "trace-fake-1",
        }

    monkeypatch.setattr("src.agent.state_machine.run_agent", _fake)
    yield {"calls": calls, "fake": _fake}


@pytest.fixture()
def resources(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    session_factory: sessionmaker[Session],
    seeded: dict[str, int],
) -> dict[str, Any]:
    """建好一组可写资源，供参数化矩阵格式化 URL 使用。

    Returns:
        含 ``business_line_id / environment_id / asset_id / asset_identifier /
        deletable_asset_id / event_id / resolvable_event_id / alert_rule_id /
        diagnosis_id / operator_id / operator_username`` 的字典。
    """
    admin = headers["admin"]
    line = client.post(
        "/api/v1/assets/business-lines",
        headers=admin,
        json={"code": "bl-res", "name": "资源线", "description": "夹具"},
    ).json()
    env = client.post(
        "/api/v1/assets/environments",
        headers=admin,
        json={"business_line_id": line["id"], "name": "prod"},
    ).json()
    asset = client.post(
        "/api/v1/assets",
        headers=admin,
        json={
            "environment_id": env["id"],
            "name": "web-01",
            "asset_type": "host",
            "identifier": "10.1.1.1",
        },
    ).json()
    throwaway = client.post(
        "/api/v1/assets",
        headers=admin,
        json={
            "environment_id": env["id"],
            "name": "throwaway",
            "asset_type": "host",
            "identifier": "10.1.1.9",
        },
    ).json()
    rule = client.post(
        "/api/v1/admin/alert-rules",
        headers=admin,
        json={
            "name": "夹具规则",
            "match_field": "title",
            "match_op": "contains",
            "match_value": "夹具",
            "severity": "minor",
        },
    ).json()
    event = client.post(
        "/api/v1/events",
        headers=headers["operator"],
        json={"title": "资源夹具事件", "severity": "major", "asset_id": asset["id"]},
    ).json()
    resolvable = client.post(
        "/api/v1/events",
        headers=headers["operator"],
        json={"title": "可直接 resolve 的事件", "severity": "minor"},
    ).json()
    with session_factory() as session:
        pending = session.get(Event, resolvable["id"])
        pending.status = "acknowledged"
        record = DiagnosisRecord(
            event_id=event["id"], status="completed", trigger_type="manual", query="q",
            root_cause="根部", suggestion="建议", confidence=0.7,
        )
        session.add(record)
        session.commit()
        diagnosis_id = record.id
    return {
        "business_line_id": line["id"],
        "environment_id": env["id"],
        "asset_id": asset["id"],
        "asset_identifier": "10.1.1.1",
        "deletable_asset_id": throwaway["id"],
        "event_id": event["id"],
        "resolvable_event_id": resolvable["id"],
        "alert_rule_id": rule["id"],
        "diagnosis_id": diagnosis_id,
        "operator_id": seeded["operator"],
        "operator_username": OPERATOR_USERNAME,
    }


@pytest.fixture()
def set_event_status(session_factory: sessionmaker[Session]):
    """把事件状态直接改到指定值（布置用例前置态；状态机本身由服务层驱动）。"""

    def _set(event_id: int, status: str) -> None:
        with session_factory() as session:
            event = session.get(Event, event_id)
            assert event is not None
            event.status = status
            session.commit()

    return _set


@pytest.fixture()
def make_event(client: TestClient, headers: dict[str, dict[str, str]]):
    """经 HTTP 手工建事件，返回事件 ID。"""
    counter = {"n": 0}

    def _make(*, title: str = "手工事件", severity: str = "major", asset_id: int | None = None) -> int:
        counter["n"] += 1
        body: dict[str, Any] = {"title": f"{title}-{counter['n']}", "severity": severity}
        if asset_id is not None:
            body["asset_id"] = asset_id
        response = client.post("/api/v1/events", headers=headers["operator"], json=body)
        assert response.status_code == 201, response.text
        return int(response.json()["id"])

    return _make


def read_event(session_factory: sessionmaker[Session], event_id: int) -> Event:
    """按 ID 读取事件（用例断言服务层副作用用）。"""
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        session.expunge(event)
        return event


def read_diagnosis(
    session_factory: sessionmaker[Session], diagnosis_id: int
) -> DiagnosisRecord:
    """按 ID 读取研判记录。"""
    with session_factory() as session:
        record = session.get(DiagnosisRecord, diagnosis_id)
        assert record is not None
        session.expunge(record)
        return record
