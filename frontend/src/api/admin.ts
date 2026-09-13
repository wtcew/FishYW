/**
 * 系统管理接口：用户 / 角色 / 告警规则 / 审计日志 / 系统健康。
 *
 * 契约（src/api/platform/admin.py）：
 * - 用户与审计、健康 = admin 专属；角色列表与告警规则列表 = 登录即可（viewer+）；
 * - 审计日志信封字段为 `pageSize`（手写 dict），与 assets/events 的 `page_size` 不同，
 *   这里交给 normalizePage 兼容；
 * - 重置密码返回一次性明文 `new_password`（仅响应中返回，不落盘）；
 * - 用户对象为 snake_case（normalizeUser 归一）。
 */
import { http } from "./http";
import {
  normalizePage,
  normalizeUser,
  type AlertRule,
  type AlertRulePayload,
  type AuditLog,
  type CreatedResult,
  type Page,
  type Role,
  type SystemHealth,
  type User,
  type UserCreatePayload,
  type UserUpdatePayload,
} from "./types";

// ── 用户 ──────────────────────────────────────────────────────────

/** 用户全量列表（admin）。 */
export async function listUsers(): Promise<User[]> {
  const rows = await http.get<Parameters<typeof normalizeUser>[0][]>("/admin/users");
  return (rows ?? []).map(normalizeUser);
}

/**
 * 新建用户（admin）。
 *
 * @param payload 用户写请求（字段为后端 snake_case 契约）。
 * @returns 新建用户档案。
 */
export async function createUser(payload: UserCreatePayload): Promise<User> {
  return normalizeUser(await http.post<Parameters<typeof normalizeUser>[0]>("/admin/users", payload));
}

/**
 * 更新用户（角色/状态/显示名；admin）。
 *
 * @param id 用户 ID。
 * @param payload 更新请求。
 * @returns 更新后的用户档案。
 */
export async function updateUser(id: number, payload: UserUpdatePayload): Promise<User> {
  return normalizeUser(await http.put<Parameters<typeof normalizeUser>[0]>(`/admin/users/${id}`, payload));
}

/**
 * 重置密码（admin）：返回一次性随机密码。
 *
 * @param id 用户 ID。
 * @returns 用户名与新密码（供管理员转交）。
 */
export async function resetPassword(id: number): Promise<{ username: string; newPassword: string }> {
  const res = await http.post<{ username?: string; new_password?: string; newPassword?: string }>(
    `/admin/users/${id}/reset-password`,
  );
  return { username: res?.username ?? "", newPassword: res?.newPassword ?? res?.new_password ?? "" };
}

// ── 角色 ──────────────────────────────────────────────────────────

/** 角色列表（登录即可，供下拉使用）。 */
export async function listRoles(): Promise<Role[]> {
  const rows = await http.get<Role[]>("/admin/roles");
  return rows ?? [];
}

// ── 告警规则 ─────────────────────────────────────────────────────

/** 告警规则列表（登录即可）。 */
export async function listAlertRules(): Promise<AlertRule[]> {
  const rows = await http.get<AlertRule[]>("/admin/alert-rules");
  return rows ?? [];
}

/**
 * 新建告警规则（admin）。
 *
 * @param payload 规则写请求。
 * @returns 新规则 ID。
 */
export async function createAlertRule(payload: AlertRulePayload): Promise<CreatedResult> {
  // 后端 AlertRuleIn 只认 snake_case；直发 camelCase 会被 Pydantic 判缺失
  // → 422 Field required（2026-09-13 UI 穷举测试 P1，与资产 422 同类）。
  return http.post<CreatedResult>("/admin/alert-rules", {
    name: payload.name,
    enabled: payload.enabled,
    match_field: payload.matchField,
    match_op: payload.matchOp,
    match_value: payload.matchValue,
    severity: payload.severity,
    auto_diagnose: payload.autoDiagnose,
    description: payload.description,
  });
}

/**
 * 更新告警规则（admin）。
 *
 * @param id 规则 ID。
 * @param payload 规则写请求（camelCase，内部转 snake_case 契约）。
 */
export async function updateAlertRule(id: number, payload: AlertRulePayload): Promise<void> {
  await http.put<{ status: string }>(`/admin/alert-rules/${id}`, {
    name: payload.name,
    enabled: payload.enabled,
    match_field: payload.matchField,
    match_op: payload.matchOp,
    match_value: payload.matchValue,
    severity: payload.severity,
    auto_diagnose: payload.autoDiagnose,
    description: payload.description,
  });
}

/**
 * 删除告警规则（admin）。
 *
 * @param id 规则 ID。
 */
export async function deleteAlertRule(id: number): Promise<void> {
  await http.del<{ status: string }>(`/admin/alert-rules/${id}`);
}

// ── 审计日志 ─────────────────────────────────────────────────────

/** 审计日志查询参数。 */
export interface AuditParams {
  page?: number;
  pageSize?: number;
  action?: string;
  username?: string;
  resourceType?: string;
  /** 按对象 ID 过滤（对象详情页回填变更历史用）。 */
  resourceId?: string;
}

/**
 * 审计日志分页（admin，倒序）。
 *
 * @param params 过滤与分页参数。
 * @param signal 可选取消信号。
 * @returns 归一后的分页结果。
 */
export async function listAuditLogs(params: AuditParams = {}, signal?: AbortSignal): Promise<Page<AuditLog>> {
  const raw = await http.get<Parameters<typeof normalizePage<AuditLog>>[0]>("/admin/audit-logs", {
    query: {
      page: params.page ?? 1,
      page_size: params.pageSize ?? 50,
      action: params.action || null,
      username: params.username || null,
      resource_type: params.resourceType || null,
      resource_id: params.resourceId || null,
    },
    signal,
  });
  return normalizePage<AuditLog>(raw);
}

// ── 系统健康 ─────────────────────────────────────────────────────

/**
 * 平台系统健康（admin：存储后端 / 表行数 / 版本）。
 *
 * silent：非管理员调用会 403，调用方（运行概览）就地降级为提示文案，
 * 不应弹全局错误提示。
 *
 * @param signal 可选取消信号。
 * @returns 健康快照。
 */
export async function systemHealth(signal?: AbortSignal): Promise<SystemHealth> {
  return http.get<SystemHealth>("/admin/system/health", { signal, silent: true });
}
