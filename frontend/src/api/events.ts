/**
 * 事件中心接口：列表 / 详情 / 手工登记 / 状态流转 / 评论 / AI 研判。
 *
 * 契约（src/api/platform/events.py）：
 * - 读 viewer+；登记/认领/恢复/关闭/评论/触发研判 operator+；
 * - 列表信封字段为 `page_size`（normalizePage 归一）；
 * - 详情 = EventOut + timeline（升序）+ latestDiagnosis；
 * - 触发研判返回 202 `{diagnosis_id, status}`（snake_case），前端轮询 getDiagnosis；
 * - 研判历史走 GET /events/{id}/diagnoses（登录可读，按开始时间倒序，信封 {items,total}）；
 * - 状态流转失败（非法流转/缺少处置说明）返回 409，detail 已中文。
 *
 * 实时性：按 PRD §5.4，轮询是默认推荐通道（SSE 观察端点属增强，本轮前端不接），
 * 因此单条研判仍由 {@link getDiagnosis} 轮询，历史列表只在进页面/刷新时拉取。
 */
import { http } from "./http";
import {
  normalizePage,
  type Diagnosis,
  type EventCreatePayload,
  type EventDetail,
  type OpsEvent,
  type Page,
} from "./types";

/** 事件列表查询参数。 */
export interface EventListParams {
  page?: number;
  pageSize?: number;
  status?: string;
  severity?: string;
  assetId?: number | null;
  businessLineId?: number | null;
  keyword?: string;
}

/**
 * 事件分页列表（创建时间倒序）。
 *
 * @param params 过滤与分页参数。
 * @param signal 可选取消信号。
 * @returns 归一后的分页结果。
 */
export async function listEvents(params: EventListParams = {}, signal?: AbortSignal): Promise<Page<OpsEvent>> {
  const raw = await http.get<Parameters<typeof normalizePage<OpsEvent>>[0]>("/events", {
    query: {
      page: params.page ?? 1,
      page_size: params.pageSize ?? 20,
      status: params.status || null,
      severity: params.severity || null,
      asset_id: params.assetId ?? null,
      business_line_id: params.businessLineId ?? null,
      keyword: params.keyword || null,
    },
    signal,
  });
  return normalizePage<OpsEvent>(raw);
}

/**
 * 事件详情（含时间线与最近一次研判）。
 *
 * @param id 事件 ID。
 * @param signal 可选取消信号。
 * @returns 事件详情。
 */
export async function getEvent(id: number, signal?: AbortSignal): Promise<EventDetail> {
  const raw = await http.get<EventDetail>(`/events/${id}`, { signal });
  return {
    ...raw,
    timeline: raw.timeline ?? [],
    latestDiagnosis: raw.latestDiagnosis ?? null,
  };
}

/**
 * 手工登记事件。
 *
 * @param payload 事件写请求。
 * @returns 新事件 ID 与编号。
 */
export async function createEvent(
  payload: EventCreatePayload,
): Promise<{ id: number; eventNo: string; status: string }> {
  // 后端 EventCreateIn 只认 snake_case（asset_id）；Pydantic 会静默忽略未知字段，
  // 直发 assetId 会导致"关联资产"被丢弃且无报错（2026-09-13 UI 穷举测试 BUG-02）。
  return http.post<{ id: number; eventNo: string; status: string }>("/events", {
    title: payload.title,
    severity: payload.severity,
    asset_id: payload.assetId,
    payload: payload.payload ?? null,
  });
}

/**
 * 认领事件（open → acknowledged）。
 *
 * @param id 事件 ID。
 * @returns 流转后的状态。
 */
export async function acknowledgeEvent(id: number): Promise<string> {
  const res = await http.post<{ status: string }>(`/events/${id}/acknowledge`);
  return res?.status ?? "";
}

/**
 * 恢复事件（需处置说明）。
 *
 * @param id 事件 ID。
 * @param note 处置说明。
 * @returns 流转后的状态。
 */
export async function resolveEvent(id: number, note: string): Promise<string> {
  const res = await http.post<{ status: string }>(`/events/${id}/resolve`, { resolution_note: note });
  return res?.status ?? "";
}

/**
 * 关闭事件（终态）。
 *
 * @param id 事件 ID。
 * @param comment 关闭备注。
 * @returns 流转后的状态。
 */
export async function closeEvent(id: number, comment: string): Promise<string> {
  const res = await http.post<{ status: string }>(`/events/${id}/close`, { comment });
  return res?.status ?? "";
}

/**
 * 追加复盘评论。
 *
 * @param id 事件 ID。
 * @param content 评论正文。
 */
export async function addComment(id: number, content: string): Promise<void> {
  await http.post<{ status: string }>(`/events/${id}/comments`, { content });
}

/**
 * 触发 AI 研判（202 受理，后台执行）。
 *
 * @param id 事件 ID。
 * @returns 研判记录 ID（已由后端的 diagnosis_id 转为 camelCase）。
 */
export async function triggerDiagnosis(id: number): Promise<number> {
  const res = await http.post<{ diagnosis_id?: number; diagnosisId?: number }>(`/events/${id}/diagnose`);
  return res?.diagnosisId ?? res?.diagnosis_id ?? 0;
}

/**
 * 取单条研判记录（轮询用）。
 *
 * @param id 研判记录 ID。
 * @param signal 可选取消信号。
 * @returns 研判记录。
 */
export async function getDiagnosis(id: number, signal?: AbortSignal): Promise<Diagnosis> {
  const raw = await http.get<Diagnosis>(`/events/diagnoses/${id}`, { signal });
  return { ...raw, evidence: raw.evidence ?? [] };
}

/**
 * 事件的研判历史（按开始时间倒序，登录可读）。
 *
 * 取代旧实现「扫时间线里出现过的 diagnosis_id 再逐条拉取」的做法：
 * 那个 hack 只能看到时间线里引用过的最多 5 条，且每次进页面要发 N 个请求。
 *
 * @param eventId 事件 ID。
 * @param signal 可选取消信号。
 * @returns 研判记录数组（新→旧）；信封里的 total 对展示无用，此处不返回。
 */
export async function listEventDiagnoses(eventId: number, signal?: AbortSignal): Promise<Diagnosis[]> {
  const raw = await http.get<{ items?: Diagnosis[]; total?: number } | Diagnosis[]>(
    `/events/${eventId}/diagnoses`,
    { signal },
  );
  // 后端约定是信封 {items,total}；顺带容忍直接返回数组的实现，避免多一条失败的联调路径
  const items = Array.isArray(raw) ? raw : (raw?.items ?? []);
  return items.map((item) => ({ ...item, evidence: item.evidence ?? [] }));
}
