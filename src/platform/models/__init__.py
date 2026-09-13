"""平台 ORM 模型汇总。

全部模型继承同一 ``Base``，供 :meth:`src.platform.db.init_platform_db` 的
``create_all`` 一次性建表；新模型必须在本模块 re-export，否则不会被建表发现。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """平台模型公共声明基类。"""


from src.platform.models.user import Role, User  # noqa: E402,F401
from src.platform.models.audit import AuditLog  # noqa: E402,F401
from src.platform.models.cmdb import Asset, BusinessLine, Environment  # noqa: E402,F401
from src.platform.models.alerting import AlertRule, Event, EventTimelineEntry  # noqa: E402,F401
from src.platform.models.diagnosis import DiagnosisRecord  # noqa: E402,F401

__all__ = [
    "Base",
    "Role",
    "User",
    "AuditLog",
    "BusinessLine",
    "Environment",
    "Asset",
    "AlertRule",
    "Event",
    "EventTimelineEntry",
    "DiagnosisRecord",
]
