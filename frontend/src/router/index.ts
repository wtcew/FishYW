/**
 * 路由表（hash 模式，便于任意静态托管环境直接打开）。
 *
 * 守卫策略：
 * - 未登录且目标非 `/login` → 跳登录，并带 `redirect` 记住来路；
 * - 已登录访问 `/login` → 回运行概览；
 * - `meta.roles` 非空时校验角色（admin 专属页），不满足回运行概览并提示；
 * - `meta.hidden` 的详情页不进侧栏菜单。
 */
import { createRouter, createWebHashHistory, type RouteRecordRaw } from "vue-router";

import type { RoleCode } from "@/api/types";
import { pushToast } from "@/composables/useToast";
import { useAuthStore } from "@/stores/auth";

declare module "vue-router" {
  interface RouteMeta {
    /** 页面标题（写入 document.title，侧栏菜单文案另在 App.vue 定义）。 */
    title?: string;
    /** 公开页（无需登录）。 */
    public?: boolean;
    /** 允许访问的角色；不填表示登录即可。 */
    roles?: RoleCode[];
    /** 详情页：不进侧栏菜单。 */
    hidden?: boolean;
    /** 独立布局（不套用侧栏骨架），登录页使用。 */
    blank?: boolean;
    /** 占位页配置（Phase 2 模块）。 */
    placeholder?: { module: string; phase: string; items: string[] };
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/views/LoginView.vue"),
    meta: { title: "登录", public: true, blank: true, hidden: true },
  },
  {
    // 注册与登录共用一个视图（模式由路由名决定），避免重复整页布局。
    path: "/register",
    name: "register",
    component: () => import("@/views/LoginView.vue"),
    meta: { title: "注册", public: true, blank: true, hidden: true },
  },
  {
    path: "/",
    name: "overview",
    component: () => import("@/views/OverviewView.vue"),
    meta: { title: "运行概览" },
  },
  // ── AIOps（原演示版视图平移，能力保留） ──
  {
    path: "/aiops/chat",
    name: "aiops-chat",
    component: () => import("@/views/chat/ChatView.vue"),
    meta: { title: "智能问答" },
  },
  {
    path: "/aiops/knowledge",
    name: "aiops-knowledge",
    component: () => import("@/views/knowledge/KnowledgeView.vue"),
    meta: { title: "知识库" },
  },
  {
    path: "/aiops/retrieval",
    name: "aiops-retrieval",
    component: () => import("@/views/retrieval/RetrievalView.vue"),
    meta: { title: "混合检索" },
  },
  {
    path: "/aiops/eval",
    name: "aiops-eval",
    component: () => import("@/views/eval/EvalView.vue"),
    meta: { title: "效果评估" },
  },
  {
    path: "/aiops/agent",
    name: "aiops-agent",
    component: () => import("@/views/agent/AgentView.vue"),
    meta: { title: "Agent 链路" },
  },
  // ── 资源中心 ──
  {
    path: "/assets",
    name: "asset-list",
    component: () => import("@/views/assets/AssetListView.vue"),
    meta: { title: "资产登记" },
  },
  {
    path: "/assets/:id(\\d+)",
    name: "asset-detail",
    component: () => import("@/views/assets/AssetDetailView.vue"),
    meta: { title: "资产详情", hidden: true },
  },
  // ── 事件中心 ──
  {
    path: "/events",
    name: "event-list",
    component: () => import("@/views/events/EventListView.vue"),
    meta: { title: "事件列表" },
  },
  {
    path: "/events/:id(\\d+)",
    name: "event-detail",
    component: () => import("@/views/events/EventDetailView.vue"),
    meta: { title: "事件详情", hidden: true },
  },
  // ── 系统管理 ──
  {
    path: "/admin",
    name: "admin",
    component: () => import("@/views/admin/AdminView.vue"),
    meta: { title: "系统管理", roles: ["admin"] },
  },
  {
    path: "/help",
    name: "help",
    component: () => import("@/views/help/HelpView.vue"),
    meta: { title: "使用指南" },
  },
  {
    path: "/settings",
    name: "settings",
    component: () => import("@/views/settings/SettingsView.vue"),
    meta: { title: "设置" },
  },
  // ── Phase 2 占位模块 ──
  {
    path: "/observability",
    name: "observability",
    component: () => import("@/views/PlaceholderView.vue"),
    meta: {
      title: "可观测性",
      placeholder: {
        module: "可观测性",
        phase: "Phase 2",
        items: ["指标（Prometheus 适配）", "日志（Loki 适配）", "链路与告警看板"],
      },
    },
  },
  {
    path: "/tasks",
    name: "tasks",
    component: () => import("@/views/PlaceholderView.vue"),
    meta: {
      title: "任务中心",
      placeholder: {
        module: "任务中心",
        phase: "Phase 2",
        items: ["Playbook 编排", "批量执行与幂等重试", "执行结果与回执"],
      },
    },
  },
  {
    path: "/tickets",
    name: "tickets",
    component: () => import("@/views/PlaceholderView.vue"),
    meta: {
      title: "工单系统",
      placeholder: {
        module: "工单系统",
        phase: "Phase 2",
        items: ["工单状态机与审批流", "PendingAction 确认执行", "SLA 与超时升级"],
      },
    },
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();

  if (to.meta.public) {
    // 公开页（登录/注册）：已登录用户不必停留，直接回运行概览。
    if (auth.isLoggedIn && !auth.user) await auth.fetchProfile();
    if (auth.isLoggedIn) return { name: "overview" };
    return true;
  }

  if (!auth.isLoggedIn) {
    return { name: "login", query: to.fullPath === "/" ? {} : { redirect: to.fullPath } };
  }

  // 角色判定需要用户档案：有 token 但无缓存（如换了浏览器标签）时先回源。
  if (!auth.user) await auth.fetchProfile();
  if (!auth.isLoggedIn) return { name: "login" };

  if (to.meta.roles?.length && !to.meta.roles.includes(auth.role as RoleCode)) {
    pushToast("权限不足：该页面仅管理员可访问", "error");
    return { name: "overview" };
  }
  return true;
});

router.afterEach((to) => {
  const title = to.meta.title;
  document.title = title ? `${title} · FishCloud` : "FishCloud · 智能运维平台";
});

export default router;
