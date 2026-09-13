"""FastAPI 依赖：当前用户解析与 RBAC 判定。

安全要点：``get_current_user`` 每次请求**回库校验**用户存在且 is_active——
token 内的 role 仅供日志，角色变更/停用即时生效。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from src.platform.db import get_db
from src.platform.models import User
from src.platform.security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

#: 角色能力矩阵（admin 恒通过一切，不逐项列出）。
ROLE_MATRIX: dict[str, set[str]] = {
    "admin": {"read", "write", "admin"},
    "operator": {"read", "write"},
    "viewer": {"read"},
}

_next_url = None  # 占位：避免 linter 误报未使用的 import


def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """解析 Bearer token 并回库加载当前用户。

    Args:
        request: 当前请求（取来源 IP 供审计）。
        token: Bearer 令牌。
        db: 平台会话。

    Returns:
        活跃用户。

    Raises:
        HTTPException: 401（缺失/过期/非法/停用）。
    """
    request.state.user = None
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims: dict[str, Any] = decode_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "登录已过期", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "令牌无效", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    user = db.get(User, int(claims.get("sub", "0")))
    if user is None or not user.is_active:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "账号不可用", headers={"WWW-Authenticate": "Bearer"}
        )
    request.state.user = user
    return user


def require_roles(*allowed: str) -> Callable[[User], User]:
    """角色依赖工厂：要求当前用户**具备目标角色所代表的全部能力**。

    语义（后端设计文档 §4.2 的 ROLE_MATRIX 能力矩阵）：

    * ``require_roles("viewer")`` → 需要 read 能力 ⇒ admin/operator/viewer 全部通过（"viewer+"）；
    * ``require_roles("operator")`` → 需要 read+write ⇒ admin/operator 通过，viewer 403；
    * ``require_roles("admin")`` → 需要 read+write+admin ⇒ 仅 admin 通过。

    Note:
        此前的实现是“角色码白名单”（``role.code not in allowed``），导致 operator
        对只读端点被 403（设计要求 operator 可读），权限不自洽；改为能力子集判定后
        只放宽读权限、不放松任何写/管理权限。

    Args:
        allowed: 目标角色码（其能力集合即通过阈值）。

    Returns:
        FastAPI 依赖函数；不满足时 403，未登录时由 :func:`get_current_user` 给 401。
    """
    required: set[str] = set()
    for code in allowed:
        required |= ROLE_MATRIX.get(code, {code})

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.code == "admin":  # 超管恒通过（与 ROLE_MATRIX 一致）
            return user
        if all(has_capability(user, capability) for capability in required):
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")

    return dependency


def has_capability(user: User, capability: str) -> bool:
    """判定用户是否具备某能力（read/write/admin）。

    Args:
        user: 当前用户（须已加载 role）。
        capability: read / write / admin。

    Returns:
        具备返回 True。
    """
    return capability in ROLE_MATRIX.get(user.role.code, set())
