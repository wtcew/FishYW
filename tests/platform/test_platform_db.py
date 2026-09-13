"""平台库初始化测试：惰性、幂等、种子策略与锁回归。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_db.py
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.platform import db as platform_db
from src.platform.models import Role, User
from src.platform.security import verify_password
from tests.platform.constants import ADMIN_PASSWORD


# ═══════════ 惰性初始化与存储兜底 ═══════════


def test_engine_is_not_built_before_first_use(platform_settings: Any, platform_db_path: Path) -> None:
    """引擎惰性：首次取用前不落任何文件（存量测试零副作用的根本保障）。"""
    assert not platform_db_path.exists()


def test_engine_url_is_sqlite_when_mysql_unconfigured(engine: Engine) -> None:
    """MySQL 四要素为空时走 SQLite 兜底（测试绝不连真实 MySQL）。"""
    assert engine.url.drivername == "sqlite"


def test_engine_fallback_flag_is_set(engine: Engine) -> None:
    """兜底标志为真，/admin/system/health 据此报告存储后端。"""
    assert platform_db.using_fallback() is True


def test_engine_file_lives_in_tmp_path(engine: Engine, platform_db_path: Path) -> None:
    """库文件落在 tmp_path，绝不落到 D:\\xingzhi-platform\\platform.db。"""
    assert Path(str(engine.url.database)) == platform_db_path


def test_get_engine_returns_singleton(engine: Engine) -> None:
    """引擎进程内唯一。"""
    assert platform_db.get_engine() is engine


def test_get_db_yields_usable_session(engine: Engine) -> None:
    """get_db 依赖产出的会话可正常查询（惰性初始化已自动完成）。"""
    generator = platform_db.get_db()
    try:
        session: Session = next(generator)
        assert session.query(Role).count() == 3
    finally:
        generator.close()


# ═══════════ 种子策略 ═══════════


def test_init_creates_three_roles(engine: Engine, session_factory: sessionmaker[Session]) -> None:
    """建表后引导 admin/operator/viewer 三个角色。"""
    with session_factory() as session:
        assert {role.code for role in session.query(Role).all()} == {"admin", "operator", "viewer"}


def test_init_creates_single_admin_user(engine: Engine, session_factory: sessionmaker[Session]) -> None:
    """无用户时创建唯一的 admin 账号。"""
    with session_factory() as session:
        assert session.query(User).count() == 1


def test_init_uses_admin_password_from_settings(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """PLATFORM_ADMIN_PASSWORD 提供时直接使用（不随机）。"""
    with session_factory() as session:
        admin = session.query(User).filter_by(username="admin").one()
    assert verify_password(ADMIN_PASSWORD, admin.password_hash) is True


def test_admin_password_hash_is_bcrypt(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """落库的是 bcrypt 哈希，明文绝不入库。"""
    with session_factory() as session:
        admin = session.query(User).filter_by(username="admin").one()
    assert admin.password_hash.startswith("$2b$")


def test_init_is_idempotent(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """重复初始化不重复建表/重复种子。"""
    platform_db.init_platform_db()
    platform_db.init_platform_db()
    with session_factory() as session:
        assert (session.query(Role).count(), session.query(User).count()) == (3, 1)


def test_init_generates_random_password_when_unset(
    platform_settings: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未提供管理员密码时随机生成，且打印出来的口令能通过库内哈希校验。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ADMIN_PASSWORD", "")
    platform_db.reset_for_tests()
    with caplog.at_level(logging.WARNING, logger="src.platform.db"):
        platform_db.init_platform_db()
    printed = [record.args[1] for record in caplog.records if record.args and len(record.args) >= 2]
    with platform_db.get_session_factory()() as session:
        admin = session.query(User).filter_by(username="admin").one()
    assert verify_password(printed[-1], admin.password_hash) is True


def test_random_password_is_long_enough(
    platform_settings: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """随机口令长度 >= 16（token_urlsafe(12) 的熵不因黑盒而缩水）。"""
    monkeypatch.setattr(platform_settings, "PLATFORM_ADMIN_PASSWORD", "")
    platform_db.reset_for_tests()
    with caplog.at_level(logging.WARNING, logger="src.platform.db"):
        platform_db.init_platform_db()
    printed = [record.args[1] for record in caplog.records if record.args and len(record.args) >= 2]
    assert len(printed[-1]) >= 16


# ═══════════ 锁回归（P0：非重入锁自死锁） ═══════════


def test_init_platform_db_does_not_deadlock(platform_settings: Any) -> None:
    """回归：init_platform_db 在持锁状态下调用 get_engine——非重入锁会永久挂起。"""
    finished = threading.Event()

    def _run() -> None:
        platform_db.init_platform_db()
        finished.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert finished.is_set()


def test_get_session_factory_does_not_deadlock(platform_settings: Any) -> None:
    """回归：get_session_factory 同样在持锁状态下调用 get_engine。"""
    created: list[Any] = []

    def _run() -> None:
        created.append(platform_db.get_session_factory())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert created != []
