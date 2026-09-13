/**
 * 大模型接入配置的本地持久化。
 *
 * API Key 只写入本机浏览器的 localStorage，不随代码提交、不发送到除所选
 * 服务商之外的任何地址。计费单价以人民币（元 / 百万 tokens）计。
 */

export interface LlmSettings {
  providerId: string;
  apiKey: string;
  model: string;
  baseUrl: string;
  /** 直连模式下先经平台后端检索知识库，把命中资料作为上下文再提问（取不到则不编造资料）。 */
  useRag: boolean;
  /** 采样温度，越低越稳定，运维诊断建议 0-0.3。 */
  temperature: number;
  /** 单次回答的最大输出 tokens。 */
  maxTokens: number;
  /** 检索送入模型的资料条数。 */
  topK: number;
  /** 回答下方是否展示引用来源。 */
  showCitations: boolean;
  /** 回答上方是否展示拆解/检索/反思/生成/校验链路。 */
  showSteps: boolean;
  /** 输入单价：元 / 百万 tokens。 */
  priceIn: number;
  /** 输出单价：元 / 百万 tokens。 */
  priceOut: number;
}

const STORAGE_KEY = "fishcloud.llm.settings.v1";

export function defaultSettings(): LlmSettings {
  return {
    providerId: "",
    apiKey: "",
    model: "",
    baseUrl: "",
    useRag: true,
    temperature: 0.2,
    maxTokens: 2048,
    topK: 4,
    showCitations: true,
    showSteps: true,
    priceIn: 2,
    priceOut: 8,
  };
}

/** 读取配置；无配置或解析失败均返回 null。缺失字段按默认值补齐，便于版本升级。 */
export function loadSettings(): LlmSettings | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LlmSettings>;
    if (!parsed.apiKey && !parsed.baseUrl) return null;
    return { ...defaultSettings(), ...parsed } as LlmSettings;
  } catch {
    return null;
  }
}

export function saveSettings(value: LlmSettings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    /* 隐私模式下写入会失败，静默忽略即可，不影响本次会话使用 */
  }
}

export function clearSettings(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* 同上 */
  }
}

/** 是否具备直连大模型的条件：有 Key（Ollama 可免）且接口地址与模型齐全。 */
export function isReady(value: LlmSettings | null): boolean {
  if (!value) return false;
  if (!value.baseUrl || !value.model) return false;
  if (value.providerId === "ollama") return true;
  return value.apiKey.trim().length > 0;
}

/**
 * 按输入的单价折算本次调用成本，单位为元。
 *
 * @param promptTokens 输入 tokens。
 * @param completionTokens 输出 tokens。
 * @param value 含 priceIn / priceOut 的配置。
 */
export function estimateCost(promptTokens: number, completionTokens: number, value: LlmSettings): number {
  return (promptTokens * value.priceIn + completionTokens * value.priceOut) / 1000000;
}

/** 人民币金额格式化：小额保留 4 位，大额保留 2 位。 */
export function formatYuan(amount: number): string {
  if (!Number.isFinite(amount) || amount <= 0) return "¥0.00";
  return "¥" + (amount < 1 ? amount.toFixed(4) : amount.toFixed(2));
}