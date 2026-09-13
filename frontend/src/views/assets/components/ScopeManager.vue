<script setup lang="ts">
/**
 * 业务线 / 环境维护弹窗（仅管理员）。
 *
 * 后端约束：
 * - 业务线 code 与 name 唯一（409）；
 * - 同一业务线内环境名唯一（409）；
 * - 存在下级环境/资产时禁止删除（409，detail 已说明数量）。
 *
 * 三态：加载中 / 失败（原地重试）/ 成功。删除走二次确认。
 */
import { onMounted, ref, watch } from "vue";

import {
  createBusinessLine,
  createEnvironment,
  deleteBusinessLine,
  deleteEnvironment,
  listBusinessLines,
  listEnvironments,
} from "@/api/assets";
import { ApiError } from "@/api/http";
import type { BusinessLine, Environment } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";
import { pushToast } from "@/composables/useToast";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{
  (event: "close"): void;
  /** 业务线/环境发生变更（父组件据此刷新下拉与列表）。 */
  (event: "changed"): void;
}>();

const lines = ref<BusinessLine[]>([]);
const environments = ref<Environment[]>([]);
const loading = ref(false);
const errorText = ref("");

/** 新建业务线表单。 */
const lineForm = ref({ code: "", name: "", description: "" });
const linePending = ref(false);
const lineError = ref("");

/** 新建环境表单。 */
const envForm = ref({ businessLineId: null as number | null, name: "", description: "" });
const envPending = ref(false);
const envError = ref("");

/** 待删除目标（两类共用一套确认框）。 */
const removing = ref<{ kind: "line" | "env"; id: number; name: string; hint: string } | null>(null);
const removePending = ref(false);
const removeError = ref("");

/** 加载业务线与环境。 */
async function load(): Promise<void> {
  loading.value = true;
  errorText.value = "";
  try {
    const [lineRows, envRows] = await Promise.all([listBusinessLines(), listEnvironments()]);
    lines.value = lineRows;
    environments.value = envRows;
    if (!envForm.value.businessLineId && lineRows.length) {
      envForm.value.businessLineId = lineRows[0]?.id ?? null;
    }
  } catch (error) {
    errorText.value = error instanceof ApiError ? error.message : "业务线/环境加载失败";
  } finally {
    loading.value = false;
  }
}

/** 新建业务线。 */
async function submitLine(): Promise<void> {
  if (linePending.value) return;
  lineError.value = "";
  if (!lineForm.value.code.trim() || !lineForm.value.name.trim()) {
    lineError.value = "业务编码与业务名称均为必填";
    return;
  }
  linePending.value = true;
  try {
    await createBusinessLine({
      code: lineForm.value.code.trim(),
      name: lineForm.value.name.trim(),
      ownerId: null,
      description: lineForm.value.description.trim(),
    });
    pushToast(`已创建业务线「${lineForm.value.name.trim()}」`, "success");
    lineForm.value = { code: "", name: "", description: "" };
    await load();
    emit("changed");
  } catch (error) {
    lineError.value = error instanceof ApiError ? error.message : "创建失败";
  } finally {
    linePending.value = false;
  }
}

/** 新建环境。 */
async function submitEnv(): Promise<void> {
  if (envPending.value) return;
  envError.value = "";
  if (!envForm.value.businessLineId) {
    envError.value = "请选择所属业务线";
    return;
  }
  if (!envForm.value.name.trim()) {
    envError.value = "环境名称为必填";
    return;
  }
  envPending.value = true;
  try {
    await createEnvironment({
      businessLineId: envForm.value.businessLineId,
      name: envForm.value.name.trim(),
      description: envForm.value.description.trim(),
    });
    pushToast(`已创建环境「${envForm.value.name.trim()}」`, "success");
    envForm.value = { businessLineId: envForm.value.businessLineId, name: "", description: "" };
    await load();
    emit("changed");
  } catch (error) {
    envError.value = error instanceof ApiError ? error.message : "创建失败";
  } finally {
    envPending.value = false;
  }
}

/** 提交删除。 */
async function confirmRemove(): Promise<void> {
  const target = removing.value;
  if (!target || removePending.value) return;
  removePending.value = true;
  removeError.value = "";
  try {
    if (target.kind === "line") await deleteBusinessLine(target.id);
    else await deleteEnvironment(target.id);
    pushToast(`已删除「${target.name}」`, "success");
    removing.value = null;
    await load();
    emit("changed");
  } catch (error) {
    removeError.value = error instanceof ApiError ? error.message : "删除失败";
  } finally {
    removePending.value = false;
  }
}

/**
 * Esc 关闭。
 *
 * @param event 键盘事件。
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && !linePending.value && !envPending.value) emit("close");
}

watch(
  () => props.open,
  (open) => {
    if (open) void load();
  },
);

onMounted(() => {
  if (props.open) void load();
});
</script>

<template>
  <div
    v-if="open"
    class="modal-mask"
    role="dialog"
    aria-modal="true"
    aria-labelledby="scope-title"
    @click.self="emit('close')"
    @keydown="onKeydown"
  >
    <div class="modal modal--wide">
      <div class="modal-head">
        <div>
          <h3 id="scope-title">业务线 / 环境管理</h3>
          <div class="modal-sub">资产的上级结构；删除前需先清空下级（含资产的删除会被后端拒绝）</div>
        </div>
        <button class="modal-close" type="button" aria-label="关闭" @click="emit('close')">
          <AppIcon name="close" :size="14" />
        </button>
      </div>

      <div class="modal-body">
        <div v-if="loading" class="loading-row"><span class="spin" aria-hidden="true"></span>加载中…</div>
        <div v-else-if="errorText" class="err-box" role="alert">
          <span style="flex: 1">{{ errorText }}</span>
          <button class="btn btn-sm" type="button" @click="load">重试</button>
        </div>

        <template v-else>
          <!-- 业务线 -->
          <div class="sec-title">业务线（{{ lines.length }}）</div>
          <div class="frm-grid" style="margin-bottom: var(--sp-4)">
            <div class="field">
              <label for="sc-code">业务编码<span class="req">*</span></label>
              <input id="sc-code" v-model="lineForm.code" class="ctl" maxlength="64" placeholder="如 payment" />
            </div>
            <div class="field">
              <label for="sc-name">业务名称<span class="req">*</span></label>
              <input id="sc-name" v-model="lineForm.name" class="ctl" maxlength="128" placeholder="如 支付中心" />
            </div>
            <div class="field full">
              <label for="sc-desc">说明</label>
              <input id="sc-desc" v-model="lineForm.description" class="ctl" maxlength="255" placeholder="可选" />
            </div>
          </div>
          <p v-if="lineError" class="ctl-err" role="alert" style="margin-bottom: var(--sp-3)">{{ lineError }}</p>
          <div style="display: flex; justify-content: flex-end; margin-bottom: var(--sp-4)">
            <button class="btn btn-sm btn-zhu" type="button" :disabled="linePending" @click="submitLine">
              <span v-if="linePending" class="spin" aria-hidden="true"></span>
              新建业务线
            </button>
          </div>

          <div v-if="!lines.length" class="empty" style="padding: 18px">暂无业务线，请先创建。</div>
          <div v-else>
            <div v-for="line in lines" :key="line.id" class="rule-row">
              <span class="rname">{{ line.name }}</span>
              <span class="rcond">{{ line.code }}</span>
              <span class="rval">{{ line.environmentCount }} 环境</span>
              <div class="r-acts">
                <button
                  class="btn btn-sm btn-ghost-danger"
                  type="button"
                  @click="removing = { kind: 'line', id: line.id, name: line.name, hint: '需先删除其下全部环境' }"
                >
                  删除
                </button>
              </div>
            </div>
          </div>

          <!-- 环境 -->
          <div class="sec-title" style="margin-top: var(--sp-6)">环境（{{ environments.length }}）</div>
          <div class="frm-grid" style="margin-bottom: var(--sp-4)">
            <div class="field">
              <label for="sc-bl">所属业务线<span class="req">*</span></label>
              <select id="sc-bl" v-model.number="envForm.businessLineId" class="ctl">
                <option :value="null" disabled>请选择业务线</option>
                <option v-for="line in lines" :key="line.id" :value="line.id">{{ line.name }}</option>
              </select>
            </div>
            <div class="field">
              <label for="sc-env">环境名称<span class="req">*</span></label>
              <input id="sc-env" v-model="envForm.name" class="ctl" maxlength="64" placeholder="如 prod / staging" />
            </div>
            <div class="field full">
              <label for="sc-env-desc">说明</label>
              <input id="sc-env-desc" v-model="envForm.description" class="ctl" maxlength="255" placeholder="可选" />
            </div>
          </div>
          <p v-if="envError" class="ctl-err" role="alert" style="margin-bottom: var(--sp-3)">{{ envError }}</p>
          <div style="display: flex; justify-content: flex-end; margin-bottom: var(--sp-4)">
            <button class="btn btn-sm btn-zhu" type="button" :disabled="envPending || !lines.length" @click="submitEnv">
              <span v-if="envPending" class="spin" aria-hidden="true"></span>
              新建环境
            </button>
          </div>

          <div v-if="!environments.length" class="empty" style="padding: 18px">暂无环境。</div>
          <div v-else>
            <div v-for="env in environments" :key="env.id" class="rule-row">
              <span class="rname">{{ env.name }}</span>
              <span class="rcond">{{
                lines.find((line) => line.id === env.businessLineId)?.name ?? `#${env.businessLineId}`
              }}</span>
              <span class="rval">{{ env.assetCount }} 资产</span>
              <div class="r-acts">
                <button
                  class="btn btn-sm btn-ghost-danger"
                  type="button"
                  @click="removing = { kind: 'env', id: env.id, name: env.name, hint: '需先移除该环境下全部资产' }"
                >
                  删除
                </button>
              </div>
            </div>
          </div>
        </template>
      </div>

      <div class="modal-foot">
        <button class="btn btn-mo" type="button" @click="emit('close')">关闭</button>
      </div>

      <!-- 删除确认 -->
      <div v-if="removing" class="modal-mask" role="dialog" aria-modal="true" aria-labelledby="scope-del-title">
        <div class="modal">
          <div class="modal-head">
            <h3 id="scope-del-title">删除确认</h3>
          </div>
          <div class="modal-body">
            <p style="font-size: 12.5px; line-height: 1.85">
              确认删除「<b>{{ removing.name }}</b>」？{{ removing.hint }}，否则后端会拒绝删除。
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
    </div>
  </div>
</template>
