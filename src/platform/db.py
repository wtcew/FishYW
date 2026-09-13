"""平台数据库访问：引擎惰性初始化、会话依赖与种子引导。

连接串优先级（用户决策 2026-09-12）：

1. `.env` 提供 ``MYSQL_HOST/PORT/USER/PASSWORD/DB`` → MySQL（pymysql 驱动）；
2. 未提供或连接失败 → 回退 SQLite ``PLATFORM_DB``（默认 D:\\fishcloud-data\\platform.db，
   WAL + busy_timeout），日志告警但不阻断启动。

引擎惰性初始化（首次 :func:`get_db` 才建引擎/建表/种子）是兼容性关键：
存量 269 个测试会触发 FastAPI lifespan 但从不请求平台端点，
惰性初始化保证它们零副作用（不触碰 D 盘真实路径）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import Base
from src.settings import get_settings

logger = logging.getLogger(__name__)

# 必须可重入：init_platform_db()/get_session_factory() 在持锁状态下还要调用
# get_engine()，而后者同样取该锁；用普通 Lock 会自死锁（首个平台请求永久挂起）。
_init_lock = threading.RLock()
_initialized = False
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_using_fallback = False


def _mysql_url() -> URL | None:
    """按 settings 组装 MySQL 连接对象；配置不完整返回 None。

    Returns:
        ``mysql+pymysql`` 的 :class:`URL`（凭据经 URL.create 转义，防特殊字符破坏连接串），
        或 ``None``（未配置完整四要素）。
    """
    settings = get_settings()
    if not (settings.MYSQL_HOST and settings.MYSQL_USER and settings.MYSQL_DB):
        return None
    return URL.create(
        drivername="mysql+pymysql",
        username=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        database=settings.MYSQL_DB,
        query={"charset": "utf8mb4"},
    )


def _sqlite_url() -> str:
    """组装 SQLite 兜底连接串，并确保其父目录存在（D 盘，项目外）。

    Returns:
        ``sqlite:///`` 连接串。
    """
    settings = get_settings()
    db_path = Path(settings.PLATFORM_DB)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def _build_engine() -> Engine:
    """构建数据库引擎，MySQL 失败时自动回退 SQLite。

    Returns:
        已配置事件钩子的 SQLAlchemy 引擎。
    """
    global _using_fallback
    settings = get_settings()
    url = _mysql_url()
    if url is not None:
        try:
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=3600,
                future=True,
            )
            with engine.connect() as probe:
                probe.exec_driver_sql("SELECT 1")
            _using_fallback = False
            logger.info(
                "平台库使用 MySQL: %s:%s/%s", settings.MYSQL_HOST, settings.MYSQL_PORT, settings.MYSQL_DB
            )
            return engine
        except Exception as exc:  # noqa: BLE001 - 回退而非阻断
            logger.warning("MySQL 连接失败，回退 SQLite: %s", exc)
    _using_fallback = True
    engine = create_engine(_sqlite_url(), future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # noqa: ANN001
        """SQLite 并发兜底：WAL + 忙等 5 秒，降低研判 worker 与请求互斥概率。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    logger.warning("平台库使用 SQLite 兜底: %s", settings.PLATFORM_DB)
    return engine


def get_engine() -> Engine:
    """返回平台引擎（惰性创建，线程安全）。

    Returns:
        进程内唯一的 SQLAlchemy 引擎。
    """
    global _engine
    with _init_lock:
        if _engine is None:
            _engine = _build_engine()
        return _engine


def get_session_factory() -> sessionmaker[Session]:
    """返回会话工厂（惰性创建，线程安全）。

    Returns:
        绑定引擎的 sessionmaker。
    """
    global _session_factory
    with _init_lock:
        if _session_factory is None:
            _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
        return _session_factory


def init_platform_db() -> None:
    """建表并引导默认角色与管理员（幂等，线程安全）。

    Note:
        管理员密码策略（2026-09-13 用户决策：固定 ``admin/123456``，便于免安装版
        开箱即用）：优先取 ``PLATFORM_ADMIN_PASSWORD``（.env/环境变量；settings
        默认值即 "123456"）；显式置为空串时回退随机口令并打印一次。
        种子仅在"无任何用户"时创建——**已有库需在「系统管理 → 用户」改密，
        或删除 admin 后重启重建**。生产部署务必修改默认口令。
    """
    global _initialized
    with _init_lock:
        if _initialized:
            return
        engine = get_engine()
        from src.platform.models import Base, Role, User
        from src.platform.security import hash_password

        Base.metadata.create_all(engine)
        settings = get_settings()
        with Session(engine) as session:
            role_count = session.query(Role).count()
            if role_count == 0:
                session.add_all(
                    [
                        Role(code="admin", name="管理员", description="全部权限"),
                        Role(code="operator", name="运维工程师", description="资产/事件读写与研判"),
                        Role(code="viewer", name="只读观察员", description="只读"),
                    ]
                )
                session.commit()
            if session.query(User).count() == 0:
                admin_role = session.query(Role).filter_by(code="admin").one()
                if settings.PLATFORM_ADMIN_PASSWORD:
                    password = settings.PLATFORM_ADMIN_PASSWORD
                    origin = "PLATFORM_ADMIN_PASSWORD（默认 123456，生产务必修改）"
                else:
                    import secrets

                    password = secrets.token_urlsafe(12)
                    origin = "随机生成（PLATFORM_ADMIN_PASSWORD 被显式置空；仅打印本次）"
                session.add(
                    User(
                        username="admin",
                        password_hash=hash_password(password),
                        display_name="管理员",
                        role_id=admin_role.id,
                    )
                )
                session.commit()
                logger.warning("已创建默认管理员 admin，密码（%s）：%s", origin, password)
        _initialized = True
        logger.info("平台数据库初始化完成（幂等跳过后续调用）")


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：提供平台数据库会话。

    Yields:
        已惰性初始化的 SQLAlchemy 会话；请求结束自动关闭。
    """
    init_platform_db()
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def reset_for_tests() -> None:
    """测试专用：清空引擎/工厂/初始化标志（配合 tmp_path SQLite 使用）。"""
    global _engine, _session_factory, _initialized
    with _init_lock:
        _engine = None
        _session_factory = None
        _initialized = False


def dispose_engine() -> None:
    """释放连接池并复位初始化标志（桌面端退出时调用，幂等）。

    未初始化过则直接返回；释放失败只记日志不抛异常——退出路径不允许
    因为清理动作本身而崩溃。
    """
    global _engine, _session_factory, _initialized
    with _init_lock:
        engine, _engine = _engine, None
        _session_factory = None
        _initialized = False
    if engine is None:
        return
    try:
        engine.dispose()
        logger.info("平台数据库连接池已释放")
    except Exception:  # noqa: BLE001 - 退出清理不允许反向阻塞进程终止
        logger.warning("平台数据库连接池释放失败（忽略）", exc_info=True)


def using_fallback() -> bool:
    """当前是否处于 SQLite 兜底模式。

    Returns:
        MySQL 不可用回退后为 True。
    """
    return _using_fallback
