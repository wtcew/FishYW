<script setup lang="ts">
/**
 * 运行概览（Phase 1）：全部指标来自后端真实接口，无演示数据。
 *
 * 数据来源：
 * - 资产计数：`GET /assets?page=1&page_size=1`（只取 total，不拉全量）；
 * - 事件 KPI：`GET /events?...&page_size=1`（按状态/严重度分别取 total）；
 * - 最近事件：`GET /events?page_size=6`；
 * - 引擎健康：`GET /api/v1/health`（向量/图谱规模）；
 * - 平台健康：`GET /admin/system/health`（存储后端/表行数/版本，仅管理员可见）。
 *
 * 每个异步块独立维护 loading / error / success 三态，任一失败不影响其它区块渲染。
 */
import { computed, onMounted, reactive, ref } from "vue";

import { listAssets } from "@/api/assets";
import { listEvents } from "@/api/events";
import { request, ApiError } from "@/api/http";
import { systemHealth } from "@/api/admin";
import AppIcon from "@/components/AppIcon.vue";
import { EVENT_STATUS_BADGE, EVENT_STATUS_LABELS, SEVERITY_BADGE, SEVERITY_LABELS, labelOf } from "@/lib/labels";
import { formatRelative, formatShortTime } from "@/lib/format";
import { useAuthStore } from "@/stores/auth";
import type { OpsEvent, RagHealth, SystemHealth } from "@/api/types";

const auth = useAuthStore();

/** KPI 计数。 */
const kpi = reactive({
  assets: 0,
  open: 0,
  critical: 0,
  resolved: 0,
});

/** 概览加载状态（KPI + 最近事件同批请求）。 */
const loading = ref(true);
const loadError = ref("");
const kpiReady = ref(false);

/** 最近事件。 */
const recent = ref<OpsEvent[]>([]);

/** 引擎健康（RAG 侧）。 */
const health = ref<RagHealth | null>(null);
const healthError = ref("");
const healthLoading = ref(true);

/** 平台健康（admin 专属）。 */
const platform = ref<SystemHealth | null>(null);
const platformError = ref("");

/** 当前用户展示用。 */
const roleText = computed(() => auth.roleName || labelOf({}, auth.role));

/** 平台存储后端的中文名。 */
const storageText = computed(() => {
  if (!platform.value) return "";
  return platform.value.storage === "mysql" ? "MySQL 主库" : "SQLite 兜底库";
});

/**
 * 加载 KPI 与最近事件。
 *
 * @param signal 取消信号。
 */
async function loadOverview(signal?: AbortSignal): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const [assets, open, critical, resolved, latest] = await Promise.all([
      listAssets({ page: 1, pageSize: 1 }, signal),
      listEvents({ page: 1, pageSize: 1, status: "open" }, signal),
      listEvents({ page: 1, pageSize: 1, severity: "critical" }, signal),
      listEvents({ page: 1, pageSize: 1, status: "resolved" }, signal),
      listEvents({ page: 1, pageSize: 6 }, signal),
    ]);
    kpi.assets = assets.total;
    kpi.open = open.total;
    kpi.critical = critical.total;
    kpi.resolved = resolved.total;
    recent.value = latest.items;
    kpiReady.value = true;
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    loadError.value = error instanceof ApiError ? error.message : "概览数据加载失败";
  } finally {
    loading.value = false;
  }
}

/**
 * 加载引擎健康（失败不阻断页面，只在该卡内提示）。
 *
 * @param signal 取消信号。
 */
async function loadHealth(signal?: AbortSignal): Promise<void> {
  healthLoading.value = true;
  try {
    health.value = await request<RagHealth>("GET", "/health", { signal, silent: true });
    healthError.value = "";
  } catch (error) {
    health.value = null;
    healthError.value = error instanceof ApiError ? error.message : "健康检查失败";
  } finally {
    healthLoading.value = false;
  }
}

/**
 * 加载平台健康（仅管理员；非管理员静默降级为提示文案）。
 *
 * @param signal 取消信号。
 */
async function loadPlatform(signal?: AbortSignal): Promise<void> {
  if (!auth.isAdmin) {
    platformError.value = "平台存储与行数统计仅管理员可见";
    return;
  }
  try {
    platform.value = await systemHealth(signal);
    platformError.value = "";
  } catch (error) {
    platform.value = null;
    platformError.value = error instanceof ApiError ? error.message : "平台健康检查失败";
  }
}

/** 刷新全部区块。 */
async function refresh(): Promise<void> {
  await Promise.all([loadOverview(), loadHealth(), loadPlatform()]);
}

onMounted(() => {
  void refresh();
});
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / Overview</div>
        <h2>运行概览</h2>
        <p>资产规模、事件处置进度与引擎健康。</p>
      </div>
      <div class="ph-actions">
        <button class="btn btn-mo" type="button" :disabled="loading" @click="refresh">
          <span v-if="loading" class="spin" aria-hidden="true"></span>
          <AppIcon v-else name="refresh" :size="13" />
          刷新
        </button>
        <RouterLink v-if="auth.can('write')" to="/events">
          <button class="btn btn-zhu" type="button">事件中心</button>
        </RouterLink>
      </div>
    </div>

    <div v-if="loadError" class="err-box" role="alert" style="margin-bottom: var(--sp-4)">
      <AppIcon name="events" :size="14" />
      <span>{{ loadError }}</span>
    </div>

    <!-- 指标条：一栏分隔，首屏纵向更省，数字等宽方便扫读 -->
    <div class="stat-strip">
      <div class="stat-cell stat-cell--asset">
        <div class="sl"><AppIcon name="assets" :size="12" />受管资产</div>
        <div class="sv">
          <span v-if="!kpiReady" class="t-dim">—</span>
          <span v-else>{{ kpi.assets }}<span class="unit">台</span></span>
        </div>
        <div class="ss">已登记入 CMDB 的主机 / 数据库 / 中间件</div>
      </div>
      <div class="stat-cell stat-cell--danger">
        <div class="sl"><AppIcon name="events" :size="12" />待处置事件</div>
        <div class="sv" :class="{ danger: kpi.open > 0 }">
          <span v-if="!kpiReady" class="t-dim">—</span>
          <span v-else>{{ kpi.open }}<span class="unit">条</span></span>
        </div>
        <div class="ss">状态为「待处置」，需人工认领</div>
      </div>
      <div class="stat-cell stat-cell--event">
        <div class="sl">P1 严重事件</div>
        <div class="sv">
          <span v-if="!kpiReady" class="t-dim">—</span>
          <span v-else>{{ kpi.critical }}<span class="unit">条</span></span>
        </div>
        <div class="ss">历史累计（含已闭环）</div>
      </div>
      <div class="stat-cell stat-cell--asset">
        <div class="sl">已恢复事件</div>
        <div class="sv ok">
          <span v-if="!kpiReady" class="t-dim">—</span>
          <span v-else>{{ kpi.resolved }}<span class="unit">条</span></span>
        </div>
        <div class="ss">已确认恢复，等待关闭归档</div>
      </div>
    </div>

    <div class="split split-main">
      <!-- 主区：最近事件 -->
      <div class="card card--flat">
        <div class="card-head">
          <div>
            <h3>最近事件</h3>
            <div class="sub">按发生时间倒序，最多 6 条</div>
          </div>
          <RouterLink to="/events"><button class="btn btn-mo btn-sm" type="button">全部事件</button></RouterLink>
        </div>

        <div v-if="loading" class="loading-row">
          <span class="spin" aria-hidden="true"></span>加载中…
        </div>
        <div v-else-if="!recent.length" class="empty">
          <div class="em-ico"><AppIcon name="events" :size="20" /></div>
          <div class="em-act">
            <RouterLink v-if="auth.can('write')" to="/events">
              <button class="btn btn-sm" type="button">去登记事件</button>
            </RouterLink>
          </div>
        </div>
        <div v-else class="timeline">
          <div
            v-for="event in recent"
            :key="event.id"
            class="tl-item"
            :class="{ err: event.severity === 'critical', run: event.status === 'diagnosing' }"
          >
            <div class="tt">
              {{ formatShortTime(event.createdAt) }} · {{ event.eventNo }}
            </div>
            <div class="tb">
              <RouterLink :to="`/events/${event.id}`" class="link link--strong">{{ event.title }}</RouterLink>
              <span class="bdg bdg--sev" :class="SEVERITY_BADGE[event.severity]" style="margin-left: 6px">
                {{ labelOf(SEVERITY_LABELS, event.severity) }}
              </span>
              <span class="bdg" :class="EVENT_STATUS_BADGE[event.status]" style="margin-left: 4px">
                {{ labelOf(EVENT_STATUS_LABELS, event.status) }}
              </span>
            </div>
            <div class="tb" style="font-size: 10.5px; color: var(--mo-4)">
              {{ event.assetName || "未关联资产" }}
              <span v-if="event.environmentName"> · {{ event.environmentName }}</span>
              <span v-if="event.source"> · 来源 {{ event.source }}</span>
              · {{ formatRelative(event.createdAt) }}
            </div>
          </div>
        </div>
      </div>

      <!-- 侧栏元信息：系统状态（sticky，滚动主区时保持可见） -->
      <div class="side-meta">
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>系统状态</h3>
              <div class="sub">引擎与存储的实时健康</div>
            </div>
            <button class="btn btn-mo btn-sm" type="button" @click="refresh">重新检测</button>
          </div>

          <div class="kv">
            <div class="k">引擎状态</div>
            <div class="v">
              <span v-if="healthLoading" class="t-dim">检测中…</span>
              <span v-else-if="health" class="bdg bdg--success bdg--pulse" style="margin: 0">
                <span class="d"></span>{{ health.status }}
              </span>
              <span v-else class="bdg bdg--danger bdg--pulse" style="margin: 0">
                <span class="d"></span>{{ healthError || "不可用" }}
              </span>
            </div>
          </div>
          <div class="kv">
            <div class="k">向量规模</div>
            <div class="v mono">
              {{ health ? health.vector_count : "—" }}
              <span class="dim" style="margin-left: 6px">条索引</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">图谱规模</div>
            <div class="v mono">
              {{ health ? health.graph_nodes : "—" }}
              <span class="dim" style="margin-left: 6px">节点</span>
              <span class="dim" style="margin: 0 6px">/</span>
              {{ health ? health.graph_edges : "—" }}
              <span class="dim" style="margin-left: 6px">边</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">平台存储</div>
            <div class="v">
              <template v-if="platform">
                {{ storageText }}
                <span class="dim" style="margin-left: 6px">v{{ platform.version }}</span>
              </template>
              <span v-else class="t-dim">{{ platformError || "—" }}</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">数据行数</div>
            <div class="v mono">
              <template v-if="platform">
                用户 {{ platform.rows.users }} · 资产 {{ platform.rows.assets }} · 事件 {{ platform.rows.events }}
              </template>
              <span v-else class="t-dim">—</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">当前用户</div>
            <div class="v">
              {{ auth.displayName || "—" }}
              <span class="bdg bdg--neutral" style="margin-left: 8px">{{ roleText || "未知角色" }}</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">上次登录</div>
            <div class="v mono">{{ auth.user?.lastLoginAt ? formatRelative(auth.user.lastLoginAt) : "—" }}</div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
