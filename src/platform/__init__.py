"""FishCloud 平台业务层。

与 AIOps 推理引擎（内存态）解耦的持久化业务层：RBAC、资产登记（CMDB）、
事件中心与研判记录。存储经 SQLAlchemy 访问 MySQL（优先）或 SQLite（兜底），
数据库文件/实例均在项目之外（零 C 盘约束下的业务落盘例外，见
tests/test_zero_disk.py 的口径说明）。

Note:
    引擎惰性初始化：首次 :func:`get_db` 被调用时才建引擎/建表/种子，
    保证存量测试（不请求平台端点）零副作用。
"""

from __future__ import annotations

from src.platform.db import get_db, init_platform_db
from src.platform.security import create_access_token, decode_token, hash_password, verify_password

__all__ = [
    "get_db",
    "init_platform_db",
    "create_access_token",
    "decode_token",
    "hash_password",
    "verify_password",
]
