<script setup lang="ts">
/**
 * 登录 / 注册页（独立布局）。
 *
 * 版式：整页同族 brand 面板渐变（与深色侧栏同一套 --side-*）+ 居中白色表单卡。
 * 与视觉 PRD §三.1 的「左 44% 面板 / 右表单」不同 —— 登录页只有一组字段，
 * 双栏会留下一大片无内容的深色区（等同装饰性假内容）。改为单卡片后：
 * 品牌与新外壳连续、信息密度更高、窄屏无需重排。理由已记入交付报告。
 *
 * 两种模式共用一页：mode=login（默认）与 mode=register（路由 /register 直达）。
 *
 * 注册开放状态在加载时预检（GET /auth/registration-status，公开端点），
 * 语义为 **fail-open**：
 * - 明确拿到 allow_registration=false（HTTP 200）→ 隐藏入口 / 禁用注册表单；
 * - 探测失败（网络错误 / 超时 / 5xx / 非预期 JSON / 端点不存在）→ 视为「未知」，
 *   照常显示注册入口，不弹 toast；真不可注册时后端会在提交时返回 403，
 *   由表单内提示兜底（绝不因为探测失败而砍掉功能入口）。
 *
 * 其余状态处理：
 * - 提交中禁用整个表单并显示进度（防重复提交）；
 * - 失败在原位给出可照做的中文提示（后端 detail 优先，401 两种口径由后端下发）。
 */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { getRegistrationStatus, register } from "@/api/auth";
import { ApiError } from "@/api/http";
import AppIcon from "@/components/AppIcon.vue";
import { pushToast } from "@/composables/useToast";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

type Mode = "login" | "register";

/** 当前模式：/register 直达注册，带 ?mode=register 亦可。 */
const mode = ref<Mode>(route.name === "register" || route.query.mode === "register" ? "register" : "login");

// 登录与注册复用同一组件：仅靠 hash 在 #/login ↔ #/register 间跳转时组件不会
// 重挂载，mode 必须随路由同步，否则地址栏已变而表单仍是旧模式
// （2026-09-13 UI 穷举测试 B-1）。
watch(
  () => [route.name, route.query.mode] as const,
  ([name, queryMode]) => {
    const want: Mode = name === "register" || queryMode === "register" ? "register" : "login";
    if (mode.value !== want) mode.value = want;
  },
);

/** 登录表单。 */
const username = ref("");
const password = ref("");

/** 注册表单。 */
const regUsername = ref("");
const regDisplayName = ref("");
const regEmail = ref("");
const regPassword = ref("");
const regConfirm = ref("");

const errorText = ref("");
const noticeText = ref("");
const submitting = ref(false);
const usernameInput = ref<HTMLInputElement | null>(null);
const regUsernameInput = ref<HTMLInputElement | null>(null);

/**
 * 注册开放状态：checking（探测中，不拦提交）/ open / closed（仅明确关闭时为 closed）。
 */
const regStatus = ref<"checking" | "open" | "closed">("checking");
/** 注册成功后是否立即可登录（auto_active）。 */
const regAutoActive = ref(false);
/** 是否拿到过后端的可信答复：拿到就以该字段为准，没拿到才回落到 status 文案判定。 */
const regConfirmed = ref(false);

/** 注册是否被明确关闭（仅此一种情况禁用表单）。 */
const regBlocked = computed(() => regStatus.value === "closed");

/** 注册按钮文案。 */
const regSubmitLabel = computed(() => (submitting.value ? "提交中…" : "注册"));

/**
 * 预检注册开放状态（fail-open）。
 *
 * 任何拿不到可信答复的情况都当作「未知」：保留入口，交给提交时的后端响应兜底。
 */
async function loadRegistrationStatus(): Promise<void> {
  try {
    const status = await getRegistrationStatus();
    regConfirmed.value = status.confirmed;
    regAutoActive.value = status.autoActive;
    regStatus.value = status.confirmed && !status.allowRegistration ? "closed" : "open";
  } catch {
    regConfirmed.value = false;
    regStatus.value = "open";
  }
}

/** 登录可提交（空值不发请求）。 */
const canSubmitLogin = computed(() => username.value.trim().length > 0 && password.value.length > 0);

/**
 * 注册表单前端校验。
 *
 * @returns 错误文案；通过校验返回空串。
 */
const registerError = computed(() => {
  const name = regUsername.value.trim();
  if (!name) return "";
  if (!/^[A-Za-z0-9._-]{3,32}$/.test(name)) return "用户名只能包含字母、数字、下划线、点与短横线，长度 3-32";
  return "";
});

/** 密码长度提示。 */
const passwordHint = computed(() => {
  if (!regPassword.value) return "";
  if (regPassword.value.length < 8) return "密码至少 8 位";
  return "";
});

/** 两次输入一致性提示。 */
const confirmHint = computed(() => {
  if (!regConfirm.value) return "";
  return regConfirm.value === regPassword.value ? "" : "两次输入的密码不一致";
});

/**
 * 切换模式。
 *
 * @param value 目标模式。
 */
async function switchMode(value: Mode): Promise<void> {
  mode.value = value;
  errorText.value = "";
  noticeText.value = "";
  await nextTick();
  // 关闭态的用户名输入框是禁用的，聚焦无意义（也不会真的拿到焦点）
  if (value === "register") {
    if (!regBlocked.value) regUsernameInput.value?.focus();
  } else {
    usernameInput.value?.focus();
  }
  // 同步路由（可分享/可刷新；登录态守卫不拦公开页）
  const query = { ...route.query };
  delete query.mode;
  void router.replace({ name: value === "register" ? "register" : "login", query });
}

/** 提交登录。 */
async function submitLogin(): Promise<void> {
  if (submitting.value) return;
  if (!canSubmitLogin.value) {
    errorText.value = "请输入用户名与密码";
    return;
  }
  submitting.value = true;
  errorText.value = "";
  noticeText.value = "";
  try {
    const profile = await auth.login(username.value.trim(), password.value);
    pushToast(`欢迎回来，${profile.displayName || profile.username}`, "success");
    const redirect = typeof route.query.redirect === "string" ? route.query.redirect : "";
    await router.replace(redirect || { name: "overview" });
  } catch (error) {
    // 401 两种口径（用户名或密码错误 / 账号已停用）由后端 detail 下发，原样展示
    errorText.value = error instanceof ApiError ? error.message : "登录失败，请稍后重试";
    password.value = "";
    // 先解除禁用再聚焦：submitting 仍为 true 时输入框是 disabled，focus() 不生效
    // （2026-09-13 UI 穷举测试 P2）。
    submitting.value = false;
    await nextTick();
    usernameInput.value?.focus();
  } finally {
    submitting.value = false;
  }
}

/** 提交注册。 */
async function submitRegister(): Promise<void> {
  if (submitting.value || regBlocked.value) return;
  errorText.value = "";
  noticeText.value = "";
  const name = regUsername.value.trim();
  if (!/^[A-Za-z0-9._-]{3,32}$/.test(name)) {
    errorText.value = "用户名只能包含字母、数字、下划线、点与短横线，长度 3-32";
    return;
  }
  if (regPassword.value.length < 8) {
    errorText.value = "密码至少 8 位";
    return;
  }
  if (regPassword.value !== regConfirm.value) {
    errorText.value = "两次输入的密码不一致";
    return;
  }
  if (regEmail.value && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(regEmail.value.trim())) {
    errorText.value = "邮箱格式不正确";
    return;
  }

  submitting.value = true;
  try {
    const result = await register({
      username: name,
      password: regPassword.value,
      display_name: regDisplayName.value.trim() || undefined,
      email: regEmail.value.trim() || undefined,
    });
    const pending = regConfirmed.value
      ? !regAutoActive.value // 预检拿到了答复：以后端 auto_active 为准
      : /pending|review|approv|待审/i.test(result.status); // 未拿到答复：沿用 status 文案判定
    username.value = result.username || name;
    await switchMode("login");
    noticeText.value = pending ? "注册已提交，待管理员启用" : "注册成功，请登录";
    password.value = "";
    pushToast(pending ? "注册已提交，等待管理员启用" : "注册成功，请登录", "success");
  } catch (error) {
    if (error instanceof ApiError) {
      // 403 = 后端关闭了自助注册（预检没探到时的兜底）：给明确的替代路径，不当作异常
      errorText.value =
        error.status === 403
          ? error.message || "注册暂未开放，请联系管理员开通账号"
          : error.message;
    } else {
      errorText.value = "注册失败，请稍后重试";
    }
  } finally {
    submitting.value = false;
  }
}

onMounted(async () => {
  if (mode.value === "login") usernameInput.value?.focus();
  // 注册开放状态预检：登录页与注册页（同一组件两种模式）加载时都请求
  await loadRegistrationStatus();
  // 预检有结论后再聚焦注册表单：关闭态不聚焦禁用控件
  if (mode.value === "register" && !regBlocked.value) regUsernameInput.value?.focus();
});
</script>

<template>
  <div class="login">
    <div class="login-in">
      <!-- 品牌行：与侧栏 .brand 同款（鱼印章 + 名称 + 副标题） -->
      <div class="login-brand">
        <div class="brand-yin" aria-hidden="true">鱼</div>
        <div class="brand-text">
          <h1>FishCloud</h1>
          <p>智能运维平台</p>
        </div>
      </div>

      <!-- 登录 -->
      <form v-if="mode === 'login'" class="login-card" novalidate @submit.prevent="submitLogin">
        <div class="login-head">
          <h2>登录</h2>
          <p>账号由管理员分配</p>
        </div>

        <div class="login-fields">
          <div class="field">
            <label for="login-username">用户名</label>
            <input
              id="login-username"
              ref="usernameInput"
              v-model="username"
              class="ctl ctl-lg"
              type="text"
              name="username"
              autocomplete="username"
              spellcheck="false"
              placeholder="请输入用户名"
              :disabled="submitting"
              :aria-invalid="Boolean(errorText)"
              aria-describedby="login-error"
            />
          </div>

          <div class="field">
            <label for="login-password">密码</label>
            <input
              id="login-password"
              v-model="password"
              class="ctl ctl-lg"
              type="password"
              name="password"
              autocomplete="current-password"
              placeholder="请输入密码"
              :disabled="submitting"
              :aria-invalid="Boolean(errorText)"
              aria-describedby="login-error"
            />
          </div>
        </div>

        <p id="login-error" class="login-error" role="alert">{{ errorText }}</p>
        <p v-if="noticeText" class="login-notice" role="status">
          <AppIcon name="check" :size="12" />{{ noticeText }}
        </p>

        <button class="btn btn-zhu login-submit" type="submit" :disabled="submitting || !canSubmitLogin">
          <span v-if="submitting" class="spin" aria-hidden="true"></span>
          {{ submitting ? "登录中…" : "登 录" }}
        </button>

        <!-- 自助注册未开放（仅明确关闭时）整块入口不渲染：不给走不通的路径 -->
        <div v-if="!regBlocked" class="login-switch">
          <span>还没有账号？</span>
          <button type="button" class="link-btn" :disabled="submitting" @click="switchMode('register')">
            注册新账号
          </button>
        </div>

        <p class="login-tip">忘记密码请联系管理员重置；连续登录失败会记入审计日志。</p>
      </form>

      <!-- 注册 -->
      <form v-else class="login-card" novalidate @submit.prevent="submitRegister">
        <div class="login-head">
          <h2>注册</h2>
          <p>{{ regConfirmed && regAutoActive ? "注册后可直接登录" : "提交后需管理员启用" }}</p>
        </div>

        <!-- 明确关闭时的结论：表单整体禁用，用户不必填完才知道 -->
        <p v-if="regBlocked" class="login-closed" role="status">
          <AppIcon name="lock" :size="12" />注册暂未开放，请联系管理员开通账号
        </p>

        <div class="login-fields">
          <div class="field">
            <label for="reg-username">用户名<span class="req">*</span></label>
            <input
              id="reg-username"
              ref="regUsernameInput"
              v-model="regUsername"
              class="ctl ctl-lg"
              type="text"
              name="username"
              autocomplete="username"
              spellcheck="false"
              maxlength="32"
              placeholder="3-32 位字母 / 数字 / _ . -"
              :disabled="submitting || regBlocked"
              :aria-invalid="Boolean(registerError)"
            />
            <div v-if="registerError" class="ctl-err">{{ registerError }}</div>
          </div>

          <div class="field">
            <label for="reg-display">显示名</label>
            <input
              id="reg-display"
              v-model="regDisplayName"
              class="ctl ctl-lg"
              type="text"
              name="display_name"
              autocomplete="nickname"
              maxlength="64"
              placeholder="可选，用于审计与负责人展示"
              :disabled="submitting || regBlocked"
            />
          </div>

          <div class="field">
            <label for="reg-email">邮箱</label>
            <input
              id="reg-email"
              v-model="regEmail"
              class="ctl ctl-lg"
              type="email"
              name="email"
              autocomplete="email"
              maxlength="128"
              placeholder="可选，用于通知与找回"
              :disabled="submitting || regBlocked"
            />
          </div>

          <div class="field">
            <label for="reg-password">密码<span class="req">*</span></label>
            <input
              id="reg-password"
              v-model="regPassword"
              class="ctl ctl-lg"
              type="password"
              name="password"
              autocomplete="new-password"
              minlength="8"
              maxlength="128"
              placeholder="至少 8 位"
              :disabled="submitting || regBlocked"
              :aria-invalid="Boolean(passwordHint)"
            />
            <div v-if="passwordHint" class="ctl-err">{{ passwordHint }}</div>
          </div>

          <div class="field">
            <label for="reg-confirm">确认密码<span class="req">*</span></label>
            <input
              id="reg-confirm"
              v-model="regConfirm"
              class="ctl ctl-lg"
              type="password"
              name="confirm_password"
              autocomplete="new-password"
              placeholder="再输入一次密码"
              :disabled="submitting || regBlocked"
              :aria-invalid="Boolean(confirmHint)"
            />
            <div v-if="confirmHint" class="ctl-err">{{ confirmHint }}</div>
          </div>
        </div>

        <p id="reg-error" class="login-error" role="alert">{{ errorText }}</p>
        <p v-if="noticeText" class="login-notice" role="status">
          <AppIcon name="check" :size="12" />{{ noticeText }}
        </p>

        <button class="btn btn-zhu login-submit" type="submit" :disabled="submitting || regBlocked">
          <span v-if="submitting" class="spin" aria-hidden="true"></span>
          {{ regSubmitLabel }}
        </button>

        <div class="login-switch">
          <span>已有账号？</span>
          <button type="button" class="link-btn" :disabled="submitting" @click="switchMode('login')">
            返回登录
          </button>
        </div>

        <p class="login-tip">新账号固定为只读角色，权限调整请联系管理员。</p>
      </form>
    </div>
  </div>
</template>
