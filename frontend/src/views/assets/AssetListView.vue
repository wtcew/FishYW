<script setup lang="ts">
/**
 * 资产登记列表（资源中心）。
 *
 * 能力：业务线/环境/类型/状态/关键字过滤 + 分页 + 登记/编辑/删除 + 业务线与环境维护。
 * 权限：读 viewer+；登记/编辑 operator+；删除与业务线/环境维护 admin。
 * 业务上下文（当前业务线/环境）写入 stores/filter，跨页保持一致。
 *
 * 三态：loading（骨架行）/ error（原地提示 + 重试）/ success（表格或空态）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";

import { deleteAsset, listAssets, listBusinessLines, listEnvironments } from "@/api/assets";
import { listUsers } from "@/api/admin";
import { ApiError } from "@/api/http";
import type { Asset, BusinessLine, Environment, User } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";
import Pagination from "@/components/Pagination.vue";
import { ASSET_STATUS_BADGE, ASSET_STATUS_LABELS, ASSET_TYPE_LABELS, labelOf } from "@/lib/labels";
import { formatRelative } from "@/lib/format";
import { useAuthStore } from "@/stores/auth";
import { useFilterStore } from "@/stores/filter";
import { pushToast } from "@/composables/useToast";
import AssetForm from "./components/AssetForm.vue";
import ScopeManager from "./components/ScopeManager.vue";

const auth = useAuthStore();
const filter = useFilterStore();

const ASSET_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "host", label: "主机" },
  { value: "db", label: "数据库" },
  { value: "middleware", label: "中间件" },
  { value: "app", label: "应用" },
  { value: "network", label: "网络设备" },
];
const ASSET_STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "active", label: "运行中" },
  { value: "maintenance", label: "维护中" },
  { value: "decommissioned", label: "已下线" },
];

/** 列表数据。 */
const rows = ref<Asset[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const loading = ref(false);
const errorText = ref("");

/** 过滤条件（业务线/环境与全局上下文同步）。 */
const assetType = ref("");
const status = ref("");
const keyword = ref("");
const keywordDraft = ref("");

/** 下拉数据源。 */
const businessLines = ref<BusinessLine[]>([]);
const environments = ref<Environment[]>([]);
const users = ref<User[]>([]);

/** 弹窗与删除确认。 */
const formOpen = ref(false);
const editing = ref<Asset | null>(null);
const scopeOpen = ref(false);
const removing = ref<Asset | null>(null);
const removePending = ref(false);
const removeError = ref("");

let abort: AbortController | null = null;

/** 环境候选：按当前业务线过滤（未选业务线则全部）。 */
const environmentOptions = computed(() =>
  filter.businessLineId
    ? environments.value.filter((env) => env.businessLineId === filter.businessLineId)
    : environments.value,
);

/** 表格是否处于「有过滤条件」状态（空态文案区分）。 */
const filtered = computed(
  () => Boolean(filter.businessLineId || filter.environmentId || assetType.value || status.value || keyword.value),
);

/** 加载业务线/环境（表单与筛选共用）。 */
async function loadScopes(): Promise<void> {
  try {
    const [lines, envs] = await Promise.all([listBusinessLines(), listEnvironments()]);
    businessLines.value = lines;
    environments.value = envs;
  } catch {
    // 下拉失败不阻断列表；具体错误由列表请求统一提示。
  }
}

/** 管理员额外拉取用户列表（负责人在表单里可选）。 */
async function loadUsers(): Promise<void> {
  if (!auth.isAdmin) {
    users.value = [];
    return;
  }
  try {
    users.value = await listUsers();
  } catch {
    users.value = [];
  }
}

/** 加载资产列表。 */
async function load(): Promise<void> {
  abort?.abort();
  abort = new AbortController();
  loading.value = true;
  errorText.value = "";
  try {
    const result = await listAssets(
      {
        page: page.value,
        pageSize: pageSize.value,
        businessLineId: filter.businessLineId,
        environmentId: filter.environmentId,
        assetType: assetType.value,
        status: status.value,
        keyword: keyword.value,
      },
      abort.signal,
    );
    rows.value = result.items;
    total.value = result.total;
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    errorText.value = error instanceof ApiError ? error.message : "资产列表加载失败";
    rows.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

/**
 * 业务线切换：清空环境并按新业务线重查。
 *
 * @param event 下拉 change 事件。
 */
function onBusinessLineChange(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value;
  filter.setBusinessLine(raw ? Number(raw) : null);
}

/**
 * 环境切换。
 *
 * @param event 下拉 change 事件。
 */
function onEnvironmentChange(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value;
  filter.setEnvironment(raw ? Number(raw) : null);
}

/** 应用关键字查询（回车或点「查询」）。 */
function applyKeyword(): void {
  keyword.value = keywordDraft.value.trim();
  page.value = 1;
}

/** 重置全部过滤条件。 */
function resetFilters(): void {
  filter.reset();
  assetType.value = "";
  status.value = "";
  keyword.value = "";
  keywordDraft.value = "";
  page.value = 1;
}

/**
 * 打开登记弹窗。
 *
 * @param asset 编辑目标；不传为新建。
 */
function openForm(asset: Asset | null = null): void {
  editing.value = asset;
  formOpen.value = true;
}

/** 保存成功：关闭弹窗、刷新列表与业务上下文计数。 */
async function onSaved(message: string): Promise<void> {
  formOpen.value = false;
  editing.value = null;
  pushToast(message, "success");
  await Promise.all([load(), loadScopes()]);
}

/** 删除确认提交。 */
async function confirmRemove(): Promise<void> {
  const target = removing.value;
  if (!target || removePending.value) return;
  removePending.value = true;
  removeError.value = "";
  try {
    await deleteAsset(target.id);
    pushToast(`已删除资产「${target.name}」`, "success");
    removing.value = null;
    await Promise.all([load(), loadScopes()]);
  } catch (error) {
    removeError.value = error instanceof ApiError ? error.message : "删除失败";
  } finally {
    removePending.value = false;
  }
}

// 业务上下文或过滤条件变化即重查（关键字走显式提交）。
watch(
  () => [filter.businessLineId, filter.environmentId, assetType.value, status.value, keyword.value, page.value, pageSize.value],
  () => {
    void load();
  },
);

onMounted(async () => {
  await Promise.all([loadScopes(), loadUsers()]);
  void load();
});

onUnmounted(() => abort?.abort());
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / Resource Center</div>
        <h2>资产登记</h2>
        <p>
          业务线 → 环境 → 资产的统一登记簿；标识（环境 + 类型 + 标识）唯一，事件与研判据此定位影响面。
          <span class="ph-count">当前 {{ total }} 条</span>
        </p>
      </div>
      <div class="ph-actions">
        <button class="btn btn-mo" type="button" :disabled="loading" @click="load">
          <span v-if="loading" class="spin" aria-hidden="true"></span>
          <AppIcon v-else name="refresh" :size="13" />
          刷新
        </button>
        <button v-if="auth.isAdmin" class="btn btn-mo" type="button" @click="scopeOpen = true">
          业务线 / 环境
        </button>
        <button v-if="auth.can('write')" class="btn btn-zhu" type="button" @click="openForm(null)">
          <AppIcon name="plus" :size="13" />
          登记资产
        </button>
      </div>
    </div>

    <!-- 筛选栏 -->
    <div class="filter-bar">
      <label class="fbi">
        <span>业务线</span>
        <select class="ctl" :value="filter.businessLineId ?? ''" @change="onBusinessLineChange">
          <option value="">全部</option>
          <option v-for="line in businessLines" :key="line.id" :value="line.id">
            {{ line.name }}（{{ line.environmentCount }} 环境）
          </option>
        </select>
      </label>
      <label class="fbi">
        <span>环境</span>
        <select class="ctl" :value="filter.environmentId ?? ''" @change="onEnvironmentChange">
          <option value="">全部</option>
          <option v-for="env in environmentOptions" :key="env.id" :value="env.id">
            {{ env.name }}（{{ env.assetCount }} 资产）
          </option>
        </select>
      </label>
      <label class="fbi">
        <span>类型</span>
        <select v-model="assetType" class="ctl">
          <option value="">全部</option>
          <option v-for="item in ASSET_TYPE_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <label class="fbi">
        <span>状态</span>
        <select v-model="status" class="ctl">
          <option value="">全部</option>
          <option v-for="item in ASSET_STATUS_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <label class="fbi grow">
        <span>关键字</span>
        <input
          v-model="keywordDraft"
          class="ctl"
          type="search"
          placeholder="按资产名称或标识搜索，回车查询"
          @keydown.enter.prevent="applyKeyword"
        />
      </label>
      <div class="fb-acts">
        <button class="btn btn-sm" type="button" @click="applyKeyword">查询</button>
        <button class="btn btn-sm btn-ghost" type="button" @click="resetFilters">重置</button>
      </div>
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
        <div class="em-ico"><AppIcon name="assets" :size="20" /></div>
        <template v-if="filtered">没有符合当前过滤条件的资产。</template>
        <template v-else>暂无资产。</template>
        <div class="em-act">
          <button v-if="filtered" class="btn btn-sm" type="button" @click="resetFilters">清除过滤</button>
          <button v-else-if="auth.can('write')" class="btn btn-sm btn-zhu" type="button" @click="openForm(null)">
            登记第一台资产
          </button>
        </div>
      </div>
    </div>

    <template v-else>
      <div class="tbl-wrap">
        <table class="tbl">
          <caption class="sr-only">资产列表</caption>
          <colgroup>
            <col />
            <col style="width: 96px" />
            <col style="width: 92px" />
            <col style="width: 96px" />
            <col style="width: 96px" />
            <col style="width: 108px" />
            <col style="width: 96px" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">资产名称 / 标识</th>
              <th scope="col">类型</th>
              <th scope="col">环境</th>
              <th scope="col">负责人</th>
              <th scope="col">状态</th>
              <th scope="col">最近变更</th>
              <th scope="col" class="ta-r">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="asset in rows" :key="asset.id" :data-health="asset.status">
              <td>
                <RouterLink :to="`/assets/${asset.id}`" class="link link--strong">{{ asset.name }}</RouterLink>
                <div class="cell-sub">{{ asset.identifier }}</div>
              </td>
              <td>
                <span class="bdg bdg--info">{{ labelOf(ASSET_TYPE_LABELS, asset.assetType) }}</span>
              </td>
              <td class="t-dim">
                {{ asset.environmentName || "—" }}
                <div v-if="asset.businessLineName" class="cell-sub">{{ asset.businessLineName }}</div>
              </td>
              <td class="t-dim">
                {{ asset.ownerName || "未指定" }}
                <div v-if="asset.ownerContact" class="cell-sub">{{ asset.ownerContact }}</div>
              </td>
              <td>
                <span class="bdg" :class="ASSET_STATUS_BADGE[asset.status]">
                  {{ labelOf(ASSET_STATUS_LABELS, asset.status) }}
                </span>
              </td>
              <td class="t-dim nowrap">{{ formatRelative(asset.updatedAt) }}</td>
              <td>
                <div class="row-actions">
                  <RouterLink :to="`/assets/${asset.id}`">
                    <button class="btn btn-sm btn-ghost" type="button">详情</button>
                  </RouterLink>
                  <button v-if="auth.can('write')" class="btn btn-sm btn-ghost" type="button" @click="openForm(asset)">
                    编辑
                  </button>
                  <button
                    v-if="auth.isAdmin"
                    class="btn btn-sm btn-ghost-danger"
                    type="button"
                    @click="removing = asset"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <Pagination
        v-model:page="page"
        v-model:page-size="pageSize"
        :total="total"
        :disabled="loading"
      />
    </template>

    <!-- 登记 / 编辑 -->
    <AssetForm
      :open="formOpen"
      :asset="editing"
      :business-lines="businessLines"
      :environments="environments"
      :users="users"
      @close="formOpen = false"
      @saved="onSaved"
    />

    <!-- 业务线 / 环境维护 -->
    <ScopeManager :open="scopeOpen" @close="scopeOpen = false" @changed="loadScopes" />

    <!-- 删除确认 -->
    <div v-if="removing" class="modal-mask" role="dialog" aria-modal="true" aria-labelledby="asset-del-title">
      <div class="modal">
        <div class="modal-head">
          <h3 id="asset-del-title">删除资产</h3>
          <button class="modal-close" type="button" aria-label="关闭" @click="removing = null">
            <AppIcon name="close" :size="14" />
          </button>
        </div>
        <div class="modal-body">
          <p style="font-size: 12.5px; line-height: 1.85">
            确认删除资产
            <b>{{ removing.name }}</b>
            （{{ removing.identifier }}）？该操作不可撤销；若资产已关联事件，后端会拒绝删除，建议改为「已下线」。
          </p>
          <p v-if="removeError" class="ctl-err" role="alert" style="margin-top: var(--sp-3)">{{ removeError }}</p>
        </div>
        <div class="modal-foot">
          <button class="btn btn-mo" type="button" :disabled="removePending" @click="removing = null">取消</button>
          <button class="btn btn-danger" type="button" :disabled="removePending" @click="confirmRemove">
            <span v-if="removePending" class="spin" aria-hidden="true"></span>
            确认删除
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
