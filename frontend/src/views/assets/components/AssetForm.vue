<script setup lang="ts">
/**
 * 资产登记/编辑弹窗。
 *
 * 字段与后端 AssetIn 一一对应（snake_case 请求体由 api/assets.ts 转换）。
 * 已知契约缺口：出参 AssetOut **不含 ownerId**，因此编辑时无法回填「负责人」，
 * 这里只对管理员开放该字段（管理员可取用户列表按显示名匹配回填），
 * 非管理员编辑时隐式保留不了该值，故在界面上明确提示。
 *
 * 状态：提交中禁用全部输入；失败在原位给出后端 detail；成功由父组件刷新列表。
 */
import { computed, nextTick, reactive, ref, watch } from "vue";

import { createAsset, updateAsset } from "@/api/assets";
import { ApiError } from "@/api/http";
import type { Asset, AssetPayload, AssetStatus, AssetType, BusinessLine, Environment, User } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";
import { ASSET_STATUS_LABELS, ASSET_TYPE_LABELS } from "@/lib/labels";

const props = withDefaults(
  defineProps<{
    /** 是否可见。 */
    open: boolean;
    /** 编辑目标；null 表示新建。 */
    asset?: Asset | null;
    /** 可选业务线。 */
    businessLines: BusinessLine[];
    /** 可选环境（父组件已按业务线筛选）。 */
    environments: Environment[];
    /** 可选负责人（仅管理员有用户列表，其余角色传空数组）。 */
    users?: User[];
  }>(),
  { asset: null, users: () => [] },
);

const emit = defineEmits<{
  /** 关闭弹窗。 */
  (event: "close"): void;
  /** 保存成功（参数为提示文案）。 */
  (event: "saved", message: string): void;
}>();

const ASSET_TYPES: AssetType[] = ["host", "db", "middleware", "app", "network"];
const ASSET_STATUSES: AssetStatus[] = ["active", "maintenance", "decommissioned"];

/** 表单状态。 */
const form = reactive({
  businessLineId: null as number | null,
  environmentId: null as number | null,
  name: "",
  assetType: "host" as AssetType,
  identifier: "",
  ownerId: null as number | null,
  ownerContact: "",
  status: "active" as AssetStatus,
  tagsText: "",
  remark: "",
});

const submitting = ref(false);
const errorText = ref("");
const nameInput = ref<HTMLInputElement | null>(null);

const isEdit = computed(() => Boolean(props.asset));
const title = computed(() => (isEdit.value ? `编辑资产 · ${props.asset?.name ?? ""}` : "登记资产"));
/** 管理员才能读写 owner_id（需要用户列表）。 */
const canPickOwner = computed(() => props.users.length > 0);

/** 按业务线过滤后的环境候选。 */
const environmentOptions = computed(() => {
  if (!form.businessLineId) return props.environments;
  return props.environments.filter((env) => env.businessLineId === form.businessLineId);
});

/**
 * 用目标资产（或空表单）重置表单。
 */
function reset(): void {
  const asset = props.asset;
  errorText.value = "";
  if (asset) {
    form.businessLineId = asset.businessLineId;
    form.environmentId = asset.environmentId;
    form.name = asset.name;
    form.assetType = (asset.assetType as AssetType) ?? "host";
    form.identifier = asset.identifier;
    form.ownerId = matchOwnerId(asset.ownerName);
    form.ownerContact = asset.ownerContact;
    form.status = (asset.status as AssetStatus) ?? "active";
    form.tagsText = (asset.tags ?? []).map((tag) => String(tag)).join(", ");
    form.remark = asset.remark;
  } else {
    const firstEnv = props.environments[0] ?? null;
    form.businessLineId = firstEnv?.businessLineId ?? props.businessLines[0]?.id ?? null;
    form.environmentId = firstEnv?.id ?? null;
    form.name = "";
    form.assetType = "host";
    form.identifier = "";
    form.ownerId = null;
    form.ownerContact = "";
    form.status = "active";
    form.tagsText = "";
    form.remark = "";
  }
}

/**
 * 依据出参里的负责人名反查用户 ID（出参无 ownerId，只能按名匹配）。
 *
 * @param ownerName 后端展开的负责人名（display_name 或 username）。
 * @returns 用户 ID；匹配不到为 null。
 */
function matchOwnerId(ownerName: string): number | null {
  if (!ownerName) return null;
  const hit = props.users.find((user) => user.displayName === ownerName || user.username === ownerName);
  return hit ? hit.id : null;
}

/**
 * 业务线变更：把环境切换到该业务线下的第一个可用项。
 *
 * @param event 下拉框 change 事件。
 */
function onBusinessLineChange(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value;
  const value = raw ? Number(raw) : null;
  form.businessLineId = value;
  const options = value ? props.environments.filter((env) => env.businessLineId === value) : props.environments;
  if (!options.some((env) => env.id === form.environmentId)) {
    form.environmentId = options[0]?.id ?? null;
  }
}

/**
 * 解析标签输入（逗号/顿号/空格分隔）。
 *
 * @param text 原始文本。
 * @returns 去重后的标签数组。
 */
function parseTags(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split(/[,，、\s]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ).slice(0, 20);
}

/** 提交表单。 */
async function submit(): Promise<void> {
  if (submitting.value) return;
  errorText.value = "";
  if (!form.name.trim()) {
    errorText.value = "请填写资产名称";
    return;
  }
  if (!form.environmentId) {
    errorText.value = "请选择所属环境";
    return;
  }
  if (!form.identifier.trim()) {
    errorText.value = "请填写资产标识（IP / 域名 / 实例名）";
    return;
  }

  const payload: AssetPayload = {
    environmentId: form.environmentId,
    name: form.name.trim(),
    assetType: form.assetType,
    identifier: form.identifier.trim(),
    ownerId: form.ownerId,
    ownerContact: form.ownerContact.trim(),
    status: form.status,
    tags: parseTags(form.tagsText),
    remark: form.remark.trim(),
  };

  submitting.value = true;
  try {
    if (props.asset) {
      await updateAsset(props.asset.id, payload);
      emit("saved", `已更新资产「${payload.name}」`);
    } else {
      await createAsset(payload);
      emit("saved", `已登记资产「${payload.name}」`);
    }
  } catch (error) {
    errorText.value = error instanceof ApiError ? error.message : "保存失败，请稍后重试";
  } finally {
    submitting.value = false;
  }
}

/**
 * Esc 关闭。
 *
 * @param event 键盘事件。
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") emit("close");
}

watch(
  () => props.open,
  async (open) => {
    if (!open) return;
    reset();
    await nextTick();
    nameInput.value?.focus();
  },
);

watch(() => props.asset, reset);
</script>

<template>
  <div
    v-if="open"
    class="modal-mask"
    role="dialog"
    aria-modal="true"
    aria-labelledby="asset-form-title"
    tabindex="-1"
    @click.self="emit('close')"
    @keydown="onKeydown"
  >
    <div class="modal modal--wide">
      <div class="modal-head">
        <div>
          <h3 id="asset-form-title">{{ title }}</h3>
          <div class="modal-sub">环境 + 类型 + 标识 三元组唯一</div>
        </div>
        <button class="modal-close" type="button" aria-label="关闭" @click="emit('close')">
          <AppIcon name="close" :size="14" />
        </button>
      </div>

      <form class="modal-body" novalidate @submit.prevent="submit">
        <div class="frm-grid">
          <div class="field">
            <label for="af-bl">业务线</label>
            <select
              id="af-bl"
              class="ctl"
              :value="form.businessLineId ?? ''"
              :disabled="submitting"
              @change="onBusinessLineChange"
            >
              <option value="">全部业务线</option>
              <option v-for="line in businessLines" :key="line.id" :value="line.id">{{ line.name }}</option>
            </select>
          </div>

          <div class="field">
            <label for="af-env">所属环境<span class="req">*</span></label>
            <select id="af-env" v-model.number="form.environmentId" class="ctl" :disabled="submitting">
              <option :value="null" disabled>请选择环境</option>
              <option v-for="env in environmentOptions" :key="env.id" :value="env.id">{{ env.name }}</option>
            </select>
            <div v-if="!environmentOptions.length" class="ctl-err">
              暂无可选环境，请先由管理员在「业务线 / 环境管理」中创建。
            </div>
          </div>

          <div class="field">
            <label for="af-name">资产名称<span class="req">*</span></label>
            <input
              id="af-name"
              ref="nameInput"
              v-model="form.name"
              class="ctl"
              type="text"
              maxlength="128"
              placeholder="如：订单库主实例"
              :disabled="submitting"
            />
          </div>

          <div class="field">
            <label for="af-type">资产类型<span class="req">*</span></label>
            <select id="af-type" v-model="form.assetType" class="ctl" :disabled="submitting">
              <option v-for="type in ASSET_TYPES" :key="type" :value="type">
                {{ ASSET_TYPE_LABELS[type] ?? type }}
              </option>
            </select>
          </div>

          <div class="field">
            <label for="af-ident">资产标识<span class="req">*</span></label>
            <input
              id="af-ident"
              v-model="form.identifier"
              class="ctl"
              type="text"
              maxlength="128"
              placeholder="IP / 域名 / 实例名"
              :disabled="submitting"
            />
          </div>

          <div class="field">
            <label for="af-status">状态</label>
            <select id="af-status" v-model="form.status" class="ctl" :disabled="submitting">
              <option v-for="status in ASSET_STATUSES" :key="status" :value="status">
                {{ ASSET_STATUS_LABELS[status] ?? status }}
              </option>
            </select>
          </div>

          <div v-if="canPickOwner" class="field">
            <label for="af-owner">负责人</label>
            <select id="af-owner" v-model.number="form.ownerId" class="ctl" :disabled="submitting">
              <option :value="null">未指定</option>
              <option v-for="user in users" :key="user.id" :value="user.id">
                {{ user.displayName || user.username }}
              </option>
            </select>
          </div>

          <div class="field">
            <label for="af-contact">负责人联系方式</label>
            <input
              id="af-contact"
              v-model="form.ownerContact"
              class="ctl"
              type="text"
              maxlength="128"
              placeholder="电话 / 邮箱 / 值班群"
              :disabled="submitting"
            />
          </div>

          <div class="field full">
            <label for="af-tags">标签</label>
            <input
              id="af-tags"
              v-model="form.tagsText"
              class="ctl"
              type="text"
              placeholder="用逗号分隔，如：核心, 双活, 需巡检"
              :disabled="submitting"
            />
          </div>

          <div class="field full">
            <label for="af-remark">备注</label>
            <textarea
              id="af-remark"
              v-model="form.remark"
              class="ctl"
              rows="3"
              maxlength="500"
              placeholder="用途、归属、变更窗口等"
              :disabled="submitting"
            ></textarea>
          </div>
        </div>

        <p v-if="errorText" class="ctl-err" role="alert" style="margin-top: var(--sp-3)">{{ errorText }}</p>
        <p v-if="isEdit && !canPickOwner" class="ctl-hint" style="margin-top: var(--sp-3)">
          非管理员保存时「负责人」字段会被置空（后端未返回负责人 ID，前端无法回填）。
        </p>
      </form>

      <div class="modal-foot">
        <button class="btn btn-mo" type="button" :disabled="submitting" @click="emit('close')">取消</button>
        <button class="btn btn-zhu" type="button" :disabled="submitting" @click="submit">
          <span v-if="submitting" class="spin" aria-hidden="true"></span>
          {{ isEdit ? "保存变更" : "登记资产" }}
        </button>
      </div>
    </div>
  </div>
</template>
