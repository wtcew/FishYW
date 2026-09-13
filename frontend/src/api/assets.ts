/**
 * 资源中心（CMDB）接口：业务线 / 环境 / 资产。
 *
 * 契约（src/api/platform/assets.py）：读 viewer+，资产写 operator+，删除与业务线/环境维护 admin。
 * 标识三元组（环境, 类型, 标识）冲突返回 409（detail 已中文）。
 * 注意：列表信封字段为 `page_size`，由 normalizePage 归一（审计日志才是 pageSize）。
 */
import { http } from "./http";
import {
  normalizePage,
  type Asset,
  type AssetPayload,
  type BusinessLine,
  type CreatedResult,
  type Environment,
  type Page,
} from "./types";

/** 资产列表查询参数。 */
export interface AssetListParams {
  page?: number;
  pageSize?: number;
  environmentId?: number | null;
  businessLineId?: number | null;
  assetType?: string;
  status?: string;
  keyword?: string;
}

/**
 * 资产分页列表。
 *
 * @param params 过滤与分页参数。
 * @param signal 可选取消信号。
 * @returns 归一后的分页结果。
 */
export async function listAssets(params: AssetListParams = {}, signal?: AbortSignal): Promise<Page<Asset>> {
  const raw = await http.get<Parameters<typeof normalizePage<Asset>>[0]>("/assets", {
    query: {
      page: params.page ?? 1,
      page_size: params.pageSize ?? 20,
      environment_id: params.environmentId ?? null,
      business_line_id: params.businessLineId ?? null,
      asset_type: params.assetType || null,
      status: params.status || null,
      keyword: params.keyword || null,
    },
    signal,
  });
  return normalizePage<Asset>(raw);
}

/**
 * 资产详情。
 *
 * @param id 资产 ID。
 * @returns 资产对象。
 */
export async function getAsset(id: number): Promise<Asset> {
  return http.get<Asset>(`/assets/${id}`);
}

/**
 * 登记资产。
 *
 * @param payload 资产写请求（camelCase，内部转 snake_case 契约）。
 * @returns 新资产 ID。
 */
export async function createAsset(payload: AssetPayload): Promise<CreatedResult> {
  return http.post<CreatedResult>("/assets", {
    environment_id: payload.environmentId,
    name: payload.name,
    asset_type: payload.assetType,
    identifier: payload.identifier,
    owner_id: payload.ownerId,
    owner_contact: payload.ownerContact,
    status: payload.status,
    tags: payload.tags,
    remark: payload.remark,
  });
}

/**
 * 更新资产（整记录覆盖）。
 *
 * @param id 资产 ID。
 * @param payload 资产写请求（camelCase，内部转 snake_case 契约）。
 */
export async function updateAsset(id: number, payload: AssetPayload): Promise<void> {
  await http.put<{ status: string }>(`/assets/${id}`, {
    environment_id: payload.environmentId,
    name: payload.name,
    asset_type: payload.assetType,
    identifier: payload.identifier,
    owner_id: payload.ownerId,
    owner_contact: payload.ownerContact,
    status: payload.status,
    tags: payload.tags,
    remark: payload.remark,
  });
}

/**
 * 删除资产（存在关联事件时 409）。
 *
 * @param id 资产 ID。
 */
export async function deleteAsset(id: number): Promise<void> {
  await http.del<{ status: string }>(`/assets/${id}`);
}

// ── 业务线 ────────────────────────────────────────────────────────

/** 业务线列表（含环境计数）。 */
export async function listBusinessLines(): Promise<BusinessLine[]> {
  const rows = await http.get<BusinessLine[]>("/assets/business-lines");
  return rows ?? [];
}

/**
 * 新建业务线（admin）。
 *
 * @param payload 业务线写请求。
 * @returns 新业务线 ID。
 */
export async function createBusinessLine(payload: {
  code: string;
  name: string;
  ownerId: number | null;
  description: string;
}): Promise<CreatedResult> {
  return http.post<CreatedResult>("/assets/business-lines", {
    code: payload.code,
    name: payload.name,
    owner_id: payload.ownerId,
    description: payload.description,
  });
}

/**
 * 更新业务线（admin）。
 *
 * @param id 业务线 ID。
 * @param payload 业务线写请求。
 */
export async function updateBusinessLine(
  id: number,
  payload: { code: string; name: string; ownerId: number | null; description: string },
): Promise<void> {
  await http.put<{ status: string }>(`/assets/business-lines/${id}`, {
    code: payload.code,
    name: payload.name,
    owner_id: payload.ownerId,
    description: payload.description,
  });
}

/**
 * 删除业务线（admin；存在下级环境时 409）。
 *
 * @param id 业务线 ID。
 */
export async function deleteBusinessLine(id: number): Promise<void> {
  await http.del<{ status: string }>(`/assets/business-lines/${id}`);
}

// ── 环境 ─────────────────────────────────────────────────────────

/**
 * 环境列表（可按业务线过滤）。
 *
 * @param businessLineId 业务线 ID；不传为全量。
 * @returns 环境数组。
 */
export async function listEnvironments(businessLineId?: number | null): Promise<Environment[]> {
  const rows = await http.get<Environment[]>("/assets/environments", {
    query: { business_line_id: businessLineId ?? null },
  });
  return rows ?? [];
}

/**
 * 新建环境（admin）。
 *
 * @param payload 环境写请求。
 * @returns 新环境 ID。
 */
export async function createEnvironment(payload: {
  businessLineId: number;
  name: string;
  description: string;
}): Promise<CreatedResult> {
  return http.post<CreatedResult>("/assets/environments", {
    business_line_id: payload.businessLineId,
    name: payload.name,
    description: payload.description,
  });
}

/**
 * 删除环境（admin；存在资产时 409）。
 *
 * @param id 环境 ID。
 */
export async function deleteEnvironment(id: number): Promise<void> {
  await http.del<{ status: string }>(`/assets/environments/${id}`);
}
