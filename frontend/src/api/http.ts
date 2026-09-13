/**
 * 平台 HTTP 客户端。
 *
 * 选 fetch 封装而非 axios：零新依赖（SSE 已用 fetch 流），与既有 api/rag.ts 风格一致。
 *
 * 统一职责：
 * - 拼 `/api/v1` 前缀（可被「设置」里的 apiBaseUrl 覆盖），序列化 query 与 JSON body；
 * - 自动附 `Authorization: Bearer <token>`（token 存 localStorage `fc.token`）；
 * - 401 → 清 token 并跳登录（处理函数由 main.ts 注入，避免与 router 循环依赖）；
 *   403 → 提示「权限不足」（不跳转，用户可能只是缺按钮权限）；
 * - 非 2xx 统一抛 {@link ApiError}，detail 已解析为可直接展示的中文；
 * - 超时（默认取「设置」里的 requestTimeout，60s）。
 */
import { loadAppSettings } from "./appSettings";
import { pushToast } from "@/composables/useToast";

/** token 在 localStorage 的键名（与设计文档一致）。 */
export const TOKEN_KEY = "fc.token";
/** 用户档案缓存键（刷新后先渲染再校验，避免白屏）。 */
export const USER_KEY = "fc.user";
/** 默认 API 前缀。 */
const API_PREFIX = "/api/v1";

/** 带状态码的接口错误。 */
export class ApiError extends Error {
  /** HTTP 状态码；网络/超时失败为 0。 */
  readonly status: number;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
  }
}

// ── token ─────────────────────────────────────────────────────────

/**
 * 读取本地 token。
 *
 * @returns token 字符串；无则空串（隐私模式下 localStorage 不可用时同样返回空串）。
 */
export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

/**
 * 写入/清除本地 token。
 *
 * @param token 新 token；传 null 表示登出。
 */
export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式下写入失败：本次会话仍可用内存态 token */
  }
}

/**
 * 组装鉴权头（rag.ts 的 live 模式复用）。
 *
 * @returns 含 Authorization 的头对象；未登录时为空对象。
 */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// ── 401 处理（登出跳转） ───────────────────────────────────────────

let unauthorizedHandler: (() => void) | null = null;

/**
 * 注册 401 处理函数（main.ts 在 router 就绪后注入）。
 *
 * @param handler 清空登录态并跳转登录页的回调。
 */
export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler;
}

/** 清 token 并跳登录页（无注册处理器时退回 hash 跳转，保证兜底可用）。 */
export function handleUnauthorized(): void {
  setToken(null);
  try {
    localStorage.removeItem(USER_KEY);
  } catch {
    /* 同上 */
  }
  if (unauthorizedHandler) unauthorizedHandler();
  else if (!window.location.hash.startsWith("#/login")) window.location.hash = "#/login";
}

// ── 请求 ──────────────────────────────────────────────────────────

/** query 值类型（null/undefined 一律跳过）。 */
export type QueryValue = string | number | boolean | null | undefined;

/** 请求选项。 */
export interface RequestOptions {
  /** 查询串参数。 */
  query?: Record<string, QueryValue>;
  /** JSON 请求体。 */
  body?: unknown;
  /** 外部取消信号（视图卸载时用）。 */
  signal?: AbortSignal;
  /** 覆盖超时（毫秒）。 */
  timeoutMs?: number;
  /** 失败时是否静默（不弹 toast），用于「探测类」请求。 */
  silent?: boolean;
}

/**
 * 序列化 query。
 *
 * @param query 参数对象。
 * @returns 形如 `?a=1&b=2` 的字符串；无有效参数时为空串。
 */
function buildQuery(query?: Record<string, QueryValue>): string {
  if (!query) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

/**
 * 拼接完整请求地址（apiBaseUrl 为空时走同源，开发环境由 Vite 代理）。
 *
 * @param path 业务路径（如 `/assets`）。
 * @returns 完整 URL。
 */
function buildUrl(path: string): string {
  const base = (loadAppSettings().apiBaseUrl || "").replace(/\/+$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${base}${API_PREFIX}${suffix}`;
}

/**
 * 把后端错误载荷翻译成可展示文本。
 *
 * @param status HTTP 状态码。
 * @param payload 已解析的响应体（可能为 null）。
 * @param rawText 原始文本（解析失败时的兜底）。
 * @returns 中文错误描述。
 */
function describeError(status: number, payload: unknown, rawText: string): string {
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  // FastAPI 校验失败：detail 为 [{loc, msg, type}]
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item as { msg?: string })?.msg)
      .filter((msg): msg is string => Boolean(msg));
    if (messages.length) return messages.join("；");
  }
  if (status === 401) return "登录已过期，请重新登录";
  if (status === 403) return "权限不足，无法执行该操作";
  if (status === 404) return "请求的资源不存在";
  if (status === 409) return "操作冲突，请刷新后重试";
  if (status >= 500) return `服务异常（${status}），请稍后重试`;
  return rawText.slice(0, 160) || `请求失败（${status}）`;
}

/**
 * 发起一次平台 API 请求。
 *
 * @param method HTTP 方法。
 * @param path 业务路径（不含 /api/v1）。
 * @param options 请求选项。
 * @returns 解析后的响应体；204 或空响应返回 null。
 *
 * @throws ApiError 非 2xx、超时或网络失败。
 */
export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? Math.max(1, loadAppSettings().requestTimeout || 60) * 1000;
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const onExternalAbort = (): void => controller.abort();
  options.signal?.addEventListener("abort", onExternalAbort);

  const headers: Record<string, string> = { Accept: "application/json", ...authHeaders() };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  try {
    const response = await fetch(buildUrl(path) + buildQuery(options.query), {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });

    const text = await response.text();
    let payload: unknown = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
    }

    if (!response.ok) {
      if (response.status === 401) {
        // 登录接口的 401 是「凭据错误/账号停用」而非「会话过期」：
        // 透传后端 detail（"用户名或密码错误"/"账号已停用"），且不触发登出跳转。
        // 其它接口的 401 才是会话失效——2026-09-13 UI 穷举测试 P2：
        // 原实现一律替换为"登录已过期"，把后端的准确文案吞掉了。
        if (path.startsWith("/auth/login")) {
          throw new ApiError(401, describeError(response.status, payload, text));
        }
        handleUnauthorized();
        throw new ApiError(401, "登录已过期，请重新登录");
      }
      const message = describeError(response.status, payload, text);
      if (!options.silent) pushToast(message, "error");
      throw new ApiError(response.status, message);
    }
    return (payload as T) ?? (null as T);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if ((error as Error).name === "AbortError") {
      throw new ApiError(0, options.signal?.aborted ? "请求已取消" : "请求超时，请稍后重试");
    }
    throw new ApiError(0, "无法连接后端服务，请确认服务已启动");
  } finally {
    window.clearTimeout(timer);
    options.signal?.removeEventListener("abort", onExternalAbort);
  }
}

/** 便捷方法集合。 */
export const http = {
  get: <T>(path: string, options?: RequestOptions): Promise<T> => request<T>("GET", path, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>("POST", path, { ...options, body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>("PUT", path, { ...options, body }),
  del: <T>(path: string, options?: RequestOptions): Promise<T> => request<T>("DELETE", path, options),
};
