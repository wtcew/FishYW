<script setup lang="ts">
/**
 * 用户新建 / 编辑弹窗（系统管理 · 用户 Tab）。
 *
 * 后端约束：
 * - 新建：username 唯一（409）、password ≥ 8 位、role_code ∈ {admin, operator, viewer}；
 * - 编辑：可改显示名 / 角色 / 启用状态；不能停用自己，也不能停用最后一个管理员（400）。
 */
import { computed, nextTick, reactive, ref, watch } from "vue";

import { createUser, updateUser } from "@/api/admin";
import { ApiError } from "@/api/http";
import type { Role, RoleCode, User } from "@/api/types";
import AppIcon from "@/components/AppIcon.vue";

const props = withDefaults(
  defineProps<{
    /** 是否可见。 */
    open: boolean;
    /** 编辑目标；null 表示新建。 */
    user?: User | null;
    /** 角色候选（来自 /admin/roles）。 */
    roles: Role[];
  }>(),
  { user: null },
);

const emit = defineEmits<{
  (event: "close"): void;
  (event: "saved", message: string): void;
}>();

const form = reactive({
  username: "",
  password: "",
  displayName: "",
  roleCode: "viewer" as RoleCode,
  isActive: true,
});

const submitting = ref(false);
const errorText = ref("");
const usernameInput = ref<HTMLInputElement | null>(null);

const isEdit = computed(() => Boolean(props.user));
const title = computed(() => (isEdit.value ? `编辑用户 · ${props.user?.username ?? ""}` : "新建用户"));

/** 角色候选；接口未就绪时给出兜底三项。 */
const roleOptions = computed<Array<{ code: string; name: string }>>(() =>
  props.roles.length
    ? props.roles
    : [
        { code: "admin", name: "管理员" },
        { code: "operator", name: "运维工程师" },
        { code: "viewer", name: "只读用户" },
      ],
);

/** 用目标用户（或空表单）重置。 */
function reset(): void {
  errorText.value = "";
  if (props.user) {
    form.username = props.user.username;
    form.password = "";
    form.displayName = props.user.displayName;
    form.roleCode = (props.user.roleCode as RoleCode) || "viewer";
    form.isActive = props.user.isActive;
  } else {
    form.username = "";
    form.password = "";
    form.displayName = "";
    form.roleCode = "viewer";
    form.isActive = true;
  }
}

/** 提交。 */
async function submit(): Promise<void> {
  if (submitting.value) return;
  errorText.value = "";
  if (!isEdit.value) {
    if (form.username.trim().length < 2) {
      errorText.value = "用户名至少 2 个字符";
      return;
    }
    if (form.password.length < 8) {
      errorText.value = "密码至少 8 位";
      return;
    }
  }
  submitting.value = true;
  try {
    if (props.user) {
      await updateUser(props.user.id, {
        display_name: form.displayName.trim(),
        role_code: form.roleCode,
        is_active: form.isActive,
      });
      emit("saved", `已更新用户「${props.user.username}」`);
    } else {
      await createUser({
        username: form.username.trim(),
        password: form.password,
        display_name: form.displayName.trim(),
        role_code: form.roleCode,
      });
      emit("saved", `已创建用户「${form.username.trim()}」`);
    }
  } catch (error) {
    errorText.value = error instanceof ApiError ? error.message : "保存失败";
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
    usernameInput.value?.focus();
  },
);

watch(() => props.user, reset);
</script>

<template>
  <div
    v-if="open"
    class="modal-mask"
    role="dialog"
    aria-modal="true"
    aria-labelledby="user-form-title"
    @click.self="emit('close')"
    @keydown="onKeydown"
  >
    <div class="modal">
      <div class="modal-head">
        <div>
          <h3 id="user-form-title">{{ title }}</h3>
          <div class="modal-sub">角色变更即时生效</div>
        </div>
        <button class="modal-close" type="button" aria-label="关闭" @click="emit('close')">
          <AppIcon name="close" :size="14" />
        </button>
      </div>

      <form class="modal-body" novalidate @submit.prevent="submit">
        <div class="frm-grid">
          <div class="field">
            <label for="uf-username">用户名<span v-if="!isEdit" class="req">*</span></label>
            <input
              id="uf-username"
              ref="usernameInput"
              v-model="form.username"
              class="ctl"
              type="text"
              maxlength="64"
              autocomplete="off"
              :disabled="isEdit || submitting"
            />
            <div v-if="isEdit" class="ctl-hint">用户名创建后不可修改</div>
          </div>

          <div class="field">
            <label for="uf-display">显示名</label>
            <input
              id="uf-display"
              v-model="form.displayName"
              class="ctl"
              type="text"
              maxlength="64"
              placeholder="用于审计与负责人展示"
              :disabled="submitting"
            />
          </div>

          <div v-if="!isEdit" class="field">
            <label for="uf-password">初始密码<span class="req">*</span></label>
            <input
              id="uf-password"
              v-model="form.password"
              class="ctl"
              type="password"
              minlength="8"
              maxlength="128"
              autocomplete="new-password"
              placeholder="至少 8 位"
              :disabled="submitting"
            />
          </div>

          <div class="field">
            <label for="uf-role">角色<span class="req">*</span></label>
            <select id="uf-role" v-model="form.roleCode" class="ctl" :disabled="submitting">
              <option v-for="role in roleOptions" :key="role.code" :value="role.code">{{ role.name }}</option>
            </select>
          </div>

          <div v-if="isEdit" class="field">
            <label for="uf-active">账号状态</label>
            <select id="uf-active" v-model="form.isActive" class="ctl" :disabled="submitting">
              <option :value="true">启用</option>
              <option :value="false">停用</option>
            </select>
            <div class="ctl-hint">不能停用自己或最后一个启用中的管理员</div>
          </div>
        </div>

        <p v-if="errorText" class="ctl-err" role="alert" style="margin-top: var(--sp-3)">{{ errorText }}</p>
      </form>

      <div class="modal-foot">
        <button class="btn btn-mo" type="button" :disabled="submitting" @click="emit('close')">取消</button>
        <button class="btn btn-zhu" type="button" :disabled="submitting" @click="submit">
          <span v-if="submitting" class="spin" aria-hidden="true"></span>
          {{ isEdit ? "保存" : "创建" }}
        </button>
      </div>
    </div>
  </div>
</template>
