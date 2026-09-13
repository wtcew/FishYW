/**
 * 认证接口。
 *
 * 契约（src/api/platform/auth.py）：
 * - POST /auth/login          {username, password} → LoginOut（access_token/expires_in/user）
 * - GET  /auth/me             → UserOut（snake_case，见 types.normalizeUser）
 * - POST /auth/change-password {old_password, new_password} → {status}
 * - GET  /auth/registration-status → {allow_registration, auto_active}（公开，免鉴权）
 * - POST /auth/register       {username, password, display_name?, email?} → 201 {id, username, status}
 *   （注册关闭时返回 403 + detail；前端已用 registration-status 预检，正常路径不会走到这里）
 */
import { http } from "./http";
import { normalizeUser, type LoginResult, type User } from "./types";

/** 后端 LoginOut 原始形态。 */
interface RawLoginOut {
  access_token?: string;
  token_type?: string;
  expires_in?: number;
  user?: Parameters<typeof normalizeUser>[0];
}

/** 注册请求（字段名与后端契约一致，snake_case）。 */
export interface RegisterPayload {
  username: string;
  password: string;
  display_name?: string;
  email?: string;
}

/** 注册结果。status 为后端返回的账号状态（如 pending 表示待管理员启用）。 */
export interface RegisterResult {
  id: number;
  username: string;
  status: string;
}

/** 注册开放状态（GET /auth/registration-status，公开端点）。 */
export interface RegistrationStatus {
  /** 是否开放自助注册。 */
  allowRegistration: boolean;
  /** 注册成功是否立即可登录。false = 需管理员启用（status=pending）。 */
  autoActive: boolean;
  /**
   * 后端是否**明确**给出了开放状态。
   *
   * false 表示这次探测没有拿到可信答复（字段缺失 / 响应不是预期 JSON）。
   * 调用方必须 fail-open：只有 confirmed === true 且 allowRegistration === false
   * 才隐藏注册入口，拿不准时一律照常显示。
   */
  confirmed: boolean;
}

/** 探测类请求的超时（毫秒）：比全局 60s 短得多，探测迟迟不回时尽快放行。 */
const REGISTRATION_PROBE_TIMEOUT_MS = 8000;

/**
 * 查询自助注册是否开放（公开端点，无需登录）。
 *
 * 失败语义（fail-open）：网络错误 / 超时 / 5xx 一律抛 {@link ApiError}（silent，
 * 不弹 toast），由调用方按「未知 → 仍显示注册入口」处理；只有 HTTP 200 且
 * 明确带回 `allow_registration: false` 才算「已关闭」。
 * 理由：宁可让用户在提交时收到后端 403 提示，也不能因为探测失败而砍掉功能入口。
 *
 * @returns 注册开放状态（snake_case 已归一为 camelCase）。
 */
export async function getRegistrationStatus(): Promise<RegistrationStatus> {
  const raw = await http.get<{ allow_registration?: unknown; auto_active?: unknown }>(
    "/auth/registration-status",
    { silent: true, timeoutMs: REGISTRATION_PROBE_TIMEOUT_MS },
  );
  const confirmed = typeof raw?.allow_registration === "boolean";
  return {
    // 字段缺失 / 类型不对：按「未知」处理（allowRegistration=true 仅为默认放行值）
    allowRegistration: confirmed ? (raw?.allow_registration as boolean) : true,
    autoActive: raw?.auto_active === true,
    confirmed,
  };
}

/**
 * 账号密码登录。
 *
 * @param username 用户名。
 * @param password 密码。
 * @returns 令牌、有效期与用户档案（已归一为 camelCase）。
 */
export async function login(username: string, password: string): Promise<LoginResult> {
  const raw = await http.post<RawLoginOut>("/auth/login", { username, password });
  return {
    token: raw?.access_token ?? "",
    tokenType: raw?.token_type ?? "bearer",
    expiresIn: raw?.expires_in ?? 0,
    user: normalizeUser(raw?.user),
  };
}

/**
 * 取当前登录用户（同时用于校验登录态）。
 *
 * @returns 用户档案。
 */
export async function fetchMe(): Promise<User> {
  return normalizeUser(await http.get<Parameters<typeof normalizeUser>[0]>("/auth/me"));
}

/**
 * 修改自己的密码。
 *
 * @param oldPassword 旧密码。
 * @param newPassword 新密码（后端要求 ≥8 位）。
 */
export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  await http.post<{ status: string }>("/auth/change-password", {
    old_password: oldPassword,
    new_password: newPassword,
  });
}

/**
 * 自助注册。
 *
 * 注册开关与审批策略由后端决定：
 * - 关闭时返回 403（detail 为提示文案），这里抛 ApiError 由调用方展示；
 * - 成功返回 201 {id, username, status}，status 为 `pending` 时需管理员启用后才能登录。
 *
 * 请求标记 silent：错误提示由登录页在表单内就地展示，避免叠加全局 toast。
 *
 * @param payload 注册信息。
 * @returns 注册结果。
 */
export async function register(payload: RegisterPayload): Promise<RegisterResult> {
  const raw = await http.post<{ id?: number; username?: string; status?: string }>(
    "/auth/register",
    {
      username: payload.username,
      password: payload.password,
      ...(payload.display_name ? { display_name: payload.display_name } : {}),
      ...(payload.email ? { email: payload.email } : {}),
    },
    { silent: true },
  );
  return {
    id: raw?.id ?? 0,
    username: raw?.username ?? payload.username,
    status: raw?.status ?? "created",
  };
}
