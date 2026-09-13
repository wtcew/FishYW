<script setup lang="ts">
/**
 * 事件与研判列表（事件中心）。
 *
 * 顶部 KPI 四卡 + 状态 Tab（带计数，服务端 total）+ 严重度色条表格。
 * 计数与列表分离加载：切 Tab 只重查列表，手动刷新时一并更新计数。
 *
 * 数据来源：`GET /events`（列表与计数共用，计数用 page_size=1 只取 total）、
 * `POST /events`（手工登记，operator+）、`POST /events/{id}/acknowledge`（快速认领）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { listAssets } from "@/api/assets";
import { acknowledgeEvent, createEvent, listEvents } from "@/api/events";
import { ApiError } from "@/api/http";
import type { Asset, EventSeverity, OpsEvent } from "@/api/types";
import { EVENT_TRANSITIONS } from "@/api/types";
import { useEventCounts } from "@/composables/useEventCounts";
import AppIcon from "@/components/AppIcon.vue";
import Pagination from "@/components/Pagination.vue";
import {
  EVENT_STATUS_BADGE,
  EVENT_STATUS_LABELS,
  SEVERITY_BADGE,
  SEVERITY_LABELS,
  labelOf,
} from "@/lib/labels";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useAuthStore } from "@/stores/auth";
import { useFilterStore } from "@/stores/filter";
import { pushToast } from "@/composables/useToast";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const filter = useFilterStore();

/** 状态 Tab 定义（顺序即处置生命周期）。 */
const STATUS_TABS: Array<{ value: string; label: string }> = [
  { value: "", label: "全部" },
  { value: "open", label: "待处置" },
  { value: "acknowledged", label: "已认领" },
  { value: "diagnosing", label: "研判中" },
  { value: "resolved", label: "已恢复" },
  { value: "closed", label: "已关闭" },
];

const SEVERITY_OPTIONS: Array<{ value: EventSeverity; label: string }> = [
  { value: "critical", label: "P1 严重" },
  { value: "major", label: "P2 重要" },
  { value: "minor", label: "P3 次要" },
  { value: "info", label: "P4 提示" },
];

/** 列表状态。 */
const rows = ref<OpsEvent[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const loading = ref(false);
const errorText = ref("");

/** 过滤条件（状态与严重度本地；业务线/环境走全局上下文）。 */
const status = ref<string>(typeof route.query.status === "string" ? route.query.status : "");
const severity = ref("");
const keyword = ref("");
const keywordDraft = ref("");
const assetId = ref<number | null>(
  route.query.assetId && Number.isFinite(Number(route.query.assetId)) ? Number(route.query.assetId) : null,
);

/**
 * 计数（KPI + Tab 徽标）：走 composables/useEventCounts ——
 * 20s 缓存 + 仅「计数相关条件」变化时重算，切 Tab 不再打 6 个 count 请求。
 */
const { counts, ready: countsReady, load: loadCounts, invalidate: invalidateCounts } = useEventCounts();

/** 手工登记表单。 */
const formOpen = ref(false);
const form = ref({ title: "", severity: "major" as EventSeverity, assetId: null as number | null });
const formPending = ref(false);
const formError = ref("");
const assets = ref<Asset[]>([]);

/** 行内认领中的事件 ID。 */
const ackPending = ref<number | null>(null);

let abort: AbortController | null = null;

/** 处置中数量（已认领 + 研判中），用于 KPI。 */
const inProgress = computed(() => (counts.value.acknowledged) + (counts.value.diagnosing));
/** 已闭环数量。 */
const closedTotal = computed(() => (counts.value.resolved) + (counts.value.closed));

/** 当前过滤条件描述（空态文案用）。 */
const filtered = computed(
  () =>
    Boolean(
      status.value || severity.value || keyword.value || assetId.value || filter.businessLineId || filter.environmentId,
    ),
);

/** 加载列表。 */
async function load(): Promise<void> {
  abort?.abort();
  abort = new AbortController();
  loading.value = true;
  errorText.value = "";
  try {
    const result = await listEvents(
      {
        page: page.value,
        pageSize: pageSize.value,
        status: status.value || undefined,
        severity: severity.value || undefined,
        assetId: assetId.value,
        businessLineId: filter.businessLineId,
        keyword: keyword.value || undefined,
      },
      abort.signal,
    );
    rows.value = result.items;
    total.value = result.total;
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    errorText.value = error instanceof ApiError ? error.message : "事件列表加载失败";
    rows.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

/** 加载资产下拉（手工登记用）。 */
async function loadAssets(): Promise<void> {
  try {
    const result = await listAssets({ page: 1, pageSize: 100 });
    assets.value = result.items;
  } catch {
    assets.value = [];
  }
}

/** 影响计数的过滤条件（不含 status）。 */
const countFilter = computed(() => ({
  businessLineId: filter.businessLineId,
  assetId: assetId.value,
  severity: severity.value,
  keyword: keyword.value,
}));

/** 手动刷新：列表 + 计数一起更新（绕过缓存）。 */
async function refresh(): Promise<void> {
  invalidateCounts();
  await Promise.all([load(), loadCounts(countFilter.value, true)]);
}

/**
 * 切换状态 Tab。
 *
 * @param value 状态值（空串为全部）。
 */
function switchStatus(value: string): void {
  status.value = value;
  page.value = 1;
}

/** 应用关键字。 */
function applyKeyword(): void {
  keyword.value = keywordDraft.value.trim();
  page.value = 1;
}

/** 重置过滤（含全局上下文与落地的 assetId）。 */
function resetFilters(): void {
  filter.reset();
  status.value = "";
  severity.value = "";
  keyword.value = "";
  keywordDraft.value = "";
  assetId.value = null;
  page.value = 1;
  void router.replace({ name: "event-list" });
}

/**
 * 取消按资产过滤。
 *
 * 必须同步清掉 URL 上的 assetId，否则刷新页面后过滤会复活
 * （2026-09-13 UI 穷举测试 BUG-06）。显式 delete 键而非置 undefined——
 * 后者在 Vue Router 序列化时行为不可靠（实测 URL 仍残留 assetId）。
 */
function clearAssetFilter(): void {
  assetId.value = null;
  page.value = 1;
  const query = { ...route.query };
  delete query.assetId;
  void router.replace({ name: "event-list", query });
}

/** 打开手工登记弹窗。 */
function openForm(): void {
  form.value = { title: "", severity: "major", assetId: null };
  formError.value = "";
  formOpen.value = true;
}

/** 提交手工登记。 */
async function submitForm(): Promise<void> {
  if (formPending.value) return;
  if (!form.value.title.trim()) {
    formError.value = "请填写事件标题";
    return;
  }
  formPending.value = true;
  formError.value = "";
  try {
    const created = await createEvent({
      title: form.value.title.trim(),
      severity: form.value.severity,
      assetId: form.value.assetId,
    });
    pushToast(`已登记事件 ${created.eventNo}`, "success");
    formOpen.value = false;
    page.value = 1;
    invalidateCounts();
    await Promise.all([load(), loadCounts(countFilter.value, true)]);
  } catch (error) {
    formError.value = error instanceof ApiError ? error.message : "登记失败";
  } finally {
    formPending.value = false;
  }
}

/**
 * 行内认领（open → acknowledged）。
 *
 * @param event 目标事件。
 */
async function acknowledge(event: OpsEvent): Promise<void> {
  if (ackPending.value !== null) return;
  ackPending.value = event.id;
  try {
    await acknowledgeEvent(event.id);
    pushToast(`已认领 ${event.eventNo}`, "success");
    invalidateCounts();
    await Promise.all([load(), loadCounts(countFilter.value, true)]);
  } catch {
    // 错误提示由 http 层统一弹出（409 会说明非法流转原因）
  } finally {
    ackPending.value = null;
  }
}

/**
 * 某事件是否允许在列表中直接认领。
 *
 * @param event 事件。
 * @returns 允许返回 true。
 */
function canAcknowledge(event: OpsEvent): boolean {
  return auth.can("write") && (EVENT_TRANSITIONS[event.status] ?? []).includes("acknowledged") && event.status === "open";
}

// 过滤条件变化 → 重查列表 + 重算计数（计数只依赖这些条件）
watch(
  () => [severity.value, keyword.value, assetId.value, filter.businessLineId, filter.environmentId],
  () => {
    page.value = 1;
    void load();
    void loadCounts(countFilter.value);
  },
);
// 状态 Tab / 翻页：只重查列表（计数与 status 无关，20s 内命中缓存）
watch(() => [status.value, page.value, pageSize.value], () => void load());

onMounted(async () => {
  await Promise.all([load(), loadCounts(countFilter.value), loadAssets()]);
});

onUnmounted(() => abort?.abort());
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / Event Center</div>
        <h2>事件与研判</h2>
        <p>
          外部告警（webhook）与手工登记汇聚为事件，按状态机流转并支持 AI 研判。
          <span class="ph-count">当前过滤命中 {{ total }} 条</span>
        </p>
      </div>
      <div class="ph-actions">
        <button class="btn btn-mo" type="button" :disabled="loading" @click="refresh">
          <span v-if="loading" class="spin" aria-hidden="true"></span>
          <AppIcon v-else name="refresh" :size="13" />
          刷新
        </button>
        <RouterLink v-if="auth.isAdmin" to="/admin?tab=rules">
          <button class="btn btn-mo" type="button">告警规则</button>
        </RouterLink>
        <button v-if="auth.can('write')" class="btn btn-zhu" type="button" @click="openForm">
          <AppIcon name="plus" :size="13" />
          登记事件
        </button>
      </div>
    </div>

    <!-- 指标条（一栏分隔，替换旧四卡，纵向省 30px 且数字等宽易扫读） -->
    <div class="stat-strip">
      <div class="stat-cell stat-cell--asset">
        <div class="sl">事件总数</div>
        <div class="sv">
          <span v-if="countsReady">{{ counts.all }}<span class="unit">条</span></span>
          <span v-else class="t-dim">—</span>
        </div>
        <div class="ss">当前过滤条件下的累计事件</div>
      </div>
      <div class="stat-cell stat-cell--danger">
        <div class="sl">待处置</div>
        <div class="sv" :class="{ danger: counts.open > 0 }">
          <span v-if="countsReady">{{ counts.open }}<span class="unit">条</span></span>
          <span v-else class="t-dim">—</span>
        </div>
        <div class="ss">open 态，需人工认领</div>
      </div>
      <div class="stat-cell stat-cell--event">
        <div class="sl">处置中</div>
        <div class="sv warn">
          <span v-if="countsReady">{{ inProgress }}<span class="unit">条</span></span>
          <span v-else class="t-dim">—</span>
        </div>
        <div class="ss">已认领 + 研判中</div>
      </div>
      <div class="stat-cell stat-cell--asset">
        <div class="sl">已闭环</div>
        <div class="sv ok">
          <span v-if="countsReady">{{ closedTotal }}<span class="unit">条</span></span>
          <span v-else class="t-dim">—</span>
        </div>
        <div class="ss">已恢复 + 已关闭</div>
      </div>
    </div>

    <!-- 状态 Tab -->
    <div class="tabs" role="tablist" aria-label="事件状态">
      <button
        v-for="tab in STATUS_TABS"
        :key="tab.value"
        class="tab"
        type="button"
        role="tab"
        :aria-selected="status === tab.value"
        @click="switchStatus(tab.value)"
      >
        {{ tab.label }}
        <span v-if="countsReady" class="tab-count">{{ tab.value ? (counts[tab.value as keyof typeof counts] ?? 0) : counts.all }}</span>
      </button>
    </div>

    <!-- 筛选栏 -->
    <div class="filter-bar">
      <label class="fbi">
        <span>严重度</span>
        <select v-model="severity" class="ctl">
          <option value="">全部</option>
          <option v-for="item in SEVERITY_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <label class="fbi grow">
        <span>关键字</span>
        <input
          v-model="keywordDraft"
          class="ctl"
          type="search"
          placeholder="按标题或事件编号搜索，回车查询"
          @keydown.enter.prevent="applyKeyword"
        />
      </label>
      <div class="fb-acts">
        <button class="btn btn-sm" type="button" @click="applyKeyword">查询</button>
        <button class="btn btn-sm btn-ghost" type="button" @click="resetFilters">重置</button>
      </div>
    </div>

    <div v-if="assetId" class="alert-entry" style="margin-bottom: var(--sp-4)">
      <AppIcon name="assets" :size="14" />
      <span style="flex: 1">正在按资产 #{{ assetId }} 过滤关联事件。</span>
      <button class="btn btn-sm btn-ghost" type="button" @click="clearAssetFilter">取消该过滤</button>
    </div>

    <!-- 列表 -->
    <div v-if="loading" class="tbl-wrap">
      <div class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
    </div>

    <div v-else-if="errorText" class="err-box" role="alert">
      <AppIcon name="events" :size="14" />
      <span style="flex: 1">{{ errorText }}</span>
      <button class="btn btn-sm" type="button" @click="load">重试</button>
    </div>

    <div v-else-if="!rows.length" class="tbl-wrap">
      <div class="empty">
        <div class="em-ico"><AppIcon name="events" :size="20" /></div>
        <template v-if="filtered">没有符合当前条件的事件。</template>
        <template v-else>暂无事件。</template>
        <div class="em-act">
          <button v-if="filtered" class="btn btn-sm" type="button" @click="resetFilters">清除过滤</button>
          <button v-else-if="auth.can('write')" class="btn btn-sm btn-zhu" type="button" @click="openForm">
            登记事件
          </button>
        </div>
      </div>
    </div>

    <template v-else>
      <div class="tbl-wrap">
        <table class="tbl">
          <caption class="sr-only">事件列表</caption>
          <colgroup>
            <col style="width: 150px" />
            <col />
            <col style="width: 92px" />
            <col style="width: 92px" />
            <col style="width: 132px" />
            <col style="width: 116px" />
            <col style="width: 96px" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">事件编号 / 来源</th>
              <th scope="col">标题</th>
              <th scope="col">严重度</th>
              <th scope="col">状态</th>
              <th scope="col">影响资产</th>
              <th scope="col">发生时间</th>
              <th scope="col" class="ta-r">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in rows" :key="event.id" :data-sev="event.severity">
              <td>
                <span class="mono">{{ event.eventNo }}</span>
                <div class="cell-sub">{{ event.source }}</div>
              </td>
              <td>
                <RouterLink :to="`/events/${event.id}`" class="link link--strong">{{ event.title }}</RouterLink>
                <div v-if="event.acknowledgedByName" class="cell-sub">认领人 {{ event.acknowledgedByName }}</div>
              </td>
              <td>
                <span class="bdg bdg--sev" :class="SEVERITY_BADGE[event.severity]">
                  {{ labelOf(SEVERITY_LABELS, event.severity) }}
                </span>
              </td>
              <td>
                <span class="bdg" :class="EVENT_STATUS_BADGE[event.status]">
                  {{ labelOf(EVENT_STATUS_LABELS, event.status) }}
                </span>
              </td>
              <td class="t-dim">
                <template v-if="event.assetId">
                  <RouterLink :to="`/assets/${event.assetId}`" class="link">{{ event.assetName || "资产" }}</RouterLink>
                  <div v-if="event.environmentName" class="cell-sub">{{ event.environmentName }}</div>
                </template>
                <span v-else>未关联</span>
              </td>
              <td class="t-dim nowrap">
                {{ formatDateTime(event.createdAt) }}
                <div class="cell-sub">{{ formatRelative(event.createdAt) }}</div>
              </td>
              <td>
                <div class="row-actions">
                  <RouterLink :to="`/events/${event.id}`">
                    <button class="btn btn-sm btn-ghost" type="button">详情</button>
                  </RouterLink>
                  <button
                    v-if="canAcknowledge(event)"
                    class="btn btn-sm btn-ghost"
                    type="button"
                    :disabled="ackPending === event.id"
                    @click="acknowledge(event)"
                  >
                    认领
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <Pagination v-model:page="page" v-model:page-size="pageSize" :total="total" :disabled="loading" />
    </template>

    <!-- 手工登记 -->
    <div v-if="formOpen" class="modal-mask" role="dialog" aria-modal="true" aria-labelledby="event-form-title">
      <div class="modal">
        <div class="modal-head">
          <div>
            <h3 id="event-form-title">登记事件</h3>
            <div class="modal-sub">手工登记的事件来源记为 manual，状态为待处置</div>
          </div>
          <button class="modal-close" type="button" aria-label="关闭" @click="formOpen = false">
            <AppIcon name="close" :size="14" />
          </button>
        </div>

        <form class="modal-body" novalidate @submit.prevent="submitForm">
          <div class="frm-grid">
            <div class="field full">
              <label for="ev-title">事件标题<span class="req">*</span></label>
              <input
                id="ev-title"
                v-model="form.title"
                class="ctl"
                maxlength="255"
                placeholder="如：订单库主实例连接数超阈值"
                :disabled="formPending"
              />
            </div>
            <div class="field">
              <label for="ev-sev">严重度</label>
              <select id="ev-sev" v-model="form.severity" class="ctl" :disabled="formPending">
                <option v-for="item in SEVERITY_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
              </select>
            </div>
            <div class="field">
              <label for="ev-asset">关联资产</label>
              <select id="ev-asset" v-model.number="form.assetId" class="ctl" :disabled="formPending">
                <option :value="null">不关联</option>
                <option v-for="asset in assets" :key="asset.id" :value="asset.id">
                  {{ asset.name }}（{{ asset.identifier }}）
                </option>
              </select>
              <div class="ctl-hint">关联资产后，研判会带上该资产的上下文与证据编号。</div>
            </div>
          </div>
          <p v-if="formError" class="ctl-err" role="alert" style="margin-top: var(--sp-3)">{{ formError }}</p>
        </form>

        <div class="modal-foot">
          <button class="btn btn-mo" type="button" :disabled="formPending" @click="formOpen = false">取消</button>
          <button class="btn btn-zhu" type="button" :disabled="formPending" @click="submitForm">
            <span v-if="formPending" class="spin" aria-hidden="true"></span>
            登记
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
