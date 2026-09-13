/**
 * 时间与数值格式化（纯函数，无依赖）。
 *
 * 后端统一返回 ISO8601 字符串（`datetime.isoformat()`，SQLite 兜底路径可能不带时区），
 * 这里全部按本地时区展示，避免出现 "Invalid Date"。
 */

/**
 * 解析后端 ISO 时间串。
 *
 * @param value ISO 字符串。
 * @returns 毫秒时间戳；无法解析时返回 NaN。
 */
export function parseTime(value: string | null | undefined): number {
  if (!value) return Number.NaN;
  const text = value.trim();
  if (!text) return Number.NaN;
  // 无时区标记的串按本地时间解释（后端 SQLite 路径落的是 naive datetime）
  const normalized = /[Zz]|[+-]\d{2}:?\d{2}$/.test(text) ? text : `${text}Z`;
  return Date.parse(normalized);
}

/** 两位补零。 */
function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/**
 * 格式化为 `YYYY-MM-DD HH:mm:ss`。
 *
 * @param value ISO 字符串。
 * @returns 本地时间文本；无效值返回 "—"。
 */
export function formatDateTime(value: string | null | undefined): string {
  const ms = parseTime(value);
  if (!Number.isFinite(ms)) return "—";
  const date = new Date(ms);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/**
 * 格式化为 `MM-DD HH:mm`（表格用短格式）。
 *
 * @param value ISO 字符串。
 * @returns 短时间文本；无效值返回 "—"。
 */
export function formatShortTime(value: string | null | undefined): string {
  const ms = parseTime(value);
  if (!Number.isFinite(ms)) return "—";
  const date = new Date(ms);
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 相对时间（刚刚 / N 分钟前 / N 小时前 / N 天前）。
 *
 * @param value ISO 字符串。
 * @returns 相对时间文本；无效值返回 "—"。
 */
export function formatRelative(value: string | null | undefined): string {
  const ms = parseTime(value);
  if (!Number.isFinite(ms)) return "—";
  const diff = Date.now() - ms;
  if (diff < 0) return formatShortTime(value);
  const minute = 60_000;
  if (diff < minute) return "刚刚";
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`;
  if (diff < 30 * 24 * 60 * minute) return `${Math.floor(diff / (24 * 60 * minute))} 天前`;
  return formatShortTime(value);
}

/**
 * 格式化耗时。
 *
 * @param ms 毫秒。
 * @returns 如 `820ms` / `1.24s`；无值返回 "—"。
 */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/**
 * 格式化置信度。
 *
 * @param value 0..1 之间的数值；null 表示模型未给出。
 * @returns 百分比文本；null 返回 "未知"（不臆造）。
 */
export function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未知";
  return `${Math.round(value * 100)}%`;
}

/**
 * 截断长文本。
 *
 * @param text 原文本。
 * @param max 最大长度。
 * @returns 截断后文本（超长加省略号）。
 */
export function truncate(text: string | null | undefined, max = 80): string {
  const value = text ?? "";
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

/**
 * 格式化字节数（研判证据/附件场景预留）。
 *
 * @param bytes 字节数。
 * @returns 人类可读文本。
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  let value = bytes;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}
