/**
 * 会话历史。
 *
 * 对标同类产品（Dify / FastGPT / RAGFlow 都支持多会话与历史回顾），
 * 这里把「对话」提升为一等对象：可新建、切换、重命名、删除，并整体持久化。
 *
 * 存储策略：localStorage，最多保留 MAX_SESSIONS 个会话，超出按更新时间淘汰；
 * 单条消息文本超过 MAX_TEXT 时截断，避免撑爆浏览器存储配额。
 */
import type { Citation, RagMode } from "./rag";

export interface MessageMeta {
  subQuestions: string[];
  docCount: number;
  topScore: number;
  sufficient: boolean;
  missing: string[];
  hasHallucination: boolean;
  sentences: string[];
  elapsedMs: number;
  traceId: string;
  mode: RagMode | "";
  status: string;
  promptTokens: number;
  completionTokens: number;
  cost: number;
  costEstimated: boolean;
  /** 用户中途停止生成：保留已输出部分，界面与「正常答完」区分开。 */
  stopped: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations: Citation[];
  state: "streaming" | "done" | "error";
  error: string;
  meta: MessageMeta;
  createdAt: number;
}

export interface ChatSession {
  id: string;
  title: string;
  messages: ChatMessage[];
  createdAt: number;
  updatedAt: number;
}

const STORAGE_KEY = "fishcloud.chat.sessions.v1";
const MAX_SESSIONS = 50;
const MAX_TEXT = 20000;

export function emptyMeta(): MessageMeta {
  return {
    subQuestions: [], docCount: 0, topScore: 0, sufficient: false, missing: [],
    hasHallucination: false, sentences: [], elapsedMs: 0, traceId: "", mode: "", status: "",
    promptTokens: 0, completionTokens: 0, cost: 0, costEstimated: false, stopped: false,
  };
}

export function uid(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
}

export function createSession(): ChatSession {
  const now = Date.now();
  return { id: uid(), title: "新对话", messages: [], createdAt: now, updatedAt: now };
}

/** 用首条提问生成会话标题。 */
export function titleFrom(text: string): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > 24 ? clean.slice(0, 24) + "…" : clean || "新对话";
}

/** 读取全部会话（按更新时间倒序）。 */
export function loadSessions(): ChatSession[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as ChatSession[];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((x) => x && typeof x.id === "string" && Array.isArray(x.messages))
      .map((x) => ({
        ...x,
        messages: x.messages.map((m) => ({ ...m, meta: { ...emptyMeta(), ...(m.meta ?? {}) } })),
      }))
      .sort((a, b) => b.updatedAt - a.updatedAt);
  } catch {
    return [];
  }
}

/** 写回会话列表；超出上限时按更新时间淘汰，单条文本超长时截断。 */
export function saveSessions(list: ChatSession[]): boolean {
  try {
    const trimmed = [...list]
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_SESSIONS)
      .map((s) => ({
        ...s,
        messages: s.messages.map((m) => ({
          ...m,
          text: m.text.length > MAX_TEXT ? m.text.slice(0, MAX_TEXT) + "\n…（已截断）" : m.text,
        })),
      }));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
    return true;
  } catch {
    return false;
  }
}

export function clearSessions(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* 隐私模式忽略 */
  }
}

/** 取最近若干轮问答，作为多轮对话上下文回传给模型。 */
export function recentContext(messages: ChatMessage[], maxTurns = 6): Array<{ role: string; content: string }> {
  const usable = messages.filter((m) => m.state === "done" && m.text.trim());
  const slice = usable.slice(-maxTurns * 2);
  return slice.map((m) => ({
    role: m.role === "user" ? "user" : "assistant",
    content: m.text.length > 1500 ? m.text.slice(0, 1500) : m.text,
  }));
}