/**
 * 平台 API 类型定义与规范化。
 *
 * 契约来源：后端 `src/platform/schemas.py` 与 `src/api/platform/{auth,assets,events,admin}.py`
 * 的**实际出参**（非设计文档描述）。三处与设计文档不一致，按"以后端为准"处理：
 *
 * 1. 分页信封：资产/事件列表返回 `page_size`（Page[T].model_dump 字段名），
 *    审计日志返回 `pageSize`（admin.py 手写 dict）—— 统一由 {@link normalizePage} 归一。
 * 2. 用户对象：`UserOut` 字段为 snake_case（display_name/role_code/role_name/is_active/
 *    last_login_at），登录出参 `LoginOut` 为 access_token/token_type/expires_in —— 归一为 camelCase。
 * 3. 若干动作端点返回 snake_case 键：triggerDiagnosis 的 `diagnosis_id`、
 *    resetPassword 的 `new_password`，由各自的 api 模块就地转换。
 */

/** 统一分页响应（前端内部形态，camelCase）。 */
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

/** 后端分页信封原始形态：page_size（assets/events）与 pageSize（audit-logs）两种。 */
interface RawPage<T> {
  items?: T[] | null;
  total?: number;
  page?: number;
  page_size?: number;
  pageSize?: number;
}

/**
 * 归一后端分页信封。
 *
 * @param raw 后端原始响应。
 * @returns 前端统一形态；缺字段按零值兜底，避免渲染层二次判空。
 */
export function normalizePage<T>(raw: RawPage<T> | null | undefined): Page<T> {
  return {
    items: raw?.items ?? [],
    total: raw?.total ?? 0,
    page: raw?.page ?? 1,
    pageSize: raw?.pageSize ?? raw?.page_size ?? 20,
  };
}

/** 可写能力（与后端 deps.ROLE_MATRIX 对齐）。 */
export type Capability = "read" | "write" | "admin";

/** 角色码：admin 恒通过一切；operator 具备读写；viewer 只读。 */
export type RoleCode = "admin" | "operator" | "viewer";

/** 后端 deps.ROLE_MATRIX 的前端镜像（仅用于按钮显隐，权限真值仍在后端）。 */
export const ROLE_CAPABILITIES: Record<RoleCode, Capability[]> = {
  admin: ["read", "write", "admin"],
  operator: ["read", "write"],
  viewer: ["read"],
};

/** 用户档案（前端统一形态）。 */
export interface User {
  id: number;
  username: string;
  displayName: string;
  roleCode: string;
  roleName: string;
  isActive: boolean;
  lastLoginAt: string | null;
}

/** 后端 UserOut 原始形态（snake_case）。 */
interface RawUser {
  id?: number;
  username?: string;
  display_name?: string;
  displayName?: string;
  role_code?: string;
  roleCode?: string;
  role_name?: string;
  roleName?: string;
  is_active?: boolean;
  isActive?: boolean;
  last_login_at?: string | null;
  lastLoginAt?: string | null;
}

/**
 * 归一用户对象（兼容 snake_case 与 camelCase 两种后端形态）。
 *
 * @param raw 后端原始用户对象。
 * @returns 前端统一形态。
 */
export function normalizeUser(raw: RawUser | null | undefined): User {
  return {
    id: raw?.id ?? 0,
    username: raw?.username ?? "",
    displayName: raw?.displayName ?? raw?.display_name ?? "",
    roleCode: raw?.roleCode ?? raw?.role_code ?? "",
    roleName: raw?.roleName ?? raw?.role_name ?? "",
    isActive: raw?.isActive ?? raw?.is_active ?? true,
    lastLoginAt: raw?.lastLoginAt ?? raw?.last_login_at ?? null,
  };
}

/** 登录结果（前端统一形态）。 */
export interface LoginResult {
  token: string;
  tokenType: string;
  expiresIn: number;
  user: User;
}

// ── 资源中心（CMDB） ───────────────────────────────────────────────

/** 业务线。 */
export interface BusinessLine {
  id: number;
  code: string;
  name: string;
  description: string;
  ownerId: number | null;
  environmentCount: number;
}

/** 环境。 */
export interface Environment {
  id: number;
  businessLineId: number;
  name: string;
  description: string;
  assetCount: number;
}

/** 资产类型（后端 Literal 白名单）。 */
export type AssetType = "host" | "db" | "middleware" | "app" | "network";

/** 资产状态。 */
export type AssetStatus = "active" | "maintenance" | "decommissioned";

/** 资产出参（AssetOut，camelCase）。 */
export interface Asset {
  id: number;
  name: string;
  assetType: string;
  identifier: string;
  status: string;
  ownerContact: string;
  tags: unknown[];
  remark: string;
  environmentId: number;
  environmentName: string;
  businessLineId: number | null;
  businessLineName: string;
  ownerName: string;
  updatedAt: string | null;
}

/** 资产写请求（AssetIn）。注意：出参不含 ownerId，编辑时无法回填负责人。 */
export interface AssetPayload {
  environmentId: number;
  name: string;
  assetType: AssetType;
  identifier: string;
  ownerId: number | null;
  ownerContact: string;
  status: AssetStatus;
  tags: string[];
  remark: string;
}

/** 创建类响应（{id, status}）。 */
export interface CreatedResult {
  id: number;
  status: string;
}

// ── 事件中心 ───────────────────────────────────────────────────────

/** 事件严重度（后端 SEVERITIES）。 */
export type EventSeverity = "critical" | "major" | "minor" | "info";

/** 事件状态（后端 TRANSITIONS 状态机键）。 */
export type EventStatus = "open" | "acknowledged" | "diagnosing" | "resolved" | "closed";

/** 事件出参（EventOut，camelCase）。 */
export interface OpsEvent {
  id: number;
  eventNo: string;
  title: string;
  source: string;
  severity: string;
  status: string;
  assetId: number | null;
  assetName: string;
  environmentName: string;
  businessLineName: string;
  acknowledgedByName: string;
  createdAt: string | null;
  resolvedAt: string | null;
}

/** 时间线条目（TimelineEntryOut）。 */
export interface TimelineEntry {
  id: number;
  entryType: string;
  actorType: string;
  actorName: string;
  content: string;
  detail: Record<string, unknown> | null;
  createdAt: string | null;
}

/** 研判状态（后端 models: running/completed/failed/timeout）。 */
export type DiagnosisStatus = "running" | "completed" | "failed" | "timeout";

/** 证据条目（services/events.py 装配）。 */
export interface DiagnosisEvidence {
  seq?: number;
  kind?: string;
  ref?: string;
  title?: string;
  snippet?: string;
}

/** 研判记录（DiagnosisOut）。 */
export interface Diagnosis {
  id: number;
  eventId: number;
  status: string;
  triggerType: string;
  rootCause: string | null;
  suggestion: string | null;
  confidence: number | null;
  evidence: DiagnosisEvidence[];
  evidenceSufficient: boolean | null;
  answerSummary: string;
  degraded: boolean;
  degradedReason: string | null;
  error: string | null;
  latencyMs: number | null;
  traceId: string;
  startedAt: string | null;
  finishedAt: string | null;
}

/** 事件详情（GET /events/{id}：EventOut + timeline + latestDiagnosis）。 */
export interface EventDetail extends OpsEvent {
  timeline: TimelineEntry[];
  latestDiagnosis: Diagnosis | null;
}

/** 手工登记事件请求（EventCreateIn）。 */
export interface EventCreatePayload {
  title: string;
  severity: EventSeverity;
  assetId: number | null;
  payload?: Record<string, unknown> | null;
}

/** 事件状态机（后端 services/events.py TRANSITIONS 的前端镜像，仅用于按钮可用性）。 */
export const EVENT_TRANSITIONS: Record<string, string[]> = {
  open: ["acknowledged", "closed"],
  acknowledged: ["diagnosing", "resolved", "closed"],
  diagnosing: ["acknowledged", "resolved", "closed"],
  resolved: ["closed"],
  closed: [],
};

// ── 系统管理 ───────────────────────────────────────────────────────

/** 角色（GET /admin/roles）。 */
export interface Role {
  code: string;
  name: string;
  description: string;
}

/** 告警规则（GET /admin/alert-rules）。 */
export interface AlertRule {
  id: number;
  name: string;
  enabled: boolean;
  matchField: string;
  matchOp: string;
  matchValue: string;
  severity: string;
  autoDiagnose: boolean;
  description: string;
}

/** 告警规则写请求（AlertRuleIn）。 */
export interface AlertRulePayload {
  name: string;
  enabled: boolean;
  matchField: string;
  matchOp: string;
  matchValue: string;
  severity: EventSeverity;
  autoDiagnose: boolean;
  description: string;
}

/** 审计日志（AuditLogOut）。 */
export interface AuditLog {
  id: number;
  username: string;
  action: string;
  resourceType: string;
  resourceId: string;
  detail: Record<string, unknown> | null;
  ip: string;
  createdAt: string | null;
}

/** 系统健康（GET /admin/system/health）。 */
export interface SystemHealth {
  status: string;
  storage: string;
  rows: { users: number; assets: number; events: number };
  version: string;
}

/** RAG 引擎健康（GET /api/v1/health，既有端点）。 */
export interface RagHealth {
  status: string;
  vector_count: number;
  graph_nodes: number;
  graph_edges: number;
}

/** 用户写请求（UserCreateIn），字段为 snake_case（后端契约）。 */
export interface UserCreatePayload {
  username: string;
  password: string;
  display_name: string;
  role_code: RoleCode;
}

/** 用户更新请求（UserUpdateIn）。 */
export interface UserUpdatePayload {
  display_name?: string;
  role_code?: RoleCode;
  is_active?: boolean;
}
