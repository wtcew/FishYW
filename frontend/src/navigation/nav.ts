/**
 * 导航模型（菜单分组 / 组内页面）。
 *
 * 单一数据源：AppSidebar（侧栏分组菜单）与各页面包屑的模块名都从这里取，
 * 避免侧栏、路由表、页头三处各写一份而走偏。
 *
 * 结构约定（对齐 docs/platform-frontend-design.md §1 路由表的「菜单分组」列）：
 * - 一级 = 「模块」，显示为侧栏分组标题，共 8 组；单条目组（运行概览 / 可观测性 /
 *   任务中心 / 工单系统）保持「组标题 + 单条」，不合并；
 * - 二级 = 模块内的页面，显示为分组下的条目（图标 + 文案）；
 * - 详情页（资产 / 事件）不进本表，只由路由 meta.hidden 标记；
 * - P2 标记挂在分组上（模块整体未落地），单条目特例下也挂在分组；
 * - roles 非空的条目只对相应角色可见（/admin 仅 admin；系统管理组对非 admin
 *   仍有「设置 / 使用指南」两条，故不做整组隐藏）。
 */
import type { RoleCode } from "@/api/types";

/** 菜单条目（原二级导航项）。 */
export interface NavItem {
  /** 目标路径。 */
  to: string;
  /** 菜单文案（与各页 page-head 标题保持一致）。 */
  title: string;
  /** AppIcon 图标名。 */
  icon: string;
  /** 仅这些角色可见（不填=登录即可）。 */
  roles?: RoleCode[];
  /** 条目级小标签（现由分组级 tag 承担，保留字段备用）。 */
  tag?: string;
}

/** 菜单分组（原一级模块）。 */
export interface NavGroup {
  /** 模块键（用于列表 key）。 */
  key: string;
  /** 模块名（侧栏分组标题）。 */
  title: string;
  /** 分组图标（侧栏分组标题不渲染图标，保留供其他导航形态使用）。 */
  icon: string;
  /** 分组级小标签（如 Phase 2 占位模块）。 */
  tag?: string;
  /** 模块内页面（第一个为模块落点）。 */
  items: NavItem[];
}

/** 全量导航（顺序即展示顺序，8 组）。 */
export const NAV_GROUPS: NavGroup[] = [
  {
    key: "overview",
    title: "运行概览",
    icon: "overview",
    items: [{ to: "/", title: "运行概览", icon: "overview" }],
  },
  {
    key: "assets",
    title: "资源中心",
    icon: "assets",
    items: [{ to: "/assets", title: "资产登记", icon: "assets" }],
  },
  {
    key: "events",
    title: "事件中心",
    icon: "events",
    items: [{ to: "/events", title: "事件与研判", icon: "events" }],
  },
  {
    key: "aiops",
    title: "AIOps",
    icon: "ai",
    items: [
      { to: "/aiops/chat", title: "智能问答", icon: "chat" },
      { to: "/aiops/knowledge", title: "知识库", icon: "knowledge" },
      { to: "/aiops/retrieval", title: "混合检索", icon: "retrieval" },
      { to: "/aiops/eval", title: "效果评估", icon: "eval" },
      { to: "/aiops/agent", title: "Agent 链路", icon: "agent" },
    ],
  },
  {
    key: "system",
    title: "系统管理",
    icon: "admin",
    items: [
      { to: "/admin", title: "系统管理", icon: "admin", roles: ["admin"] },
      { to: "/settings", title: "设置", icon: "settings" },
      // 使用指南为实现新增（PRD 路由表无 /help），就近挂在系统管理组下
      { to: "/help", title: "使用指南", icon: "help" },
    ],
  },
  {
    key: "observability",
    title: "可观测性",
    icon: "monitor",
    tag: "P2",
    items: [{ to: "/observability", title: "可观测性", icon: "monitor" }],
  },
  {
    key: "tasks",
    title: "任务中心",
    icon: "tasks",
    tag: "P2",
    items: [{ to: "/tasks", title: "任务中心", icon: "tasks" }],
  },
  {
    key: "tickets",
    title: "工单系统",
    icon: "tickets",
    tag: "P2",
    items: [{ to: "/tickets", title: "工单系统", icon: "tickets" }],
  },
];

/**
 * 按角色过滤后的模块（保持顺序，空模块剔除）。
 *
 * @param roleCode 当前角色码。
 * @returns 可见模块数组。
 */
export function visibleGroups(roleCode: string): NavGroup[] {
  return NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.roles || item.roles.includes(roleCode as RoleCode)),
  })).filter((group) => group.items.length > 0);
}

/**
 * 判断导航项是否为当前页（详情页也算其列表页激活）。
 *
 * @param to 导航目标路径。
 * @param path 当前路由路径。
 * @returns 激活返回 true。
 */
export function isItemActive(to: string, path: string): boolean {
  if (to === "/") return path === "/";
  return path === to || path.startsWith(`${to}/`);
}

/**
 * 取当前路由所属分组（找不到时回落到第一个可见分组）。
 *
 * 用途：外壳按模块切换渐变（`[data-module]`，见 styles/layout.css），
 * 以及面包屑/侧栏高亮 —— 详情页（/assets/:id、/events/:id）由
 * {@link isItemActive} 的前缀判断归入其列表页所属分组。
 *
 * @param path 当前路由路径。
 * @param groups 可见分组（默认全量）。
 * @returns 当前分组。
 */
export function activeGroup(path: string, groups: NavGroup[] = NAV_GROUPS): NavGroup {
  const hit = groups.find((group) => group.items.some((item) => isItemActive(item.to, path)));
  return hit ?? groups[0] ?? NAV_GROUPS[0]!;
}
