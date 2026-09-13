<script setup lang="ts">
/**
 * 系统管理（仅管理员，路由 meta.roles 已拦截）。
 *
 * 五个 Tab：用户 / 角色 / 告警规则 / 审计日志 / 系统健康。
 * 说明：设计稿原写「三 Tab」，实现按主会话派单扩展为五 Tab
 * （告警规则 CRUD 与系统健康原本在演示版 AlertsView/Overview 中，删除演示页后并入此处）。
 *
 * 审计日志分页信封字段是后端唯一使用 `pageSize` 的接口（其余为 `page_size`），
 * 已在 api/normalizePage 内兼容。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import {
  createAlertRule,
  deleteAlertRule,
  listAlertRules,
  listAuditLogs,
  listRoles,
  listUsers,
  resetPassword,
  systemHealth,
  updateAlertRule,
  updateUser,
} from "@/api/admin";
import { request, ApiError } from "@/api/http";
import type { AlertRule, AlertRulePayload, AuditLog, EventSeverity, RagHealth, Role, SystemHealth, User } from "@/api/types";
import { ROLE_CAPABILITIES, type Capability, type RoleCode } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";
import Pagination from "@/components/Pagination.vue";
import { ROLE_LABELS, SEVERITY_LABELS, labelOf } from "@/lib/labels";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useAuthStore } from "@/stores/auth";
import { pushToast } from "@/composables/useToast";
import UserForm from "./components/UserForm.vue";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

/** Tab 定义。 */
const TABS = [
  { key: "users", label: "用户" },
  { key: "roles", label: "角色" },
  { key: "rules", label: "告警规则" },
  { key: "audit", label: "审计日志" },
  { key: "health", label: "系统健康" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

/** 当前 Tab（与 query 同步，便于从事件页直达告警规则）。 */
const tab = ref<TabKey>("users");
const tabFromQuery = computed(() => (typeof route.query.tab === "string" ? route.query.tab : ""));

/**
 * 切换 Tab（写入 query，保持可分享/可刷新）。
 *
 * @param key Tab 键。
 */
function switchTab(key: TabKey): void {
  // 切 Tab 必须触发该 Tab 的首次加载：原实现只改 tab 与 query，
  // 导致「角色」空白、「告警规则」误显示为空态、「系统健康」显示不可用
  // （2026-09-13 UI 穷举测试 P1——数据其实在后端好好的）。
  tab.value = key;
  ensureLoaded(key);
  void router.replace({ query: { ...route.query, tab: key } });
}

// ── 用户 ──────────────────────────────────────────────────────────
const users = ref<User[]>([]);
const usersLoading = ref(false);
const usersError = ref("");
const userFormOpen = ref(false);
const editingUser = ref<User | null>(null);

/** 重置密码结果（一次性展示）。 */
const resetResult = ref<{ username: string; password: string } | null>(null);
const resetPending = ref<number | null>(null);

/** 加载用户列表。 */
async function loadUsers(): Promise<void> {
  usersLoading.value = true;
  usersError.value = "";
  try {
    users.value = await listUsers();
  } catch (error) {
    usersError.value = error instanceof ApiError ? error.message : "用户列表加载失败";
    users.value = [];
  } finally {
    usersLoading.value = false;
  }
}

/**
 * 打开用户表单。
 *
 * @param user 编辑目标；null 为新建。
 */
function openUserForm(user: User | null = null): void {
  editingUser.value = user;
  userFormOpen.value = true;
}

/**
 * 用户保存成功后的收尾。
 *
 * @param message 成功提示文案。
 */
async function onUserSaved(message: string): Promise<void> {
  userFormOpen.value = false;
  editingUser.value = null;
  pushToast(message, "success");
  await loadUsers();
  if (!roles.value.length) await loadRoles();
}

/**
 * 切换用户启用状态。
 *
 * @param user 目标用户。
 */
async function toggleUserActive(user: User): Promise<void> {
  try {
    await updateUser(user.id, { is_active: !user.isActive });
    pushToast(user.isActive ? `已停用「${user.username}」` : `已启用「${user.username}」`, "success");
    await loadUsers();
  } catch {
    // 错误提示由 http 层弹出（含「不能停用自己 / 最后一个管理员」）
  }
}

/**
 * 重置密码（后端生成一次性随机密码，仅在响应中返回一次）。
 *
 * @param user 目标用户。
 */
async function doResetPassword(user: User): Promise<void> {
  if (resetPending.value !== null) return;
  resetPending.value = user.id;
  try {
    const result = await resetPassword(user.id);
    resetResult.value = { username: result.username, password: result.newPassword };
  } finally {
    resetPending.value = null;
  }
}

// ── 角色 ──────────────────────────────────────────────────────────
const roles = ref<Role[]>([]);
const rolesError = ref("");
const selectedRole = ref<string>("admin");

/** 权限矩阵的能力列。 */
const CAPABILITIES: Array<{ key: Capability; label: string; desc: string }> = [
  { key: "read", label: "读取", desc: "查看资产/事件/审计以外的只读接口" },
  { key: "write", label: "写入", desc: "登记与编辑资产、认领与流转事件、触发研判" },
  { key: "admin", label: "管理", desc: "用户与角色、告警规则、删除资产、审计日志" },
];

/** 加载角色列表（登录即可访问）。 */
async function loadRoles(): Promise<void> {
  rolesError.value = "";
  try {
    roles.value = await listRoles();
    selectedRole.value = roles.value[0]?.code ?? "admin";
  } catch (error) {
    rolesError.value = error instanceof ApiError ? error.message : "角色列表加载失败";
  }
}

/**
 * 判定角色是否具备某能力（镜像后端 deps.ROLE_MATRIX）。
 *
 * @param roleCode 角色码。
 * @param capability 能力。
 * @returns 具备返回 true。
 */
function roleHas(roleCode: string, capability: Capability): boolean {
  return (ROLE_CAPABILITIES[roleCode as RoleCode] ?? []).includes(capability);
}

// ── 告警规则 ─────────────────────────────────────────────────────
const rules = ref<AlertRule[]>([]);
const rulesLoading = ref(false);
const rulesError = ref("");
const ruleFormOpen = ref(false);
const editingRule = ref<AlertRule | null>(null);
const rulePending = ref(false);
const ruleError = ref("");
const ruleForm = ref<AlertRulePayload>({
  name: "",
  enabled: true,
  matchField: "title",
  matchOp: "contains",
  matchValue: "",
  severity: "major",
  autoDiagnose: false,
  description: "",
});

/** 匹配字段候选（后端支持 title / source / asset.identifier / payload_key:<key>）。 */
const MATCH_FIELDS = [
  { value: "title", label: "标题 title" },
  { value: "source", label: "来源 source" },
  { value: "asset.identifier", label: "资产标识 asset.identifier" },
];
const MATCH_OPS = [
  { value: "contains", label: "包含 contains" },
  { value: "eq", label: "等于 eq" },
  { value: "regex", label: "正则 regex" },
];
const SEVERITY_OPTIONS: EventSeverity[] = ["critical", "major", "minor", "info"];

/** 加载告警规则。 */
async function loadRules(): Promise<void> {
  rulesLoading.value = true;
  rulesError.value = "";
  try {
    rules.value = await listAlertRules();
  } catch (error) {
    rulesError.value = error instanceof ApiError ? error.message : "告警规则加载失败";
    rules.value = [];
  } finally {
    rulesLoading.value = false;
  }
}

/**
 * 打开规则表单。
 *
 * @param rule 编辑目标；不传为新建。
 */
function openRuleForm(rule: AlertRule | null = null): void {
  editingRule.value = rule;
  ruleError.value = "";
  ruleForm.value = rule
    ? {
        name: rule.name,
        enabled: rule.enabled,
        matchField: rule.matchField,
        matchOp: rule.matchOp,
        matchValue: rule.matchValue,
        severity: (rule.severity as EventSeverity) ?? "major",
        autoDiagnose: rule.autoDiagnose,
        description: rule.description,
      }
    : {
        name: "",
        enabled: true,
        matchField: "title",
        matchOp: "contains",
        matchValue: "",
        severity: "major",
        autoDiagnose: false,
        description: "",
      };
  ruleFormOpen.value = true;
}

/** 提交规则表单。 */
async function submitRule(): Promise<void> {
  if (rulePending.value) return;
  if (!ruleForm.value.name.trim() || !ruleForm.value.matchValue.trim()) {
    ruleError.value = "规则名称与匹配值均为必填";
    return;
  }
  rulePending.value = true;
  ruleError.value = "";
  try {
    if (editingRule.value) {
      await updateAlertRule(editingRule.value.id, { ...ruleForm.value, name: ruleForm.value.name.trim() });
      pushToast(`已更新规则「${ruleForm.value.name.trim()}」`, "success");
    } else {
      await createAlertRule({ ...ruleForm.value, name: ruleForm.value.name.trim() });
      pushToast(`已创建规则「${ruleForm.value.name.trim()}」`, "success");
    }
    ruleFormOpen.value = false;
    await loadRules();
  } catch (error) {
    ruleError.value = error instanceof ApiError ? error.message : "保存失败";
  } finally {
    rulePending.value = false;
  }
}

/**
 * 删除规则。
 *
 * @param rule 目标规则。
 */
async function removeRule(rule: AlertRule): Promise<void> {
  try {
    await deleteAlertRule(rule.id);
    pushToast(`已删除规则「${rule.name}」`, "success");
    await loadRules();
  } catch {
    // 错误提示由 http 层弹出
  }
}

// ── 审计日志 ─────────────────────────────────────────────────────
const auditRows = ref<AuditLog[]>([]);
const auditTotal = ref(0);
const auditPage = ref(1);
const auditPageSize = ref(50);
const auditLoading = ref(false);
const auditError = ref("");
const auditAction = ref("");
const auditUsername = ref("");
const auditResourceType = ref("");

/** 常用资源类型候选（来自后端 audit 的 resource_type 取值）。 */
const RESOURCE_TYPES = [
  { value: "user", label: "user 用户" },
  { value: "asset", label: "asset 资产" },
  { value: "business_line", label: "business_line 业务线" },
  { value: "environment", label: "environment 环境" },
  { value: "event", label: "event 事件" },
  { value: "alert_rule", label: "alert_rule 告警规则" },
];

/** 加载审计日志。 */
async function loadAudit(): Promise<void> {
  auditLoading.value = true;
  auditError.value = "";
  try {
    const result = await listAuditLogs({
      page: auditPage.value,
      pageSize: auditPageSize.value,
      action: auditAction.value,
      username: auditUsername.value,
      resourceType: auditResourceType.value,
    });
    auditRows.value = result.items;
    auditTotal.value = result.total;
  } catch (error) {
    auditError.value = error instanceof ApiError ? error.message : "审计日志加载失败";
    auditRows.value = [];
    auditTotal.value = 0;
  } finally {
    auditLoading.value = false;
  }
}

/** 审计过滤提交。 */
function applyAuditFilter(): void {
  auditPage.value = 1;
  void loadAudit();
}

/** 重置审计过滤。 */
function resetAuditFilter(): void {
  auditAction.value = "";
  auditUsername.value = "";
  auditResourceType.value = "";
  auditPage.value = 1;
  void loadAudit();
}

// ── 系统健康 ─────────────────────────────────────────────────────
const health = ref<SystemHealth | null>(null);
const healthError = ref("");
const healthLoading = ref(false);
const ragHealth = ref<RagHealth | null>(null);
const ragHealthError = ref("");

/** 加载平台健康 + 引擎健康。 */
async function loadHealth(): Promise<void> {
  healthLoading.value = true;
  healthError.value = "";
  ragHealthError.value = "";
  try {
    health.value = await systemHealth();
  } catch (error) {
    health.value = null;
    healthError.value = error instanceof ApiError ? error.message : "平台健康检查失败";
  }
  try {
    ragHealth.value = await request<RagHealth>("GET", "/health", { silent: true });
  } catch (error) {
    ragHealth.value = null;
    ragHealthError.value = error instanceof ApiError ? error.message : "引擎健康检查失败";
  }
  healthLoading.value = false;
}

/**
 * 按 Tab 懒加载数据（避免进页面就打 4 个接口）。
 *
 * @param key Tab 键。
 */
function ensureLoaded(key: TabKey): void {
  if (key === "users" && !users.value.length) void loadUsers();
  if (key === "roles" && !roles.value.length) void loadRoles();
  if (key === "rules" && !rules.value.length) void loadRules();
  if (key === "audit" && !auditRows.value.length) void loadAudit();
  if (key === "health" && !health.value) void loadHealth();
}

watch(
  () => [tab.value, auditPage.value, auditPageSize.value],
  () => {
    if (tab.value === "audit") void loadAudit();
  },
);

onMounted(() => {
  const initial = (["users", "roles", "rules", "audit", "health"] as const).find((key) => key === tabFromQuery.value);
  tab.value = initial ?? "users";
  if (initial) void router.replace({ query: { ...route.query, tab: initial } });
  if (tab.value === "users") void loadUsers();
  else ensureLoaded(tab.value);
});
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / Administration</div>
        <h2>系统管理</h2>
        <p>
          用户与角色、告警规则、审计日志与系统健康的统一入口。所有写操作都会落审计（含操作人与来源 IP）。
        </p>
      </div>
      <div class="ph-actions">
        <span class="bdg bdg--neutral"><AppIcon name="shield" :size="12" />管理员专属</span>
      </div>
    </div>

    <!-- Tab -->
    <div class="tabs tabs--sys" role="tablist" aria-label="系统管理分区">
      <button
        v-for="item in TABS"
        :key="item.key"
        class="tab"
        type="button"
        role="tab"
        :aria-selected="tab === item.key"
        @click="switchTab(item.key)"
      >
        {{ item.label }}
      </button>
    </div>

    <!-- ── 用户 ── -->
    <template v-if="tab === 'users'">
      <div class="ph-tools">
        <span class="ph-count">共 {{ users.length }} 个账号</span>
        <div style="margin-left: auto; display: flex; gap: 6px">
          <button class="btn btn-sm btn-mo" type="button" :disabled="usersLoading" @click="loadUsers">刷新</button>
          <button class="btn btn-sm btn-zhu" type="button" @click="openUserForm(null)">
            <AppIcon name="plus" :size="12" />新建用户
          </button>
        </div>
      </div>

      <div v-if="usersLoading" class="tbl-wrap">
        <div class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
      </div>
      <div v-else-if="usersError" class="err-box" role="alert">
        <span style="flex: 1">{{ usersError }}</span>
        <button class="btn btn-sm" type="button" @click="loadUsers">重试</button>
      </div>
      <div v-else class="tbl-wrap">
        <table class="tbl">
          <caption class="sr-only">用户列表</caption>
          <colgroup>
            <col style="width: 170px" />
            <col />
            <col style="width: 140px" />
            <col style="width: 96px" />
            <col style="width: 130px" />
            <col style="width: 190px" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">用户名</th>
              <th scope="col">显示名</th>
              <th scope="col">角色</th>
              <th scope="col">状态</th>
              <th scope="col">最近登录</th>
              <th scope="col" class="ta-r">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in users" :key="user.id">
              <td>
                <span class="cell-main">{{ user.username }}</span>
                <div v-if="user.id === auth.user?.id" class="cell-sub">当前登录账号</div>
              </td>
              <td>{{ user.displayName || "—" }}</td>
              <td>
                <span class="bdg bdg--neutral">{{ user.roleName || labelOf(ROLE_LABELS, user.roleCode) }}</span>
              </td>
              <td>
                <span class="bdg" :class="user.isActive ? 'bdg--success' : 'bdg--neutral'">
                  {{ user.isActive ? "启用" : "停用" }}
                </span>
              </td>
              <td class="t-dim nowrap">{{ user.lastLoginAt ? formatRelative(user.lastLoginAt) : "从未登录" }}</td>
              <td>
                <div class="row-actions">
                  <button
                    class="btn btn-sm btn-ghost"
                    type="button"
                    @click="openUserForm(user)"
                  >
                    编辑
                  </button>
                  <button
                    class="btn btn-sm btn-ghost"
                    type="button"
                    :disabled="resetPending === user.id"
                    @click="doResetPassword(user)"
                  >
                    重置密码
                  </button>
                  <button
                    class="btn btn-sm"
                    :class="user.isActive ? 'btn-ghost-danger' : 'btn-ghost'"
                    type="button"
                    :disabled="user.id === auth.user?.id"
                    @click="toggleUserActive(user)"
                  >
                    {{ user.isActive ? "停用" : "启用" }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- ── 角色 ── -->
    <template v-else-if="tab === 'roles'">
      <div v-if="rolesError" class="err-box" role="alert" style="margin-bottom: var(--sp-4)">
        <span style="flex: 1">{{ rolesError }}</span>
        <button class="btn btn-sm" type="button" @click="loadRoles">重试</button>
      </div>

      <div class="split split-260">
        <div>
          <div class="sec-title">内置角色（{{ roles.length }}）</div>
          <div
            v-for="role in roles"
            :key="role.code"
            class="mx-role"
            :class="{ on: selectedRole === role.code }"
            role="button"
            tabindex="0"
            @click="selectedRole = role.code"
            @keydown.enter.prevent="selectedRole = role.code"
            @keydown.space.prevent="selectedRole = role.code"
          >
            <span class="mr-name">{{ role.name }}</span>
            <span class="mr-code">{{ role.code }}</span>
            <span class="mr-desc">{{ role.description || "—" }}</span>
          </div>
        </div>

        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>{{ labelOf(ROLE_LABELS, selectedRole) }} · 能力矩阵</h3>
              <div class="sub">能力判定在后端，admin 恒通过一切校验。</div>
            </div>
          </div>
          <table class="mx">
            <thead>
              <tr>
                <th scope="col">能力</th>
                <th scope="col" style="width: 90px">是否具备</th>
                <th scope="col">说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="cap in CAPABILITIES" :key="cap.key">
                <td>
                  <span class="mono">{{ cap.key }}</span>
                </td>
                <td>
                  <span class="mx-cb" :class="roleHas(selectedRole, cap.key) ? 'on' : 'off'">
                    <AppIcon v-if="roleHas(selectedRole, cap.key)" name="check" :size="12" />
                  </span>
                </td>
                <td class="t-dim">{{ cap.desc }}</td>
              </tr>
            </tbody>
          </table>
          <p class="ctl-hint" style="margin-top: var(--sp-4)">
            角色为内置固定集合（admin / operator / viewer），不支持自定义。
          </p>
        </div>
      </div>
    </template>

    <!-- ── 告警规则 ── -->
    <template v-else-if="tab === 'rules'">
      <div class="ph-tools">
        <span class="ph-count">共 {{ rules.length }} 条规则</span>
        <div style="margin-left: auto; display: flex; gap: 6px">
          <button class="btn btn-sm btn-mo" type="button" :disabled="rulesLoading" @click="loadRules">刷新</button>
          <button class="btn btn-sm btn-zhu" type="button" @click="openRuleForm(null)">
            <AppIcon name="plus" :size="12" />新建规则
          </button>
        </div>
      </div>

      <div v-if="rulesLoading" class="tbl-wrap">
        <div class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
      </div>
      <div v-else-if="rulesError" class="err-box" role="alert">
        <span style="flex: 1">{{ rulesError }}</span>
        <button class="btn btn-sm" type="button" @click="loadRules">重试</button>
      </div>
      <div v-else-if="!rules.length" class="tbl-wrap">
        <div class="empty">
          <div class="em-ico"><AppIcon name="alerts" :size="20" /></div>
          暂无告警规则。
        </div>
      </div>
      <template v-else>
        <div v-for="rule in rules" :key="rule.id" class="rule-row" :class="{ fired: !rule.enabled }">
          <span class="rname">{{ rule.name }}</span>
          <span class="rcond">{{ rule.matchField }} {{ rule.matchOp }} "{{ rule.matchValue }}"</span>
          <span class="bdg bdg--sev" :class="rule.severity === 'critical' ? 'bdg--danger-solid' : 'bdg--warning'">
            {{ labelOf(SEVERITY_LABELS, rule.severity) }}
          </span>
          <span v-if="rule.autoDiagnose" class="bdg bdg--info">命中自动研判</span>
          <span class="rval">{{ rule.enabled ? "已启用" : "已停用" }}</span>
          <div class="r-switch">
            <button class="btn btn-sm btn-ghost" type="button" @click="openRuleForm(rule)">编辑</button>
            <button class="btn btn-sm btn-ghost-danger" type="button" @click="removeRule(rule)">删除</button>
          </div>
        </div>
      </template>
    </template>

    <!-- ── 审计日志 ── -->
    <template v-else-if="tab === 'audit'">
      <div class="filter-bar">
        <label class="fbi">
          <span>资源类型</span>
          <select v-model="auditResourceType" class="ctl" @change="applyAuditFilter">
            <option value="">全部</option>
            <option v-for="item in RESOURCE_TYPES" :key="item.value" :value="item.value">{{ item.label }}</option>
          </select>
        </label>
        <label class="fbi">
          <span>动作</span>
          <input
            v-model="auditAction"
            class="ctl"
            placeholder="如 asset.create"
            @keydown.enter.prevent="applyAuditFilter"
          />
        </label>
        <label class="fbi">
          <span>操作人</span>
          <input
            v-model="auditUsername"
            class="ctl"
            placeholder="用户名"
            @keydown.enter.prevent="applyAuditFilter"
          />
        </label>
        <div class="fb-acts">
          <button class="btn btn-sm" type="button" @click="applyAuditFilter">查询</button>
          <button class="btn btn-sm btn-ghost" type="button" @click="resetAuditFilter">重置</button>
        </div>
      </div>

      <div v-if="auditLoading" class="tbl-wrap">
        <div class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
      </div>
      <div v-else-if="auditError" class="err-box" role="alert">
        <span style="flex: 1">{{ auditError }}</span>
        <button class="btn btn-sm" type="button" @click="loadAudit">重试</button>
      </div>
      <div v-else-if="!auditRows.length" class="tbl-wrap">
        <div class="empty">没有符合条件的审计记录。</div>
      </div>
      <template v-else>
        <div class="tbl-wrap">
          <table class="tbl">
            <caption class="sr-only">审计日志</caption>
            <colgroup>
              <col style="width: 148px" />
              <col style="width: 108px" />
              <col style="width: 168px" />
              <col style="width: 120px" />
              <col style="width: 116px" />
              <col />
            </colgroup>
            <thead>
              <tr>
                <th scope="col">时间</th>
                <th scope="col">操作人</th>
                <th scope="col">动作</th>
                <th scope="col">对象</th>
                <th scope="col">来源 IP</th>
                <th scope="col">详情</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in auditRows" :key="row.id">
                <td class="mono nowrap">{{ formatDateTime(row.createdAt) }}</td>
                <td class="mono">{{ row.username || "—" }}</td>
                <td class="mono">{{ row.action }}</td>
                <td class="mono t-dim">
                  {{ row.resourceType || "—" }}<span v-if="row.resourceId">#{{ row.resourceId }}</span>
                </td>
                <td class="mono t-dim">{{ row.ip || "—" }}</td>
                <td class="mono t-dim" style="word-break: break-all">
                  {{ row.detail ? JSON.stringify(row.detail) : "—" }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <Pagination v-model:page="auditPage" v-model:page-size="auditPageSize" :total="auditTotal" :disabled="auditLoading" />
      </template>
    </template>

    <!-- ── 系统健康 ── -->
    <template v-else>
      <div class="ph-tools">
        <span class="ph-count">连通性与数据规模</span>
        <div style="margin-left: auto">
          <button class="btn btn-sm btn-mo" type="button" :disabled="healthLoading" @click="loadHealth">
            <span v-if="healthLoading" class="spin" aria-hidden="true"></span>重新检测
          </button>
        </div>
      </div>

      <div class="grid2">
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>平台服务</h3>
              <div class="sub">FastAPI · 平台库（资产/事件/用户/审计）</div>
            </div>
            <span v-if="health" class="bdg bdg--success bdg--pulse"><span class="d"></span>{{ health.status }}</span>
            <span v-else class="bdg bdg--danger"><span class="d"></span>不可用</span>
          </div>
          <div v-if="healthError" class="err-box" role="alert">{{ healthError }}</div>
          <template v-else-if="health">
            <div class="kv">
              <div class="k">存储后端</div>
              <div class="v mono">{{ health.storage === "mysql" ? "MySQL（主库）" : "SQLite（兜底库）" }}</div>
            </div>
            <div class="kv">
              <div class="k">平台版本</div>
              <div class="v mono">{{ health.version }}</div>
            </div>
            <div class="kv">
              <div class="k">用户</div>
              <div class="v mono">{{ health.rows.users }} 个账号</div>
            </div>
            <div class="kv">
              <div class="k">资产</div>
              <div class="v mono">{{ health.rows.assets }} 条</div>
            </div>
            <div class="kv">
              <div class="k">事件</div>
              <div class="v mono">{{ health.rows.events }} 条</div>
            </div>
          </template>
          <div v-else-if="healthLoading" class="loading-row"><span class="spin" aria-hidden="true"></span>检测中…</div>
        </div>

        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>AI 引擎</h3>
              <div class="sub">LangGraph 状态机 + 混合检索（向量/BM25/图谱）</div>
            </div>
            <span v-if="ragHealth" class="bdg bdg--success bdg--pulse"><span class="d"></span>{{ ragHealth.status }}</span>
            <span v-else class="bdg bdg--danger"><span class="d"></span>不可用</span>
          </div>
          <div v-if="ragHealthError" class="err-box" role="alert">{{ ragHealthError }}</div>
          <template v-else-if="ragHealth">
            <div class="kv">
              <div class="k">向量索引</div>
              <div class="v mono">{{ ragHealth.vector_count }} 条</div>
            </div>
            <div class="kv">
              <div class="k">图谱节点</div>
              <div class="v mono">{{ ragHealth.graph_nodes }} 个</div>
            </div>
            <div class="kv">
              <div class="k">图谱边</div>
              <div class="v mono">{{ ragHealth.graph_edges }} 条</div>
            </div>
          </template>
          <div v-else-if="healthLoading" class="loading-row"><span class="spin" aria-hidden="true"></span>检测中…</div>
        </div>
      </div>
    </template>

    <!-- 用户表单 -->
    <UserForm
      :open="userFormOpen"
      :user="editingUser"
      :roles="roles"
      @close="userFormOpen = false"
      @saved="onUserSaved"
    />

    <!-- 重置密码结果 -->
    <div v-if="resetResult" class="modal-mask" role="dialog" aria-modal="true" aria-labelledby="reset-title">
      <div class="modal">
        <div class="modal-head">
          <h3 id="reset-title">密码已重置</h3>
          <button class="modal-close" type="button" aria-label="关闭" @click="resetResult = null">
            <AppIcon name="close" :size="14" />
          </button>
        </div>
        <div class="modal-body">
          <p style="font-size: 12.5px; line-height: 1.85">
            用户 <b>{{ resetResult.username }}</b> 的新密码（<span style="color: var(--st-danger-text)">仅本次显示</span>）：
          </p>
          <p class="quote mono" style="margin-top: var(--sp-3); font-size: 13px">{{ resetResult.password }}</p>
          <p class="ctl-hint" style="margin-top: var(--sp-3)">请通过安全渠道转交用户。</p>
        </div>
        <div class="modal-foot">
          <button class="btn btn-zhu" type="button" @click="resetResult = null">我已记录</button>
        </div>
      </div>
    </div>

    <!-- 告警规则表单 -->
    <div
      v-if="ruleFormOpen"
      class="modal-mask"
      role="dialog"
      aria-modal="true"
      aria-labelledby="rule-form-title"
      @click.self="ruleFormOpen = false"
    >
      <div class="modal modal--wide">
        <div class="modal-head">
          <div>
            <h3 id="rule-form-title">{{ editingRule ? "编辑告警规则" : "新建告警规则" }}</h3>
            <div class="modal-sub">命中后按规则严重度入库，并可自动触发 AI 研判</div>
          </div>
          <button class="modal-close" type="button" aria-label="关闭" @click="ruleFormOpen = false">
            <AppIcon name="close" :size="14" />
          </button>
        </div>
        <form class="modal-body" novalidate @submit.prevent="submitRule">
          <div class="frm-grid">
            <div class="field">
              <label for="rf-name">规则名称<span class="req">*</span></label>
              <input id="rf-name" v-model="ruleForm.name" class="ctl" maxlength="128" :disabled="rulePending" />
            </div>
            <div class="field">
              <label for="rf-sev">命中后严重度</label>
              <select id="rf-sev" v-model="ruleForm.severity" class="ctl" :disabled="rulePending">
                <option v-for="sev in SEVERITY_OPTIONS" :key="sev" :value="sev">{{ labelOf(SEVERITY_LABELS, sev) }}</option>
              </select>
            </div>
            <div class="field">
              <label for="rf-field">匹配字段</label>
              <select id="rf-field" v-model="ruleForm.matchField" class="ctl" :disabled="rulePending">
                <option v-for="item in MATCH_FIELDS" :key="item.value" :value="item.value">{{ item.label }}</option>
              </select>
            </div>
            <div class="field">
              <label for="rf-op">匹配算子</label>
              <select id="rf-op" v-model="ruleForm.matchOp" class="ctl" :disabled="rulePending">
                <option v-for="item in MATCH_OPS" :key="item.value" :value="item.value">{{ item.label }}</option>
              </select>
            </div>
            <div class="field full">
              <label for="rf-value">匹配值<span class="req">*</span></label>
              <input
                id="rf-value"
                v-model="ruleForm.matchValue"
                class="ctl"
                maxlength="255"
                placeholder="如 磁盘使用率 / OOM / db-prod-01"
                :disabled="rulePending"
              />
            </div>
            <div class="field">
              <label for="rf-enabled">规则状态</label>
              <select id="rf-enabled" v-model="ruleForm.enabled" class="ctl" :disabled="rulePending">
                <option :value="true">启用</option>
                <option :value="false">停用</option>
              </select>
            </div>
            <div class="field">
              <label for="rf-auto">命中后自动研判</label>
              <select id="rf-auto" v-model="ruleForm.autoDiagnose" class="ctl" :disabled="rulePending">
                <option :value="false">不自动触发</option>
                <option :value="true">自动触发 AI 研判</option>
              </select>
            </div>
            <div class="field full">
              <label for="rf-desc">说明</label>
              <input id="rf-desc" v-model="ruleForm.description" class="ctl" maxlength="255" :disabled="rulePending" />
            </div>
          </div>
          <p v-if="ruleError" class="ctl-err" role="alert" style="margin-top: var(--sp-3)">{{ ruleError }}</p>
        </form>
        <div class="modal-foot">
          <button class="btn btn-mo" type="button" :disabled="rulePending" @click="ruleFormOpen = false">取消</button>
          <button class="btn btn-zhu" type="button" :disabled="rulePending" @click="submitRule">
            <span v-if="rulePending" class="spin" aria-hidden="true"></span>
            {{ editingRule ? "保存" : "创建" }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
