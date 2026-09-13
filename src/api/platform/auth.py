"""认证路由：登录 / 当前用户 / 改密。

登录成败均写审计（失败 user_id 为空、username 记尝试值）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.platform.audit import write_audit
from src.platform.db import get_db
from src.platform.deps import get_current_user
from src.platform.models import Role, User
from src.platform.schemas import (
    ChangePasswordIn,
    LoginIn,
    LoginOut,
    RegisterIn,
    RegisterOut,
    UserOut,
)
from src.platform.security import create_access_token, hash_password, verify_password
from src.settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    """取来源 IP（兼容代理头）。

    Args:
        request: 当前请求。

    Returns:
        IP 字符串。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _user_out(user: User) -> UserOut:
    """构造用户出参（含角色展开）。

    Args:
        user: 用户 ORM 对象（须已加载 role）。

    Returns:
        UserOut。
    """
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role_code=user.role.code,
        role_name=user.role.name,
        is_active=user.is_active,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> LoginOut:
    """账号密码登录，签发 JWT。

    Args:
        body: 登录请求。
        request: 当前请求。
        db: 平台会话。

    Returns:
        令牌与用户档案。

    Raises:
        HTTPException: 401（账号不存在/密码错误/已停用）。
    """
    ip = _client_ip(request)
    user = db.query(User).filter(User.username == body.username).one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        write_audit(
            db,
            action="user.login",
            resource_type="user",
            resource_id=body.username,
            username=body.username,
            detail={"result": "failed"},
            ip=ip,
        )
        db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        write_audit(
            db,
            action="user.login",
            resource_type="user",
            resource_id=str(user.id),
            username=user.username,
            detail={"result": "failed", "reason": "inactive"},
            ip=ip,
        )
        db.commit()
        raise HTTPException(status_code=401, detail="账号已停用")

    token, expires_in = create_access_token(user.id, user.username, user.role.code)
    user.last_login_at = datetime.now(timezone.utc)
    write_audit(
        db,
        action="user.login",
        resource_type="user",
        resource_id=str(user.id),
        user_id=user.id,
        username=user.username,
        detail={"result": "success"},
        ip=ip,
    )
    db.commit()
    db.refresh(user)
    return LoginOut(access_token=token, expires_in=expires_in, user=_user_out(user))


@router.get("/registration-status")
def registration_status() -> dict[str, bool]:
    """注册开放状态（公开端点，供前端在登录/注册页预检，避免填完表单才被 403）。

    Returns:
        ``allow_registration``：是否开放自助注册；
        ``auto_active``：注册后是否直接可登录（False = 待管理员启用）。
    """
    settings = get_settings()
    return {
        "allow_registration": settings.PLATFORM_ALLOW_REGISTRATION,
        "auto_active": settings.PLATFORM_REGISTRATION_AUTO_ACTIVE,
    }


@router.post("/register", status_code=201, response_model=RegisterOut)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> RegisterOut:
    """自助注册（受 ``PLATFORM_ALLOW_REGISTRATION`` 开关控制，默认关闭）。

    新账号一律以只读角色（viewer）创建，默认需管理员在用户管理里启用
    （``PLATFORM_REGISTRATION_AUTO_ACTIVE`` 为真时注册即可登录）。
    注册成败均写审计；``RegisterIn.email`` 暂不落库（users 表无该列）。

    Args:
        body: 注册请求。
        request: 当前请求。
        db: 平台会话。

    Returns:
        新账号标识与状态（pending 表示待管理员启用）。

    Raises:
        HTTPException: 403（注册未开放）、409（用户名已存在）、500（角色未初始化）。
    """
    settings = get_settings()
    if not settings.PLATFORM_ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="注册暂未开放，请联系管理员开通账号")
    username = body.username.strip()
    if db.query(User).filter(User.username == username).one_or_none() is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    viewer = db.query(Role).filter(Role.code == "viewer").one_or_none()
    if viewer is None:
        raise HTTPException(status_code=500, detail="系统角色未初始化")
    active = settings.PLATFORM_REGISTRATION_AUTO_ACTIVE
    user = User(
        username=username,
        password_hash=hash_password(body.password),
        display_name=body.display_name.strip() or username,
        role_id=viewer.id,
        is_active=active,
    )
    db.add(user)
    db.flush()
    write_audit(
        db,
        action="user.register",
        resource_type="user",
        resource_id=str(user.id),
        user_id=user.id,
        username=username,
        detail={"status": "active" if active else "pending"},
        ip=_client_ip(request),
    )
    db.commit()
    return RegisterOut(id=user.id, username=username, status="active" if active else "pending")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    """当前用户档案。

    Args:
        user: 当前用户依赖。

    Returns:
        UserOut。
    """
    return _user_out(user)


@router.post("/change-password")
def change_password(
    body: ChangePasswordIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """修改自身密码（校验旧密码；旧 token 仍有效为 Phase 1 已知取舍）。

    Args:
        body: 改密请求。
        request: 当前请求。
        user: 当前用户。
        db: 平台会话。

    Returns:
        状态说明。
    """
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")
    user.password_hash = hash_password(body.new_password)
    write_audit(
        db,
        action="user.change_password",
        resource_type="user",
        resource_id=str(user.id),
        user_id=user.id,
        username=user.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "changed"}
