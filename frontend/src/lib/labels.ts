/**
 * 领域枚举 → 界面文案/配色映射（纯函数，无依赖）。
 *
 * 取值集合来自后端：严重度与状态见 `src/platform/services/events.py`
 * （SEVERITIES / TRANSITIONS），研判状态见 `src/platform/models/diagnosis.py`，
 * 资源类型与状态见 `src/platform/schemas.py` 的 Literal 白名单。
 */

/** 严重度四档（后端 critical/major/minor/info ↔ 现场 P1..P4）。 */
export const SEVERITY_LABELS: Record<string, string> = {
  critical: "P1 严重",
  major: "P2 重要",
  minor: "P3 次要",
  info: "P4 提示",
};

/** 严重度对应的徽章修饰类（实底仅 P1 与「运行中」使用）。 */
export const SEVERITY_BADGE: Record<string, string> = {
  critical: "bdg--danger-solid",
  major: "bdg--warning",
  minor: "bdg--info",
  info: "bdg--neutral",
};

/** 事件状态（状态机当前态）。 */
export const EVENT_STATUS_LABELS: Record<string, string> = {
  open: "待处置",
  acknowledged: "已认领",
  diagnosing: "研判中",
  resolved: "已恢复",
  closed: "已关闭",
};

/** 事件状态配色。 */
export const EVENT_STATUS_BADGE: Record<string, string> = {
  open: "bdg--danger",
  acknowledged: "bdg--warning",
  diagnosing: "bdg--info",
  resolved: "bdg--success",
  closed: "bdg--neutral",
};

/** 资产类型。 */
export const ASSET_TYPE_LABELS: Record<string, string> = {
  host: "主机",
  db: "数据库",
  middleware: "中间件",
  app: "应用",
  network: "网络设备",
};

/** 资产状态。 */
export const ASSET_STATUS_LABELS: Record<string, string> = {
  active: "运行中",
  maintenance: "维护中",
  decommissioned: "已下线",
};

/** 资产状态配色。 */
export const ASSET_STATUS_BADGE: Record<string, string> = {
  active: "bdg--success",
  maintenance: "bdg--warning",
  decommissioned: "bdg--neutral",
};

/** 研判状态。 */
export const DIAGNOSIS_STATUS_LABELS: Record<string, string> = {
  running: "研判中",
  completed: "已完成",
  failed: "失败",
  timeout: "超时",
};

/** 研判状态配色。 */
export const DIAGNOSIS_STATUS_BADGE: Record<string, string> = {
  running: "bdg--info",
  completed: "bdg--success",
  failed: "bdg--danger",
  timeout: "bdg--warning",
};

/** 研判降级原因（后端 degraded_reason）。 */
export const DEGRADED_REASON_LABELS: Record<string, string> = {
  timeout: "研判超时，结果可能不完整",
  llm_unavailable: "模型服务不可用",
  insufficient_evidence: "证据不足，置信度已下调",
  unparsed: "模型输出未能解析出结构化结论",
};

/** 研判触发方式。 */
export const TRIGGER_TYPE_LABELS: Record<string, string> = {
  manual: "人工触发",
  rule: "规则自动",
};

/** 时间线条目类型。 */
export const ENTRY_TYPE_LABELS: Record<string, string> = {
  created: "事件登记",
  rule_matched: "命中规则",
  status_change: "状态流转",
  diagnosis_started: "研判开始",
  diagnosis_finished: "研判结束",
  comment: "复盘评论",
};

/** 时间线主体类型。 */
export const ACTOR_TYPE_LABELS: Record<string, string> = {
  user: "用户",
  system: "系统",
  agent: "AI 研判",
};

/** 角色码 → 中文名（后端 roles 表的兜底映射；取值须与 db.py 种子一致）。 */
export const ROLE_LABELS: Record<string, string> = {
  admin: "管理员",
  operator: "运维工程师",
  viewer: "只读观察员",
};

/**
 * 取映射值，缺失时回退原值（避免界面出现空白）。
 *
 * @param map 文案映射表。
 * @param key 后端原始值。
 * @returns 展示文案。
 */
export function labelOf(map: Record<string, string>, key: string | null | undefined): string {
  if (!key) return "—";
  return map[key] ?? key;
}
