/**
 * 登录态 store。
 *
 * state：token（localStorage `fc.token`）/ user（缓存于 `fc.user`，刷新后先渲染再回源校验）。
 * 权限判定只做「按钮显隐」，真正的权限边界始终在后端 RBAC（require_roles）。
 *
 * 关于 401：token 由 api/http.ts 清除，store 的重置通过 main.ts 注册的
 * onUnauthorized 回调触发，避免 http 层反向依赖 Pinia。
 */
import { computed, ref } from "vue";
import { defineStore } from "pinia";

import { fetchMe, login as loginApi } from "@/api/auth";
import { getToken, USER_KEY, setToken } from "@/api/http";
import { ROLE_CAPABILITIES, type Capability, type RoleCode, type User } from "@/api/types";

/**
 * 读取本地缓存的用户档案。
 *
 * @returns 用户对象；无缓存或格式非法时 null。
 */
function loadCachedUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as User;
    return parsed && typeof parsed.id === "number" ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * 写入/清除用户档案缓存。
 *
 * @param user 用户对象；null 表示清除。
 */
function cacheUser(user: User | null): void {
  try {
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
    else localStorage.removeItem(USER_KEY);
  } catch {
    /* 隐私模式：忽略 */
  }
}

export const useAuthStore = defineStore("auth", () => {
  const token = ref<string>(getToken());
  const user = ref<User | null>(loadCachedUser());
  /** 登录/回源请求进行中（登录页 loading 用）。 */
  const pending = ref(false);
  /** 回源校验的并发去重（路由守卫 + App 挂载可能同时触发）。 */
  let inflight: Promise<void> | null = null;

  const isLoggedIn = computed(() => Boolean(token.value));
  const role = computed(() => user.value?.roleCode ?? "");
  const roleName = computed(() => user.value?.roleName || "");
  const displayName = computed(() => user.value?.displayName || user.value?.username || "");
  const isAdmin = computed(() => role.value === "admin");
  /** operator 及以上（可写资产/处置事件）。 */
  const isOperator = computed(() => role.value === "admin" || role.value === "operator");

  /**
   * 判定是否具备某能力。
   *
   * @param capability read / write / admin。
   * @returns 具备返回 true。
   */
  function can(capability: Capability): boolean {
    const code = role.value as RoleCode;
    return (ROLE_CAPABILITIES[code] ?? []).includes(capability);
  }

  /**
   * 登录并落地 token / 用户档案。
   *
   * @param username 用户名。
   * @param password 密码。
   * @returns 登录后的用户档案。
   */
  async function login(username: string, password: string): Promise<User> {
    pending.value = true;
    try {
      const result = await loginApi(username, password);
      token.value = result.token;
      setToken(result.token);
      user.value = result.user;
      cacheUser(result.user);
      return result.user;
    } finally {
      pending.value = false;
    }
  }

  /**
   * 回源拉取当前用户（同时校验 token 是否仍有效）。
   *
   * @param force 为 true 时忽略已有用户缓存强制刷新。
   */
  async function fetchProfile(force = false): Promise<void> {
    if (!token.value) return;
    if (!force && user.value) return;
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const profile = await fetchMe();
        user.value = profile;
        cacheUser(profile);
      } catch {
        // 401 已由 http 层清 token 并跳登录；这里只保证本地态一致。
        reset();
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  /** 清空登录态（内联登出，不发请求 —— JWT 无服务端会话）。 */
  function reset(): void {
    token.value = "";
    user.value = null;
    setToken(null);
    cacheUser(null);
  }

  /** 登出并回到登录页（跳转由调用方处理）。 */
  function logout(): void {
    reset();
  }

  return {
    token,
    user,
    pending,
    isLoggedIn,
    role,
    roleName,
    displayName,
    isAdmin,
    isOperator,
    can,
    login,
    fetchProfile,
    reset,
    logout,
  };
});
