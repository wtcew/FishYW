"""系统管理路由：用户/角色/告警规则/审计日志。

除角色只读列表（登录即可）外全部 admin 专属。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from src.platform.audit import write_audit
from src.platform.db import get_db
from src.platform.deps import require_roles
from src.platform.models import AlertRule, AuditLog, Role, User
from src.platform.schemas import AlertRuleIn, AuditLogOut, UserCreateIn, UserOut, UserUpdateIn

router = APIRouter(prefix="/admin", tags=["admin"])


def _client_ip(request: Request) -> str:
    """取来源 IP。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _user_out(user: User) -> dict[str, Any]:
    """用户出参（含角色展开）。"""
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role_code=user.role.code,
        role_name=user.role.name,
        is_active=user.is_active,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    ).model_dump()


@router.get("/users")
def list_users(
    _admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """用户全量列表。"""
    return [_user_out(user) for user in db.query(User).order_by(User.id).all()]


@router.post("/users", status_code=201)
def create_user(
    body: UserCreateIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """新建用户（密码必填，bcrypt 落库）。"""
    if db.query(User).filter(User.username == body.username).count():
        raise HTTPException(status_code=409, detail="用户名已存在")
    role = db.query(Role).filter(Role.code == body.role_code).one()
    from src.platform.security import hash_password

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    write_audit(
        db, action="admin.user.create", resource_type="user", resource_id=str(user.id),
        user_id=admin.id, username=admin.username, detail={"role": body.role_code},
        ip=_client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdateIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新用户（角色/状态/显示名）；禁止停用自己 / 停用或降级最后一个管理员。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    def _other_active_admins() -> int:
        """除目标用户外仍在职的活跃管理员数。

        原实现统计全体活跃 admin（含操作者自己）再比较 <=1，在"不能停用自己"
        已生效的前提下永远为假——是死代码，起不到保护作用
        （2026-09-13 UI 穷举测试 P2）。
        """
        return (
            db.query(User)
            .join(Role)
            .filter(Role.code == "admin", User.is_active.is_(True), User.id != user.id)
            .count()
        )

    if body.is_active is False:
        if user.id == admin.id:
            raise HTTPException(status_code=400, detail="不能停用自己")
        if user.role.code == "admin" and _other_active_admins() == 0:
            raise HTTPException(status_code=400, detail="不能停用最后一个管理员")
    # 降级（admin → 其它角色）同样会抽走管理能力，且原实现完全没检查——
    # 允许把唯一管理员降级会让系统失去管理入口。（目标为他人时，操作者
    # 自己必定是活跃 admin，故 _other_active_admins() ≥ 1，天然放行。）
    if body.role_code is not None and body.role_code != "admin" and user.role.code == "admin":
        if _other_active_admins() == 0:
            raise HTTPException(status_code=400, detail="不能降级最后一个管理员")
    if body.role_code is not None:
        role = db.query(Role).filter(Role.code == body.role_code).one()
        user.role_id = role.id
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.is_active is not None:
        user.is_active = body.is_active
    write_audit(
        db, action="admin.user.update", resource_type="user", resource_id=str(user.id),
        user_id=admin.id, username=admin.username, detail=body.model_dump(exclude_none=True),
        ip=_client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return _user_out(user)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """重置密码：生成一次性随机密码（响应中返回，不落盘）。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_password = secrets.token_urlsafe(12)
    from src.platform.security import hash_password

    user.password_hash = hash_password(new_password)
    write_audit(
        db, action="admin.user.reset_password", resource_type="user", resource_id=str(user.id),
        user_id=admin.id, username=admin.username, ip=_client_ip(request),
    )
    db.commit()
    return {"username": user.username, "new_password": new_password}


@router.get("/roles")
def list_roles(_user: User = Depends(require_roles("viewer")), db: Session = Depends(get_db)) -> list[dict[str, str]]:
    """角色列表（登录即可，供前端下拉）。"""
    return [
        {"code": role.code, "name": role.name, "description": role.description}
        for role in db.query(Role).order_by(Role.id).all()
    ]


# ── 告警规则 ─────────────────────────────────────────────────────


@router.get("/alert-rules")
def list_alert_rules(
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """告警规则列表。"""
    return [
        {
            "id": rule.id, "name": rule.name, "enabled": rule.enabled,
            "matchField": rule.match_field, "matchOp": rule.match_op,
            "matchValue": rule.match_value, "severity": rule.severity,
            "autoDiagnose": rule.auto_diagnose, "description": rule.description,
        }
        for rule in db.query(AlertRule).order_by(AlertRule.id).all()
    ]


@router.post("/alert-rules", status_code=201)
def create_alert_rule(
    body: AlertRuleIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """新建告警规则。"""
    rule = AlertRule(**body.model_dump())
    db.add(rule)
    db.flush()
    write_audit(
        db, action="admin.alert_rule.create", resource_type="alert_rule",
        resource_id=str(rule.id), user_id=admin.id, username=admin.username,
        detail={"name": rule.name}, ip=_client_ip(request),
    )
    db.commit()
    return {"id": rule.id, "status": "created"}


@router.put("/alert-rules/{rule_id}")
def update_alert_rule(
    rule_id: int,
    body: AlertRuleIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """更新告警规则。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    for field, value in body.model_dump().items():
        setattr(rule, field, value)
    write_audit(
        db, action="admin.alert_rule.update", resource_type="alert_rule",
        resource_id=str(rule_id), user_id=admin.id, username=admin.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "updated"}


@router.delete("/alert-rules/{rule_id}")
def delete_alert_rule(
    rule_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """删除告警规则。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    db.delete(rule)
    write_audit(
        db, action="admin.alert_rule.delete", resource_type="alert_rule",
        resource_id=str(rule_id), user_id=admin.id, username=admin.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "deleted"}


# ── 审计日志 ─────────────────────────────────────────────────────


@router.get("/audit-logs")
def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str | None = Query(default=None),
    username: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    _admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """审计日志分页（动作/操作人/资源类型/资源 ID 过滤，倒序）。

    ``resource_id`` 过滤用于对象详情页回填变更历史（2026-09-13 UI 穷举测试
    BUG-04：前端原以"最近 100 条再前端过滤"的方式实现，审计总量超过窗口后
    早期对象的变更历史会整段消失）。
    """
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if username:
        query = query.filter(AuditLog.username == username)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if resource_id:
        query = query.filter(AuditLog.resource_id == resource_id)
    total = query.count()
    rows = (
        query.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [
            AuditLogOut(
                id=row.id, username=row.username, action=row.action,
                resourceType=row.resource_type, resourceId=row.resource_id,
                detail=row.detail, ip=row.ip,
                createdAt=row.created_at.isoformat() if row.created_at else None,
            ).model_dump()
            for row in rows
        ],
        "total": total, "page": page, "page_size": page_size,
    }


@router.get("/system/health")
def system_health(_admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)) -> dict[str, Any]:
    """平台系统健康（连通性 + 表行数 + 版本）。"""
    from src.platform.db import using_fallback
    from src.platform.models import Asset, Event, User

    return {
        "status": "ok",
        "storage": "sqlite-fallback" if using_fallback() else "mysql",
        "rows": {
            "users": db.query(User).count(),
            "assets": db.query(Asset).count(),
            "events": db.query(Event).count(),
        },
        "version": "2.0.0",
    }
