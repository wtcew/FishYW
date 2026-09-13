/**
 * 内置大模型服务商预设。
 *
 * 全部使用 OpenAI 兼容的 /chat/completions 协议，因此一套调用代码即可适配；
 * 用户只需选服务商填 Key，模型与接口地址会自动带出（仍可手写覆盖）。
 * 模型列表为查证到的最新版本；各家迭代很快，界面上的模型框可自由输入。
 * 默认单价单位为「元 / 百万 tokens」，仅作成本估算的初始值，可随时改。
 */

export interface Provider {
  id: string;
  name: string;
  baseUrl: string;
  models: string[];
  note?: string;
  priceIn?: number;
  priceOut?: number;
}

export const PROVIDERS: Provider[] = [
  {
    id: "deepseek",
    name: "DeepSeek 深度求索",
    // 官方文档同时列 https://api.deepseek.com 与 /v1 两种写法（OpenAI 兼容路径），
    // 这里保留 /v1 与本文件其它服务商保持一致。
    baseUrl: "https://api.deepseek.com/v1",
    // 当前有效 ID：deepseek-flash（V4.1 Flash，推荐）/ deepseek-v4-pro。
    // 旧的 deepseek-chat、deepseek-reasoner 已于 2026-07-24 停用，
    // deepseek-v4-flash 亦已下线，勿再写入配置。
    models: ["deepseek-flash", "deepseek-v4-pro"],
    note: "deepseek-flash（V4.1 Flash）为推荐主力：1M 上下文 / 384K 最大输出 / 思考模式默认开启",
    // 单价为「元 / 百万 tokens」的成本估算基准，取官方标准（空闲）价：
    // 输入 1 / 输出 4；高峰时段（北京时间工作日 9:00-12:00、14:00-18:00）为 输入 2 / 输出 8。
    // 若常在高峰时段使用，可把这里改成 2 / 8。
    priceIn: 1,
    priceOut: 4,
  },
  {
    id: "dashscope",
    name: "阿里云百炼 · 通义千问",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    models: ["qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash", "qwen-long"],
    priceIn: 2.4,
    priceOut: 9.6,
  },
  {
    id: "zhipu",
    name: "智谱 AI · GLM",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    // 两个免费档（2026-09-13 实测，无需充值资源包），推荐零成本起步：
    // glm-4.7-flash 为默认主力；glm-4-flash 作为备选（glm-4.7-flash 偶发 429 时切）。
    // glm-5.3-flash 等旗舰档需购买资源包，按需在「自定义…」里手填。
    models: ["glm-4.7-flash", "glm-4-flash"],
    note:
      "两个模型均免费：glm-4.7-flash（默认）偶发 429 / 1305「访问量过大」（平台侧繁忙），" +
      "可切 glm-4-flash（实测约 2.8s / 次）",
    priceIn: 0,
    priceOut: 0,
  },
  {
    id: "moonshot",
    name: "Moonshot · Kimi",
    baseUrl: "https://api.moonshot.cn/v1",
    models: ["kimi-k3", "kimi-latest", "kimi-k2-thinking", "moonshot-v1-128k"],
    priceIn: 4,
    priceOut: 16,
  },
  {
    id: "siliconflow",
    name: "硅基流动 SiliconFlow",
    baseUrl: "https://api.siliconflow.cn/v1",
    models: ["deepseek-ai/DeepSeek-V4-Flash", "Qwen/Qwen3.8-Max", "moonshotai/Kimi-K3"],
    note: "模型 ID 形如 组织/模型名，可在模型广场复制准确名称",
    priceIn: 1,
    priceOut: 4,
  },
  {
    id: "ark",
    name: "火山方舟 · 豆包",
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    models: ["doubao-seed-2-1-turbo", "doubao-pro-32k"],
    note: "方舟通常要求填写自行创建的接入点 ID（ep- 开头）",
    priceIn: 0.8,
    priceOut: 2,
  },
  {
    id: "openai",
    name: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    models: ["gpt-5.5", "gpt-5.4-mini", "gpt-5.4-nano", "o4-mini"],
    priceIn: 18,
    priceOut: 72,
  },
  {
    id: "openrouter",
    name: "OpenRouter",
    baseUrl: "https://openrouter.ai/api/v1",
    models: ["deepseek/deepseek-chat", "openai/gpt-5.5", "anthropic/claude-sonnet-4.5"],
    note: "聚合网关，服务端允许浏览器直连；模型 ID 形如 厂商/模型",
    priceIn: 7,
    priceOut: 28,
  },
  {
    id: "ollama",
    name: "本地 Ollama",
    baseUrl: "http://localhost:11434/v1",
    models: ["qwen3:8b", "deepseek-r1:7b", "llama3.2:3b"],
    note: "本机推理不产生费用，Key 可留空",
    priceIn: 0,
    priceOut: 0,
  },
  {
    id: "custom",
    name: "自定义（OpenAI 兼容）",
    baseUrl: "",
    models: [],
    note: "自行填写接口地址与模型名",
    priceIn: 2,
    priceOut: 8,
  },
];

/** 按 id 取服务商预设。 */
export function findProvider(id: string): Provider | undefined {
  return PROVIDERS.find((item) => item.id === id);
}