"""资产登记路由：业务线 / 环境 / 资产 CRUD 与分页过滤。

读=viewer+，写=operator+，删除与业务线/环境维护=admin。
标识三元组（环境, 类型, 标识）唯一冲突返回 409。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from src.platform.audit import write_audit
from src.platform.db import get_db
from src.platform.deps import get_current_user, require_roles
from src.platform.models import Asset, BusinessLine, Environment, User
from src.platform.schemas import AssetIn, AssetOut, BusinessLineIn, EnvironmentIn, Page

router = APIRouter(prefix="/assets", tags=["assets"])


def _client_ip(request: Request) -> str:
    """取来源 IP。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _asset_out(asset: Asset) -> AssetOut:
    """构造资产出参（展开环境/业务线/负责人名）。"""
    owner_name = ""
    if asset.owner_id:
        owner = asset.owner
        owner_name = owner.display_name or owner.username if owner else ""
    return AssetOut(
        id=asset.id,
        name=asset.name,
        assetType=asset.asset_type,
        identifier=asset.identifier,
        status=asset.status,
        ownerContact=asset.owner_contact,
        tags=asset.tags or [],
        remark=asset.remark,
        environmentId=asset.environment_id,
        environmentName=asset.environment.name if asset.environment else "",
        businessLineId=asset.environment.business_line_id if asset.environment else None,
        businessLineName=(
            asset.environment.business_line.name if asset.environment and asset.environment.business_line else ""
        ),
        ownerName=owner_name,
        updatedAt=asset.updated_at.isoformat() if asset.updated_at else None,
    )


# ── 业务线 ────────────────────────────────────────────────────────


@router.get("/business-lines")
def list_business_lines(
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """业务线全量列表（含环境计数）。"""
    lines = db.query(BusinessLine).order_by(BusinessLine.id).all()
    return [
        {
            "id": line.id,
            "code": line.code,
            "name": line.name,
            "description": line.description,
            "ownerId": line.owner_id,
            "environmentCount": len(line.environments),
        }
        for line in lines
    ]


@router.post("/business-lines", status_code=201)
def create_business_line(
    body: BusinessLineIn,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """新建业务线（code/name 唯一）。"""
    if db.query(BusinessLine).filter(BusinessLine.code == body.code).count():
        raise HTTPException(status_code=409, detail="业务编码已存在")
    if db.query(BusinessLine).filter(BusinessLine.name == body.name).count():
        raise HTTPException(status_code=409, detail="业务名称已存在")
    line = BusinessLine(**body.model_dump())
    db.add(line)
    db.flush()
    write_audit(
        db, action="asset.business_line.create", resource_type="business_line",
        resource_id=str(line.id), user_id=user.id, username=user.username,
        detail={"code": line.code}, ip=_client_ip(request),
    )
    db.commit()
    return {"id": line.id, "status": "created"}


@router.put("/business-lines/{line_id}")
def update_business_line(
    line_id: int,
    body: BusinessLineIn,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """更新业务线。"""
    line = db.get(BusinessLine, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="业务线不存在")
    for field, value in body.model_dump().items():
        setattr(line, field, value)
    write_audit(
        db, action="asset.business_line.update", resource_type="business_line",
        resource_id=str(line.id), user_id=user.id, username=user.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "updated"}


@router.delete("/business-lines/{line_id}")
def delete_business_line(
    line_id: int,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """删除业务线（存在下级环境时 409）。"""
    line = db.get(BusinessLine, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="业务线不存在")
    if line.environments:
        raise HTTPException(status_code=409, detail=f"存在 {len(line.environments)} 个下级环境，禁止删除")
    db.delete(line)
    write_audit(
        db, action="asset.business_line.delete", resource_type="business_line",
        resource_id=str(line_id), user_id=user.id, username=user.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "deleted"}


# ── 环境 ─────────────────────────────────────────────────────────


@router.get("/environments")
def list_environments(
    business_line_id: int | None = Query(default=None),
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """环境列表（可按业务线过滤）。"""
    query = db.query(Environment)
    if business_line_id is not None:
        query = query.filter(Environment.business_line_id == business_line_id)
    return [
        {
            "id": env.id,
            "businessLineId": env.business_line_id,
            "name": env.name,
            "description": env.description,
            "assetCount": len(env.assets),
        }
        for env in query.order_by(Environment.id).all()
    ]


@router.post("/environments", status_code=201)
def create_environment(
    body: EnvironmentIn,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """新建环境（同一业务线内名称唯一）。"""
    if db.get(BusinessLine, body.business_line_id) is None:
        raise HTTPException(status_code=404, detail="业务线不存在")
    exists = (
        db.query(Environment)
        .filter(Environment.business_line_id == body.business_line_id, Environment.name == body.name)
        .count()
    )
    if exists:
        raise HTTPException(status_code=409, detail="该业务线下环境名已存在")
    env = Environment(**body.model_dump())
    db.add(env)
    db.flush()
    write_audit(
        db, action="asset.environment.create", resource_type="environment",
        resource_id=str(env.id), user_id=user.id, username=user.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"id": env.id, "status": "created"}


@router.delete("/environments/{env_id}")
def delete_environment(
    env_id: int,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """删除环境（存在资产时 409）。"""
    env = db.get(Environment, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    if env.assets:
        raise HTTPException(status_code=409, detail=f"存在 {len(env.assets)} 台资产，禁止删除")
    db.delete(env)
    write_audit(
        db, action="asset.environment.delete", resource_type="environment",
        resource_id=str(env_id), user_id=user.id, username=user.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "deleted"}


# ── 资产 ─────────────────────────────────────────────────────────


@router.get("")
def list_assets(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    environment_id: int | None = Query(default=None),
    business_line_id: int | None = Query(default=None),
    asset_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> Page[dict[str, object]]:
    """资产分页列表（环境/业务线/类型/状态/关键字过滤）。"""
    query = db.query(Asset)
    if environment_id is not None:
        query = query.filter(Asset.environment_id == environment_id)
    if business_line_id is not None:
        query = query.join(Environment).filter(Environment.business_line_id == business_line_id)
    if asset_type:
        query = query.filter(Asset.asset_type == asset_type)
    if status:
        query = query.filter(Asset.status == status)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(Asset.name.like(like) | Asset.identifier.like(like))
    total = query.count()
    assets = (
        query.order_by(Asset.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return Page(
        items=[_asset_out(asset).model_dump() for asset in assets],
        total=total, page=page, page_size=page_size,
    )


@router.get("/{asset_id}")
def get_asset(
    asset_id: int,
    _user: User = Depends(require_roles("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """资产详情。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    return _asset_out(asset).model_dump()


def _assert_unique(db: Session, body: AssetIn, exclude_id: int | None = None) -> None:
    """校验标识三元组唯一（排除自身 id）。"""
    query = db.query(Asset).filter(
        Asset.environment_id == body.environment_id,
        Asset.asset_type == body.asset_type,
        Asset.identifier == body.identifier,
    )
    if exclude_id is not None:
        query = query.filter(Asset.id != exclude_id)
    if query.count():
        raise HTTPException(status_code=409, detail="同环境下同类型同标识的资产已存在")


@router.post("", status_code=201)
def create_asset(
    body: AssetIn,
    request: Request,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """登记资产。"""
    if db.get(Environment, body.environment_id) is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    _assert_unique(db, body)
    asset = Asset(**body.model_dump())
    db.add(asset)
    db.flush()
    write_audit(
        db, action="asset.create", resource_type="asset",
        resource_id=str(asset.id), user_id=user.id, username=user.username,
        detail={"name": asset.name, "identifier": asset.identifier},
        ip=_client_ip(request),
    )
    db.commit()
    return {"id": asset.id, "status": "created"}


@router.put("/{asset_id}")
def update_asset(
    asset_id: int,
    body: AssetIn,
    request: Request,
    user: User = Depends(require_roles("operator")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """更新资产。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    _assert_unique(db, body, exclude_id=asset_id)
    for field, value in body.model_dump().items():
        setattr(asset, field, value)
    write_audit(
        db, action="asset.update", resource_type="asset",
        resource_id=str(asset_id), user_id=user.id, username=user.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "updated"}


@router.delete("/{asset_id}")
def delete_asset(
    asset_id: int,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """删除资产（存在关联事件时 409，建议先置 decommissioned）。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    from src.platform.models import Event

    if db.query(Event).filter(Event.asset_id == asset_id).count():
        raise HTTPException(status_code=409, detail="存在关联事件，建议先置为已下线（decommissioned）")
    db.delete(asset)
    write_audit(
        db, action="asset.delete", resource_type="asset",
        resource_id=str(asset_id), user_id=user.id, username=user.username,
        ip=_client_ip(request),
    )
    db.commit()
    return {"status": "deleted"}
