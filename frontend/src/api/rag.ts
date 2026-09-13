/**
 * 问答客户端：两条真实执行路径，无任何本地假答案。
 *
 * 1. direct —— 用户在设置里填了 API Key，直接调用所选服务商的 OpenAI 兼容接口。
 *    若开启「先检索知识库」，会用平台 `POST /api/v1/retrieve` 的真实命中结果作为上下文；
 *    取不到上下文时在状态行明说，仅用模型自有知识作答（不编造资料）。
 * 2. live   —— 调用本项目后端 `POST /api/v1/diagnose`（SSE 流式）。
 *
 * 两条路径都失败时**直接报错并给出可操作提示**：运维场景里一个伪造的根因
 * 比一条明确的错误危险得多，因此历史版本里的「本地演示答案」分支已整体移除。
 */
import { retrieve as retrieveFromBackend } from "./aiops";
import { authHeaders, handleUnauthorized } from "./http";
import { estimateCost, isReady, type LlmSettings } from "./settings";

/** 执行路径（不再有本地演示）。 */
export type RagMode = "direct" | "live";

export interface Citation {
  /** 后端引用的 chunk_id；直连模式下按需构造。 */
  id?: string;
  title?: string;
  score: number;
  /** 以下字段由后端结构化引用返回，用于定位原文与高亮。 */
  chunk_id?: string;
  doc_id?: string;
  kb_id?: string;
  filename?: string;
  chunk_index?: number;
  snippet?: string;
  content?: string;
  char_start?: number | null;
  char_end?: number | null;
  locatable?: boolean;
  degrade_reason?: string;
}

export interface RagHandlers {
  onSubQuestions?: (items: string[]) => void;
  onRetrieve?: (info: { docCount: number; topScore: number }) => void;
  onReflect?: (info: { sufficient: boolean; missing: string[] }) => void;
  /** isStream=true 表示这是累积中的流式文本，界面应直接替换；否则按整段处理。 */
  onAnswer?: (text: string, isStream?: boolean) => void;
  onHallucination?: (info: { hasHallucination: boolean; sentences: string[] }) => void;
  onCitations?: (items: Citation[]) => void;
  onStatus?: (text: string) => void;
  /** 本次调用的 token 用量与折算人民币成本；estimated=true 表示按字符数估算。 */
  onUsage?: (info: {
    promptTokens: number;
    completionTokens: number;
    cost: number;
    estimated: boolean;
  }) => void;
  onDone?: (info: { elapsedMs: number; traceId: string; mode: RagMode }) => void;
  onError?: (message: string) => void;
}

export interface AskOptions {
  topK?: number;
  rerankK?: number;
  useCache?: boolean;
  signal?: AbortSignal;
  /** 已保存的大模型配置；就绪时走直连。 */
  direct?: LlmSettings | null;
  /** 历史问答，用于多轮上下文。 */
  history?: Array<{ role: string; content: string }>;
}

const ENDPOINT = "/api/v1/diagnose";

/**
 * 组装对话消息。
 *
 * @param query 用户问题。
 * @param context 真实检索到的资料文本（可能为空）。
 * @param history 历史轮次。
 * @returns OpenAI 兼容的消息数组。
 */
function buildMessages(
  query: string,
  context: string,
  history: Array<{ role: string; content: string }> = [],
): Array<{ role: string; content: string }> {
  const system = context
    ? "你是工业设备运维诊断专家。请严格基于用户提供的「参考资料」作答：\n" +
      "1. 结论先行，按可能性从高到低排序；\n" +
      "2. 每条结论注明依据（可引用资料编号）；\n" +
      "3. 资料未覆盖的部分必须说明是推断，不得编造设备型号、参数或数据；\n" +
      "4. 最后给出可执行的排查顺序。"
    : "你是工业设备运维诊断专家。当前没有检索到任何参考资料，" +
      "请只依据你的通用知识作答，并在开头明确说明「本次回答未经知识库资料支持」，不得编造设备型号、参数或数据。";
  return [...history, { role: "system", content: system }, { role: "user", content: query }];
}

/** 计算实际请求地址：开发环境经 Vite 同源代理转发，规避浏览器 CORS 限制。 */
function endpointFor(settings: LlmSettings): string {
  const base = settings.baseUrl.replace(/\/+$/, "");
  if (import.meta.env.DEV) {
    try {
      const url = new URL(base);
      return "/llm-proxy/" + url.host + url.pathname.replace(/\/+$/, "");
    } catch {
      return base;
    }
  }
  return base;
}

/** 把 HTTP 状态翻译成用户能照做的提示。 */
function describeError(status: number, body: string): string {
  if (status === 401 || status === 403) return "API Key 无效或没有该模型的权限（" + status + "），请检查设置里的密钥";
  if (status === 402) return "账户余额不足（402），请先充值";
  if (status === 404) return "接口地址或模型名不正确（404），请核对设置里的接口地址与模型";
  if (status === 429) return "请求过于频繁或超出配额（429），请稍后重试";
  if (status >= 500) return "服务商暂时不可用（" + status + "），请稍后重试";
  return "请求失败（" + status + "）：" + body.slice(0, 140);
}

/**
 * 取真实知识库上下文（平台混合检索）。
 *
 * @param query 查询文本。
 * @param topK 召回条数。
 * @returns 命中列表；后端不可用时返回 null（调用方负责在状态行说明）。
 */
async function fetchContext(query: string, topK: number): Promise<Citation[] | null> {
  try {
    const result = await retrieveFromBackend({ query, topK });
    return result.documents.map((item) => ({
      chunk_id: item.chunk_id,
      doc_id: item.doc_id,
      kb_id: item.kb_id,
      filename: item.filename,
      title: item.filename || item.doc_id,
      chunk_index: item.chunk_index,
      score: item.score,
      snippet: item.snippet,
      content: item.content,
      char_start: item.char_start,
      char_end: item.char_end,
      locatable: item.locatable,
      degrade_reason: item.degrade_reason,
    }));
  } catch {
    return null;
  }
}

/** 直连所选服务商（OpenAI 兼容协议）；上下文来自真实检索或为空。 */
async function askDirect(
  query: string,
  settings: LlmSettings,
  h: RagHandlers,
  opts: AskOptions,
): Promise<void> {
  const started = Date.now();
  let citations: Citation[] = [];
  let context = "";

  if (settings.useRag) {
    h.onStatus?.("正在检索知识库…");
    const hits = await fetchContext(query, settings.topK || 4);
    if (hits === null) {
      h.onStatus?.("知识库检索不可用（后端未就绪），本次仅使用模型自有知识");
    } else if (hits.length) {
      citations = hits;
      context = hits.map((x, i) => `[${i + 1}] ${x.filename || x.doc_id}\n${x.content ?? x.snippet ?? ""}`).join("\n\n");
      h.onRetrieve?.({ docCount: hits.length, topScore: hits[0]?.score ?? 0 });
      h.onCitations?.(hits);
    } else {
      h.onStatus?.("知识库没有命中资料，本次仅使用模型自有知识");
    }
  }

  h.onSubQuestions?.([query]);
  h.onStatus?.("正在请求 " + settings.model + " …");

  const messages = buildMessages(query, context, opts.history ?? []);
  const payload = (withUsage: boolean) =>
    JSON.stringify({
      model: settings.model,
      messages,
      stream: true,
      temperature: settings.temperature,
      max_tokens: settings.maxTokens,
      ...(withUsage ? { stream_options: { include_usage: true } } : {}),
    });
  const send = (withUsage: boolean) =>
    fetch(endpointFor(settings) + "/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + (settings.apiKey || "ollama"),
      },
      body: payload(withUsage),
      signal: opts.signal,
    });

  let res = await send(true);
  // 少数厂商不认 stream_options，退一步去掉该参数重发一次。
  if (res.status === 400) res = await send(false);

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(describeError(res.status, text));
  }
  if (!res.body) throw new Error("服务商未返回流式响应体");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let full = "";
  let usagePrompt = 0;
  let usageCompletion = 0;
  let usageReal = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n");
    while (cut >= 0) {
      const line = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 1);
      cut = buffer.indexOf("\n");
      if (!line.startsWith("data:")) continue;
      const chunk = line.slice(5).trim();
      if (!chunk || chunk === "[DONE]") continue;
      try {
        const json = JSON.parse(chunk) as {
          choices?: Array<{ delta?: { content?: string } }>;
          error?: { message?: string };
          usage?: { prompt_tokens?: number; completion_tokens?: number };
        };
        if (json.error?.message) throw new Error(json.error.message);
        if (json.usage) {
          usagePrompt = Number(json.usage.prompt_tokens ?? 0);
          usageCompletion = Number(json.usage.completion_tokens ?? 0);
          usageReal = true;
        }
        const delta = json.choices?.[0]?.delta?.content;
        if (delta) {
          full += delta;
          h.onAnswer?.(full, true);
        }
      } catch (err) {
        if (err instanceof Error && err.message && !err.message.includes("JSON")) throw err;
      }
    }
  }
  if (!full) throw new Error("模型没有返回内容，请确认模型名是否正确、该模型是否支持对话");
  // 多数厂商默认不在流式响应里返回用量，此时按字符数折算并标注为估算值。
  const estimated = !usageReal;
  if (estimated) {
    usagePrompt = Math.ceil(JSON.stringify(messages).length / 1.6);
    usageCompletion = Math.ceil(full.length / 1.6);
  }
  h.onUsage?.({
    promptTokens: usagePrompt,
    completionTokens: usageCompletion,
    cost: estimateCost(usagePrompt, usageCompletion, settings),
    estimated,
  });
  h.onStatus?.("已连接 " + settings.model);
  h.onDone?.({
    elapsedMs: Date.now() - started,
    traceId: "direct-" + Math.random().toString(16).slice(2, 8),
    mode: "direct",
  });
}

/** 消费后端 SSE 流并分发事件。 */
async function consume(
  body: ReadableStream<Uint8Array>,
  h: RagHandlers,
  started: number,
  mode: RagMode,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let traceId = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      cut = buffer.indexOf("\n\n");
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(data) as Record<string, unknown>;
      } catch {
        continue;
      }
      if (payload.traceId) traceId = String(payload.traceId);
      switch (event) {
        case "decompose":
          h.onSubQuestions?.((payload.subQuestions as string[]) ?? []);
          break;
        case "retrieve":
          h.onRetrieve?.({ docCount: Number(payload.docCount ?? 0), topScore: Number(payload.topScore ?? 0) });
          if (Array.isArray(payload.documents)) h.onCitations?.(payload.documents as Citation[]);
          break;
        case "reflect":
          h.onReflect?.({ sufficient: Boolean(payload.sufficient), missing: (payload.missing as string[]) ?? [] });
          break;
        case "generate":
          h.onAnswer?.(String(payload.delta ?? ""), false);
          break;
        case "hallucination":
          h.onHallucination?.({
            hasHallucination: Boolean(payload.hasHallucination),
            sentences: (payload.sentences as string[]) ?? [],
          });
          break;
        case "error":
          // 后端 error 载荷为 {error, detail}（routes.py），兼容 message 以防字段演进
          h.onError?.(String(payload.detail ?? payload.message ?? "服务端返回错误"));
          break;
        default:
          break;
      }
    }
  }
  h.onDone?.({ elapsedMs: Date.now() - started, traceId, mode });
}

/**
 * 用一次最小请求验证配置是否连通，供设置面板的「测试连接」使用。
 *
 * @param settings 待验证的配置。
 */
export async function testConnection(settings: LlmSettings): Promise<{ ok: boolean; message: string }> {
  try {
    const res = await fetch(endpointFor(settings) + "/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + (settings.apiKey || "ollama"),
      },
      body: JSON.stringify({
        model: settings.model,
        messages: [{ role: "user", content: "ping" }],
        max_tokens: 1,
        stream: false,
      }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      return { ok: false, message: describeError(res.status, text) };
    }
    return { ok: true, message: "连接正常，模型可用" };
  } catch (err) {
    const msg = (err as Error).message || "";
    if (msg.includes("Failed to fetch")) {
      return { ok: false, message: "无法连接：请检查接口地址，或该服务商不允许浏览器直连" };
    }
    return { ok: false, message: "连接失败：" + msg };
  }
}

/**
 * 发起一次问答：配置了模型则直连，否则走后端 SSE。
 *
 * 两条路径都失败时报错，不做任何形式的本地兜底 —— 伪造的根因在运维现场有害。
 */
export async function askRag(query: string, h: RagHandlers, opts: AskOptions = {}): Promise<void> {
  const started = Date.now();
  if (isReady(opts.direct ?? null)) {
    try {
      await askDirect(query, opts.direct as LlmSettings, h, opts);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      h.onError?.((err as Error).message || "直连大模型失败");
      h.onDone?.({ elapsedMs: Date.now() - started, traceId: "", mode: "direct" });
    }
    return;
  }

  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      // 平台后端已开启 RBAC：/diagnose 属登录后接口，必须带 Bearer 令牌。
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...authHeaders() },
      body: JSON.stringify({
        query,
        topK: opts.topK ?? 15,
        rerankK: opts.rerankK ?? 5,
        useCache: opts.useCache ?? true,
        // 多轮上下文必须真的发出去：前端存了会话历史却没带在请求里，
        // 后端只能把每句追问当成孤立问题，指代与省略全部丢失。
        history: (opts.history ?? []).slice(-12),
      }),
      signal: opts.signal,
    });
    // 401 先处理登录态，不能转换成「后端不可用」这类误导性提示。
    if (res.status === 401) {
      handleUnauthorized();
      h.onError?.("登录已过期，请重新登录后再试");
      h.onDone?.({ elapsedMs: Date.now() - started, traceId: "", mode: "live" });
      return;
    }
    if (!res.ok || !res.body) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}${detail ? "：" + detail.slice(0, 160) : ""}`);
    }
    await consume(res.body, h, started, "live");
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    h.onError?.(
      "后端推理不可用（" +
        ((err as Error).message || "未知错误") +
        "）。请确认后端服务已启动，或在「设置」中配置模型凭据后改用直连模式。",
    );
    h.onDone?.({ elapsedMs: Date.now() - started, traceId: "", mode: "live" });
  }
}
