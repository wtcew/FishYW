<script setup lang="ts">
/**
 * 资产详情：左侧概览卡（sticky）+ 右侧关系与影响面 / 变更历史。
 *
 * 数据来源（全部真实接口）：
 * - `GET /assets/{id}` 资产本体；
 * - `GET /events?asset_id=` 该资产的关联事件（影响面）；
 * - `GET /admin/audit-logs?resource_type=asset` 变更历史（**仅管理员**，非管理员给出来源说明）。
 *   注意：后端审计接口不支持按 resource_id 过滤，这里取最近 100 条后在前端按资源 ID 过滤。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { deleteAsset, getAsset, listBusinessLines, listEnvironments } from "@/api/assets";
import { listAuditLogs, listUsers } from "@/api/admin";
import { listEvents } from "@/api/events";
import { ApiError } from "@/api/http";
import type { Asset, AuditLog, BusinessLine, Environment, OpsEvent, User } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";
import {
  ASSET_STATUS_BADGE,
  ASSET_STATUS_LABELS,
  ASSET_TYPE_LABELS,
  EVENT_STATUS_BADGE,
  EVENT_STATUS_LABELS,
  SEVERITY_BADGE,
  SEVERITY_LABELS,
  labelOf,
} from "@/lib/labels";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useAuthStore } from "@/stores/auth";
import { pushToast } from "@/composables/useToast";
import AssetForm from "./components/AssetForm.vue";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const assetId = computed(() => Number(route.params.id));

const asset = ref<Asset | null>(null);
const loading = ref(true);
const errorText = ref("");

/** 关联事件与变更历史各自独立三态。 */
const events = ref<OpsEvent[]>([]);
const eventsTotal = ref(0);
const eventsLoading = ref(true);

const history = ref<AuditLog[]>([]);
const historyLoading = ref(false);
const historyError = ref("");

/** 表单依赖。 */
const businessLines = ref<BusinessLine[]>([]);
const environments = ref<Environment[]>([]);
const users = ref<User[]>([]);
const formOpen = ref(false);

/** 删除确认。 */
const removing = ref(false);
const removePending = ref(false);
const removeError = ref("");

/** 标签（后端 tags 为任意 JSON 数组，这里统一按字符串展示）。 */
const tags = computed(() => (asset.value?.tags ?? []).map((tag) => String(tag)));

/** 加载资产本体。 */
async function loadAsset(): Promise<void> {
  loading.value = true;
  errorText.value = "";
  try {
    asset.value = await getAsset(assetId.value);
  } catch (error) {
    asset.value = null;
    errorText.value = error instanceof ApiError ? error.message : "资产加载失败";
  } finally {
    loading.value = false;
  }
}

/** 加载关联事件（影响面）。 */
async function loadEvents(): Promise<void> {
  eventsLoading.value = true;
  try {
    const result = await listEvents({ assetId: assetId.value, page: 1, pageSize: 8 });
    events.value = result.items;
    eventsTotal.value = result.total;
  } catch {
    events.value = [];
    eventsTotal.value = 0;
  } finally {
    eventsLoading.value = false;
  }
}

/** 加载变更历史（管理员）。 */
async function loadHistory(): Promise<void> {
  if (!auth.isAdmin) {
    historyError.value = "变更历史读取自审计日志，仅管理员可见";
    return;
  }
  historyLoading.value = true;
  historyError.value = "";
  try {
    // 按 resource_id 精确过滤（后端支持）：原实现"取最近 100 条再前端过滤"在
    // 审计总量超过窗口后会让早期对象的变更历史整段消失（2026-09-13 BUG-04）。
    const result = await listAuditLogs(
      { page: 1, pageSize: 50, resourceType: "asset", resourceId: String(assetId.value) },
      undefined,
    );
    history.value = result.items;
  } catch (error) {
    history.value = [];
    historyError.value = error instanceof ApiError ? error.message : "变更历史加载失败";
  } finally {
    historyLoading.value = false;
  }
}

/** 加载表单下拉与用户列表。 */
async function loadFormDeps(): Promise<void> {
  try {
    const [lines, envs] = await Promise.all([listBusinessLines(), listEnvironments()]);
    businessLines.value = lines;
    environments.value = envs;
  } catch {
    /* 下拉失败时表单会提示无可选环境 */
  }
  if (auth.isAdmin) {
    try {
      users.value = await listUsers();
    } catch {
      users.value = [];
    }
  }
}

/** 删除当前资产。 */
async function confirmRemove(): Promise<void> {
  if (removePending.value || !asset.value) return;
  removePending.value = true;
  removeError.value = "";
  try {
    await deleteAsset(asset.value.id);
    pushToast(`已删除资产「${asset.value.name}」`, "success");
    await router.replace({ name: "asset-list" });
  } catch (error) {
    removeError.value = error instanceof ApiError ? error.message : "删除失败";
  } finally {
    removePending.value = false;
  }
}

/** 保存后刷新。 */
async function onSaved(message: string): Promise<void> {
  formOpen.value = false;
  pushToast(message, "success");
  await Promise.all([loadAsset(), loadHistory(), loadEvents()]);
}

onMounted(async () => {
  await Promise.all([loadAsset(), loadEvents(), loadHistory(), loadFormDeps()]);
});
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">
          FishCloud / 资源中心 / <RouterLink to="/assets" class="link">资产登记</RouterLink> / 详情
        </div>
        <h2>{{ asset?.name ?? "资产详情" }}</h2>
        <p v-if="asset">
          {{ labelOf(ASSET_TYPE_LABELS, asset.assetType) }} ·
          <span class="mono">{{ asset.identifier }}</span>
          <span class="ph-count">最近变更 {{ formatRelative(asset.updatedAt) }}</span>
        </p>
      </div>
      <div class="ph-actions">
        <button class="btn btn-mo" type="button" @click="router.back()">
          <AppIcon name="back" :size="13" />
          返回
        </button>
        <button v-if="auth.can('write') && asset" class="btn btn-zhu" type="button" @click="formOpen = true">
          编辑资产
        </button>
      </div>
    </div>

    <div v-if="loading" class="card">
      <div class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
    </div>

    <div v-else-if="errorText" class="err-box" role="alert">
      <AppIcon name="events" :size="14" />
      <span style="flex: 1">{{ errorText }}</span>
      <button class="btn btn-sm" type="button" @click="loadAsset">重试</button>
    </div>

    <div v-else-if="asset" class="split split-main">
      <!-- 主列：关系与影响面 + 变更历史（内容主体） -->
      <div class="main-col">
        <!-- 关系与影响面 -->
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>关系与影响面</h3>
              <div class="sub">该资产在 CMDB 中的归属链路与关联事件</div>
            </div>
            <RouterLink :to="`/events?assetId=${asset.id}`">
              <button class="btn btn-sm btn-mo" type="button">查看全部事件（{{ eventsTotal }}）</button>
            </RouterLink>
          </div>

          <div class="kv">
            <div class="k">归属链路</div>
            <div class="v">
              <span class="mono">{{ asset.businessLineName || "—" }}</span>
              <AppIcon name="chevron" :size="11" style="display: inline-block; vertical-align: -1px" />
              <span class="mono">{{ asset.environmentName || "—" }}</span>
              <AppIcon name="chevron" :size="11" style="display: inline-block; vertical-align: -1px" />
              <span class="mono">{{ asset.name }}</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">关联事件</div>
            <div class="v">
              <template v-if="eventsLoading"><span class="spin" aria-hidden="true"></span></template>
              <template v-else>{{ eventsTotal }} 条（最近 {{ events.length }} 条见下）</template>
            </div>
          </div>

          <div v-if="eventsLoading" class="loading-row" style="padding: 16px">
            <span class="spin" aria-hidden="true"></span>加载中…
          </div>
          <div v-else-if="!events.length" class="empty" style="padding: 18px">该资产暂无关联事件。</div>
          <div v-else class="timeline" style="margin-top: var(--sp-3)">
            <div
              v-for="event in events"
              :key="event.id"
              class="tl-item"
              :class="{ err: event.severity === 'critical', run: event.status === 'diagnosing' }"
            >
              <div class="tt">{{ formatDateTime(event.createdAt) }} · {{ event.eventNo }}</div>
              <div class="tb">
                <RouterLink :to="`/events/${event.id}`" class="link link--strong">{{ event.title }}</RouterLink>
                <span class="bdg bdg--sev" :class="SEVERITY_BADGE[event.severity]" style="margin-left: 6px">
                  {{ labelOf(SEVERITY_LABELS, event.severity) }}
                </span>
                <span class="bdg" :class="EVENT_STATUS_BADGE[event.status]" style="margin-left: 4px">
                  {{ labelOf(EVENT_STATUS_LABELS, event.status) }}
                </span>
              </div>
            </div>
          </div>
        </div>

        <!-- 变更历史 -->
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>变更历史</h3>
              <div class="sub">来自平台审计日志（resource_type = asset）</div>
            </div>
            <button class="btn btn-sm btn-mo" type="button" :disabled="historyLoading" @click="loadHistory">
              刷新
            </button>
          </div>

          <div v-if="historyLoading" class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
          <div v-else-if="historyError" class="empty">{{ historyError }}</div>
          <div v-else-if="!history.length" class="empty">暂无变更记录。</div>
          <div v-else class="timeline">
            <div v-for="row in history" :key="row.id" class="tl-item ok">
              <div class="tt">{{ formatDateTime(row.createdAt) }} · {{ row.ip || "未知来源" }}</div>
              <div class="tb">
                <b>{{ row.action }}</b>
                <span class="tl-who" style="margin-left: 6px">操作人 {{ row.username || "—" }}</span>
              </div>
              <div v-if="row.detail" class="tb mono" style="font-size: 10.5px; color: var(--mo-4)">
                {{ JSON.stringify(row.detail) }}
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 元信息列（资产概览）：右侧常驻，主列滚动时保持可见 -->
      <div class="side-meta">
        <div class="card card--flat">
          <div class="card-head">
            <div>
              <h3>资产概览</h3>
              <div class="sub">{{ asset.environmentName || "未归属环境" }}</div>
            </div>
            <span class="bdg" :class="ASSET_STATUS_BADGE[asset.status]">
              {{ labelOf(ASSET_STATUS_LABELS, asset.status) }}
            </span>
          </div>

          <div class="kv">
            <div class="k">资产类型</div>
            <div class="v"><span class="bdg bdg--info">{{ labelOf(ASSET_TYPE_LABELS, asset.assetType) }}</span></div>
          </div>
          <div class="kv">
            <div class="k">标识</div>
            <div class="v mono">{{ asset.identifier }}</div>
          </div>
          <div class="kv">
            <div class="k">业务线</div>
            <div class="v">{{ asset.businessLineName || "—" }}</div>
          </div>
          <div class="kv">
            <div class="k">环境</div>
            <div class="v">{{ asset.environmentName || "—" }}</div>
          </div>
          <div class="kv">
            <div class="k">负责人</div>
            <div class="v">
              {{ asset.ownerName || "未指定" }}
              <div v-if="asset.ownerContact" class="dim mono" style="font-size: 11px">{{ asset.ownerContact }}</div>
            </div>
          </div>
          <div class="kv">
            <div class="k">标签</div>
            <div class="v">
              <div v-if="tags.length" class="tag-row">
                <span v-for="tag in tags" :key="tag" class="tag">{{ tag }}</span>
              </div>
              <span v-else class="dim">无</span>
            </div>
          </div>
          <div class="kv">
            <div class="k">备注</div>
            <div class="v" style="white-space: pre-wrap">{{ asset.remark || "—" }}</div>
          </div>
          <div class="kv">
            <div class="k">最近变更</div>
            <div class="v mono">{{ formatDateTime(asset.updatedAt) }}</div>
          </div>

          <div v-if="auth.isAdmin" style="margin-top: var(--sp-4); display: flex; justify-content: flex-end">
            <button class="btn btn-sm btn-ghost-danger" type="button" @click="removing = true">删除资产</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 编辑弹窗 -->
    <AssetForm
      :open="formOpen"
      :asset="asset"
      :business-lines="businessLines"
      :environments="environments"
      :users="users"
      @close="formOpen = false"
      @saved="onSaved"
    />

    <!-- 删除确认 -->
    <div v-if="removing" class="modal-mask" role="dialog" aria-modal="true" aria-labelledby="asset-del-title">
      <div class="modal">
        <div class="modal-head">
          <h3 id="asset-del-title">删除资产</h3>
          <button class="modal-close" type="button" aria-label="关闭" @click="removing = false">
            <AppIcon name="close" :size="14" />
          </button>
        </div>
        <div class="modal-body">
          <p style="font-size: 12.5px; line-height: 1.85">
            确认删除资产 <b>{{ asset?.name }}</b>？该操作不可撤销；若存在关联事件，后端会拒绝并提示改为「已下线」。
          </p>
          <p v-if="removeError" class="ctl-err" role="alert" style="margin-top: var(--sp-3)">{{ removeError }}</p>
        </div>
        <div class="modal-foot">
          <button class="btn btn-mo" type="button" :disabled="removePending" @click="removing = false">取消</button>
          <button class="btn btn-danger" type="button" :disabled="removePending" @click="confirmRemove">
            <span v-if="removePending" class="spin" aria-hidden="true"></span>
            确认删除
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
