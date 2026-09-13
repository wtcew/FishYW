"""平台 Pydantic Schema 集中定义（请求/响应模型）。

约定：出参字段统一 camelCase 别名（前端 TS 类型一一对应）；
分页响应统一 ``Page[T]`` 结构 ``{items, total, page, pageSize}``。
"""

from __future__ import annotations

from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """统一分页响应。"""

    items: list[T]
    total: int
    page: int
    page_size: int


class PageParams(BaseModel):
    """统一分页参数（路由层从 query 解析后构造）。"""

    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


# ── auth ──────────────────────────────────────────────────────────


class LoginIn(BaseModel):
    """登录请求。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class LoginOut(BaseModel):
    """登录响应。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class ChangePasswordIn(BaseModel):
    """改密请求。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class RegisterIn(BaseModel):
    """自助注册请求。"""

    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=128)


class RegisterOut(BaseModel):
    """注册响应；``status=pending`` 表示待管理员启用。"""

    id: int
    username: str
    status: str


class UserOut(BaseModel):
    """用户档案（出参）。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    username: str
    display_name: str = ""
    role_code: str
    role_name: str = ""
    is_active: bool = True
    last_login_at: Optional[str] = None


class UserCreateIn(BaseModel):
    """管理员建用户。"""

    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    role_code: Literal["admin", "operator", "viewer"]


class UserUpdateIn(BaseModel):
    """管理员改用户（角色/状态/显示名）。"""

    display_name: Optional[str] = None
    role_code: Optional[Literal["admin", "operator", "viewer"]] = None
    is_active: Optional[bool] = None


# ── cmdb ──────────────────────────────────────────────────────────


class BusinessLineIn(BaseModel):
    """业务线写请求。"""

    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    owner_id: Optional[int] = None
    description: str = ""


class EnvironmentIn(BaseModel):
    """环境写请求。"""

    business_line_id: int
    name: str = Field(min_length=1, max_length=64)
    description: str = ""


class AssetIn(BaseModel):
    """资产写请求。"""

    environment_id: int
    name: str = Field(min_length=1, max_length=128)
    asset_type: Literal["host", "db", "middleware", "app", "network"]
    identifier: str = Field(min_length=1, max_length=128)
    owner_id: Optional[int] = None
    owner_contact: str = ""
    status: Literal["active", "maintenance", "decommissioned"] = "active"
    tags: list[str] = []
    remark: str = ""


class AssetOut(BaseModel):
    """资产出参（含环境/业务线展开与负责人名）。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    name: str
    assetType: str
    identifier: str
    status: str
    ownerContact: str = ""
    tags: list[Any] = []
    remark: str = ""
    environmentId: int
    environmentName: str = ""
    businessLineId: Optional[int] = None
    businessLineName: str = ""
    ownerName: str = ""
    updatedAt: Optional[str] = None


# ── events ────────────────────────────────────────────────────────


class EventCreateIn(BaseModel):
    """手工建事件。"""

    title: str = Field(min_length=1, max_length=255)
    severity: Literal["critical", "major", "minor", "info"] = "major"
    asset_id: Optional[int] = None
    payload: Optional[dict[str, Any]] = None


class EventTransitionIn(BaseModel):
    """状态流转请求（resolve 需处置说明）。"""

    resolution_note: str = ""
    comment: str = ""


class CommentIn(BaseModel):
    """复盘时间线评论。"""

    content: str = Field(min_length=1, max_length=2000)


class TimelineEntryOut(BaseModel):
    """时间线出参。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    entryType: str
    actorType: str
    actorName: str = ""
    content: str = ""
    detail: Optional[dict[str, Any]] = None
    createdAt: Optional[str] = None


class DiagnosisOut(BaseModel):
    """研判记录出参。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    eventId: int
    status: str
    triggerType: str
    rootCause: Optional[str] = None
    suggestion: Optional[str] = None
    confidence: Optional[float] = None
    evidence: list[Any] = []
    evidenceSufficient: Optional[bool] = None
    answerSummary: str = ""
    degraded: bool = False
    degradedReason: Optional[str] = None
    error: Optional[str] = None
    latencyMs: Optional[float] = None
    traceId: str = ""
    startedAt: Optional[str] = None
    finishedAt: Optional[str] = None


class EventOut(BaseModel):
    """事件出参。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    eventNo: str
    title: str
    source: str
    severity: str
    status: str
    assetId: Optional[int] = None
    assetName: str = ""
    environmentName: str = ""
    businessLineName: str = ""
    acknowledgedByName: str = ""
    createdAt: Optional[str] = None
    resolvedAt: Optional[str] = None


class WebhookIn(BaseModel):
    """外部告警 webhook 载荷（宽松：任意 JSON）。"""

    title: Optional[str] = None
    severity: Optional[str] = None
    asset_identifier: Optional[str] = None
    payload: Optional[dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


# ── admin ─────────────────────────────────────────────────────────


class AlertRuleIn(BaseModel):
    """告警规则写请求。"""

    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    match_field: Literal["title", "source", "asset.identifier"] | str = "title"
    match_op: Literal["contains", "eq", "regex"]
    match_value: str = Field(min_length=1, max_length=255)
    severity: Literal["critical", "major", "minor", "info"] = "major"
    auto_diagnose: bool = False
    description: str = ""


class AuditLogOut(BaseModel):
    """审计出参。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    username: str = ""
    action: str
    resourceType: str = ""
    resourceId: str = ""
    detail: Optional[dict[str, Any]] = None
    ip: str = ""
    createdAt: Optional[str] = None


LoginOut.model_rebuild()
