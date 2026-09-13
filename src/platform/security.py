"""平台安全原语：密码哈希与 JWT。

选型理由（后端设计文档 §0）：bcrypt 原生 API（不用与 bcrypt>=4.1 有兼容问题的
passlib）；PyJWT 而非维护停滞的 python-jose。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import bcrypt
import jwt

from src.settings import get_settings

_BCRYPT_ROUNDS = 12


def hash_password(plain: str) -> str:
    """生成 bcrypt 密码哈希。

    Args:
        plain: 明文密码。

    Returns:
        可入库的哈希串（含盐与轮次）。
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode(
        "utf-8"
    )


def verify_password(plain: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配。

    Args:
        plain: 待校验明文。
        password_hash: 库内哈希。

    Returns:
        匹配返回 True。
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _secret() -> str:
    """取 JWT 密钥：显式配置优先；未配置时生成进程级临时密钥。

    Returns:
        HS256 密钥字符串（长度 >= 32）。
    """
    settings = get_settings()
    configured = settings.JWT_SECRET.strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError("JWT_SECRET 长度必须 >= 32（当前 %d）" % len(configured))
        return configured
    global _ephemeral_secret
    if _ephemeral_secret is None:
        import secrets

        _ephemeral_secret = secrets.token_urlsafe(48)
        import logging

        logging.getLogger(__name__).warning(
            "未配置 JWT_SECRET，本次进程使用临时密钥（重启后所有 token 失效）；"
            "生产部署必须在 .env 提供长度 >= 32 的 JWT_SECRET"
        )
    return _ephemeral_secret


_ephemeral_secret: str | None = None


def create_access_token(user_id: int, username: str, role_code: str) -> tuple[str, int]:
    """签发访问令牌。

    Args:
        user_id: 用户 ID（写入 sub）。
        username: 用户名（仅供日志，权限判定一律回库）。
        role_code: 角色码（仅供日志）。

    Returns:
        ``(token, expires_in_seconds)``。
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_in = settings.JWT_EXPIRE_MINUTES * 60
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role_code,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": uuid4().hex,
    }
    token = jwt.encode(payload, _secret(), algorithm=settings.JWT_ALGORITHM)
    return token, expires_in


def decode_token(token: str) -> dict[str, Any]:
    """解码并校验令牌。

    Args:
        token: JWT 字符串。

    Returns:
        claims 字典。

    Raises:
        jwt.ExpiredSignatureError: 令牌过期。
        jwt.InvalidTokenError: 令牌非法。
    """
    settings = get_settings()
    return jwt.decode(token, _secret(), algorithms=[settings.JWT_ALGORITHM])
