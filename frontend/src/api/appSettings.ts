/**
 * 系统全局设置。
 *
 * 与「模型设置」（api/settings.ts，只管大模型接入）分开：这里管理服务连接、
 * 检索、Agent、评估、告警、界面与数据策略。
 *
 * 设计上处理了这几类常见问题：
 * 1. 版本升级导致旧配置缺字段 —— 读取时按默认值补齐，不丢用户已有设置；
 * 2. 隐私模式 / 配额写满导致写入失败 —— 捕获异常，不阻断本次会话；
 * 3. 导入非法或异构配置 —— 逐字段类型校验，只接受认识的键，返回失败原因；
 * 4. 前端配置与后端进程割裂 —— 可导出为环境变量文本，直接用于启动后端。
 */

export type ThemeMode = "light" | "dark" | "auto";
export type Density = "compact" | "normal" | "relaxed";
/** 品牌配色方案（与明暗主题正交，见 styles/tokens.css 的 html[data-brand]）。 */
export type BrandTheme = "blue-pink" | "teal" | "zhu";

export interface AppSettings {
  version: number;
  /** 后端 API 根地址；留空表示走同源 /api（开发环境由 Vite 代理转发）。 */
  apiBaseUrl: string;
  /** 后端健康检查间隔（秒），0 表示不自动检查。 */
  healthInterval: number;
  /** 请求超时（秒）。 */
  requestTimeout: number;

  /** 召回候选数量。 */
  topK: number;
  /** 重排后保留数量。 */
  rerankK: number;
  /** RRF 融合参数 k。 */
  rrfK: number;
  useVector: boolean;
  useBm25: boolean;
  useGraph: boolean;
  /** 结果最低相关度，低于该值不送入生成。 */
  minScore: number;

  /** Agent 反思循环最大迭代次数。 */
  maxIterations: number;
  /** 幻觉重试上限。 */
  maxHallucinationRetry: number;
  /** 语义缓存开关与 TTL（秒）。 */
  useCache: boolean;
  cacheTtl: number;
  /** 单个节点超时（秒）。 */
  nodeTimeout: number;

  /** 黄金测试集规模。 */
  goldenSize: number;
  /** 参与评估的指标。 */
  metrics: string[];

  /** 告警 Webhook 与阈值。 */
  alertsEnabled: boolean;
  webhookUrl: string;
  latencyThreshold: number;
  hallucinationThreshold: number;
  cacheHitThreshold: number;

  /** 界面。 */
  theme: ThemeMode;
  /** 品牌配色：blue-pink（默认）/ teal（青绿备选）/ zhu（朱砂原版）。 */
  brand: BrandTheme;
  fontScale: number;
  density: Density;
  reduceMotion: boolean;
  /** 启动后默认落地页。 */
  defaultPage: string;

  /** 是否把每次诊断链路写入本地（仅浏览器内存/存储，不落服务器磁盘）。 */
  keepTrace: boolean;
}

export const SETTINGS_VERSION = 1;
const STORAGE_KEY = "fishcloud.app.settings.v1";

export const METRIC_OPTIONS = ["faithfulness", "answer_relevancy", "context_recall", "context_precision"];

/**
 * 品牌配色候选（value 与 html[data-brand] 及 tokens.css 的品牌块一一对应）。
 *
 * themeColor 用于同步 `<meta name="theme-color">`（移动端地址栏/状态栏着色）。
 */
export const BRAND_OPTIONS: Array<{ value: BrandTheme; label: string; swatch: string; themeColor: string }> = [
  { value: "blue-pink", label: "蓝粉", swatch: "linear-gradient(135deg,#3B82F6 0%,#2563EB 52%,#EC4899 100%)", themeColor: "#2563EB" },
  { value: "teal", label: "青绿", swatch: "linear-gradient(135deg,#2DD4BF 0%,#0D9488 55%,#06B6D4 100%)", themeColor: "#0D9488" },
  { value: "zhu", label: "朱砂", swatch: "linear-gradient(135deg,#C74B3A 0%,#B03A2E 60%,#B8860B 100%)", themeColor: "#B03A2E" },
];

/**
 * 启动后默认落地页候选（value 与 router 的路由 name 对齐）。
 *
 * 仅列登录后可见的主页面；详情页与管理员专属页不作为落地页。
 * 旧值（pipeline/monitor/feedback/stack/alerts/trace）对应的演示视图已随
 * Phase 1 平台改造删除，此处同步收敛，避免设置里选出空白页。
 */
export const PAGE_OPTIONS = [
  { value: "overview", label: "运行概览" },
  { value: "asset-list", label: "资产登记" },
  { value: "event-list", label: "事件与研判" },
  { value: "aiops-chat", label: "智能问答" },
  { value: "aiops-agent", label: "Agent 链路" },
  { value: "aiops-retrieval", label: "混合检索" },
  { value: "aiops-knowledge", label: "知识库" },
  { value: "aiops-eval", label: "效果评估" },
  { value: "observability", label: "指标与日志" },
  { value: "tasks", label: "任务与 Playbook" },
  { value: "tickets", label: "工单与审批" },
  { value: "settings", label: "设置" },
];

export function defaultAppSettings(): AppSettings {
  return {
    version: SETTINGS_VERSION,
    apiBaseUrl: "",
    healthInterval: 30,
    requestTimeout: 60,
    topK: 15,
    rerankK: 5,
    rrfK: 60,
    useVector: true,
    useBm25: true,
    useGraph: true,
    minScore: 0.5,
    maxIterations: 3,
    maxHallucinationRetry: 2,
    useCache: true,
    cacheTtl: 300,
    nodeTimeout: 30,
    goldenSize: 200,
    metrics: [...METRIC_OPTIONS],
    alertsEnabled: true,
    webhookUrl: "",
    latencyThreshold: 3000,
    hallucinationThreshold: 5,
    cacheHitThreshold: 25,
    theme: "light",
    brand: "blue-pink",
    fontScale: 1,
    density: "normal",
    reduceMotion: false,
    defaultPage: "overview",
    keepTrace: true,
  };
}

/** 读取设置；缺字段按默认补齐，未知键丢弃。 */
export function loadAppSettings(): AppSettings {
  const base = defaultAppSettings();
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return base;
    return mergeSettings(base, JSON.parse(raw));
  } catch {
    return base;
  }
}

/**
 * 把任意来源的对象合并进默认设置：只取类型匹配的已知键。
 *
 * @param base 默认设置。
 * @param input 待合并的对象。
 */
export function mergeSettings(base: AppSettings, input: unknown): AppSettings {
  if (!input || typeof input !== "object") return base;
  const source = input as Record<string, unknown>;
  const out = { ...base } as unknown as Record<string, unknown>;
  for (const key of Object.keys(base)) {
    if (!(key in source)) continue;
    const want = (base as unknown as Record<string, unknown>)[key];
    const got = source[key];
    if (Array.isArray(want)) {
      if (Array.isArray(got)) out[key] = got.filter((x) => typeof x === "string");
      continue;
    }
    if (typeof want === typeof got) out[key] = got;
  }
  out.version = SETTINGS_VERSION;
  return out as unknown as AppSettings;
}

export function saveAppSettings(value: AppSettings): boolean {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

export function clearAppSettings(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* 隐私模式下忽略 */
  }
}

/** 导出全部本地配置（含模型配置）为可读 JSON 文本。 */
export function exportAll(app: AppSettings, llm: unknown): string {
  return JSON.stringify({ exportedAt: new Date().toISOString(), app, llm }, null, 2);
}

/**
 * 解析导入的配置文本。
 *
 * @returns ok=true 时带合并后的设置；否则带可直接展示给用户的失败原因。
 */
export function importAll(text: string): { ok: boolean; app?: AppSettings; llm?: unknown; error?: string } {
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return { ok: false, error: "内容不是有效的配置对象" };
    const app = mergeSettings(defaultAppSettings(), parsed.app ?? parsed);
    return { ok: true, app, llm: parsed.llm };
  } catch (err) {
    return { ok: false, error: "解析失败：" + (err as Error).message };
  }
}

/**
 * 生成后端启动所需的环境变量文本。
 *
 * 前端设置与后端进程是两个世界，这一步把界面上的检索/Agent 参数导出成
 * 可直接粘贴到启动命令前的环境变量，避免两处配置各说各话。
 */
export function toBackendEnv(app: AppSettings, llm: { apiKey?: string; model?: string; baseUrl?: string } | null): string {
  const lines = [
    "# 后端启动环境变量（PowerShell：逐行 $env:NAME='value'；Linux/macOS：export NAME=value）",
  ];
  if (llm?.apiKey) lines.push("DEEPSEEK_API_KEY=" + llm.apiKey);
  if (llm?.model) lines.push("DEEPSEEK_MODEL_NAME=" + llm.model);
  if (llm?.baseUrl) lines.push("DEEPSEEK_BASE_URL=" + llm.baseUrl);
  lines.push("TOP_K_RETRIEVAL=" + app.topK);
  lines.push("TOP_K_RERANK=" + app.rerankK);
  lines.push("MAX_AGENT_ITERATIONS=" + app.maxIterations);
  lines.push("# 模型权重缓存位置（避免联网下载）");
  lines.push("HF_HOME=D:\\hf-cache");
  lines.push("HF_HUB_OFFLINE=1");
  return lines.join("\n");
}

/** 应用界面类设置到文档根节点（主题、字号、密度、动效）。 */
export function applyAppearance(app: AppSettings): void {
  const root = document.documentElement;
  const prefersDark =
    typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia("(prefers-color-scheme: dark)").matches
      : false;
  const theme = app.theme === "auto" ? (prefersDark ? "dark" : "light") : app.theme;
  root.setAttribute("data-theme", theme);
  // 品牌配色与明暗主题正交：非法值一律回落 blue-pink（导入配置可能带脏值）。
  const brand = BRAND_OPTIONS.some((item) => item.value === app.brand) ? app.brand : "blue-pink";
  root.setAttribute("data-brand", brand);
  root.style.setProperty("--font-scale", String(app.fontScale));
  root.setAttribute("data-density", app.density);
  root.setAttribute("data-reduce-motion", app.reduceMotion ? "on" : "off");
  syncThemeColor(brand);
}

/**
 * 同步浏览器 UI 主题色（移动端地址栏 / 状态栏）。
 *
 * @param brand 品牌方案。
 */
export function syncThemeColor(brand: BrandTheme): void {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) return;
  const hit = BRAND_OPTIONS.find((item) => item.value === brand);
  meta.setAttribute("content", hit?.themeColor ?? "#2563EB");
}