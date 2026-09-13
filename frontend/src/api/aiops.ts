/**
 * AIOps 引擎接口：混合检索 / 评估 / 链路追踪 / 人工反馈。
 *
 * 契约来源 `src/api/routes.py`：
 * - POST /retrieve          {query, topK?, rerankK?} → {query, count, documents:[citation]}
 *   citation 字段：doc_id/chunk_id/kb_id/filename/chunk_index/score/snippet/content/
 *   char_start/char_end/locatable/degrade_reason（缺坐标时 locatable=false）
 * - POST /evaluate          → {status, evaluation_status}（后台执行，需轮询）
 * - GET  /evaluate/results  → {status: "no_result"|"ok", evaluation_status, overall_metrics?…}
 * - GET  /trace/{trace_id}  → {found, trace_id, query?, answer?, sources?, iterations?, elapsed_ms?, error?}
 *   （明细存内存，最多 200 条，重启即失效）
 * - POST /feedback          {trace_id, rating, comment?} → {status, total}
 *
 * 这些都是登录后接口（平台 RBAC 已开启），统一经 api/http 带 Bearer 令牌。
 */
import { http } from "./http";

/** 检索命中的结构化引用（与后端 _citation 一一对应）。 */
export interface RetrievalHit {
  doc_id: string;
  chunk_id: string;
  kb_id: string;
  filename: string;
  chunk_index: number;
  score: number;
  snippet: string;
  content: string;
  char_start: number | null;
  char_end: number | null;
  locatable: boolean;
  degrade_reason: string;
}

/** 检索结果。 */
export interface RetrieveResult {
  query: string;
  count: number;
  documents: RetrievalHit[];
}

/**
 * 执行一次混合检索（仅检索，不生成答案）。
 *
 * @param params 查询与调参。
 * @param signal 可选取消信号。
 * @returns 命中列表与统计。
 */
export async function retrieve(
  params: { query: string; topK?: number; rerankK?: number },
  signal?: AbortSignal,
): Promise<RetrieveResult> {
  const raw = await http.post<Partial<RetrieveResult>>(
    "/retrieve",
    { query: params.query, topK: params.topK ?? null, rerankK: params.rerankK ?? null },
    { signal },
  );
  return {
    query: raw?.query ?? params.query,
    count: raw?.count ?? 0,
    documents: raw?.documents ?? [],
  };
}

/** 评估结果（后端字段为 snake_case，此处如实保留）。 */
export interface EvalResult {
  /** ok = 有结果；no_result = 尚未评估。 */
  status: string;
  /** 后台任务状态：idle / running / done / failed。 */
  evaluation_status?: string;
  overall_metrics?: Record<string, number>;
  single_hop_metrics?: Record<string, number>;
  multi_hop_metrics?: Record<string, number>;
  passed?: boolean;
  sample_count?: number;
  error?: string;
}

/**
 * 触发后台评估任务（202 受理）。
 *
 * @returns 任务状态与当前评估状态。
 */
export async function triggerEvaluate(): Promise<{ status: string; evaluation_status: string }> {
  const raw = await http.post<{ status?: string; evaluation_status?: string }>("/evaluate");
  return { status: raw?.status ?? "running", evaluation_status: raw?.evaluation_status ?? "idle" };
}

/**
 * 读取评估结果（前端轮询用）。
 *
 * @param signal 可选取消信号。
 * @returns 评估结果或 no_result。
 */
export async function fetchEvaluateResults(signal?: AbortSignal): Promise<EvalResult> {
  const raw = await http.get<EvalResult>("/evaluate/results", { signal, silent: true });
  return { ...raw, status: raw?.status ?? "no_result" };
}

/** 链路记录（内存态，重启即失效）。 */
export interface TraceRecord {
  found: boolean;
  trace_id: string;
  query?: string;
  answer?: string;
  sources?: RetrievalHit[];
  iterations?: number;
  elapsed_ms?: number;
  error?: string | null;
}

/**
 * 按 trace_id 取链路明细。
 *
 * @param traceId 链路标识（问答/研判结果里返回）。
 * @returns 链路记录；未找到时 found=false（不抛错，由界面提示）。
 */
export async function fetchTrace(traceId: string): Promise<TraceRecord> {
  const raw = await http.get<TraceRecord>(`/trace/${encodeURIComponent(traceId)}`, { silent: true });
  return { ...raw, found: raw?.found ?? false, trace_id: raw?.trace_id ?? traceId };
}

/**
 * 提交人工反馈（用于评估与调优，落内存）。
 *
 * @param payload 反馈内容。
 * @returns 接收状态与累计条数。
 */
export async function sendFeedback(payload: {
  traceId: string;
  rating: string;
  comment?: string;
}): Promise<{ status: string; total: number }> {
  const raw = await http.post<{ status?: string; total?: number }>("/feedback", {
    trace_id: payload.traceId,
    rating: payload.rating,
    comment: payload.comment ?? "",
  });
  return { status: raw?.status ?? "ok", total: raw?.total ?? 0 };
}
