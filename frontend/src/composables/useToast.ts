/**
 * 全局轻提示（toast）。
 *
 * 用模块级 ref 而非 Pinia store：http.ts（底层请求层）与视图层都要能推送提示，
 * 走 Pinia 会在「store 尚未安装」的时序下取不到实例；模块单例没有这个隐患。
 */
import { ref, type Ref } from "vue";

export type ToastKind = "info" | "success" | "error";

export interface ToastItem {
  id: number;
  text: string;
  kind: ToastKind;
}

const items = ref<ToastItem[]>([]);
let seq = 0;

/**
 * 推送一条提示。
 *
 * @param text 提示文本。
 * @param kind 语义类型（决定配色）。
 * @param ttl 展示时长（毫秒）。
 */
export function pushToast(text: string, kind: ToastKind = "info", ttl = 3200): void {
  const id = ++seq;
  items.value = [...items.value, { id, text, kind }];
  window.setTimeout(() => dismissToast(id), ttl);
}

/**
 * 关闭指定提示。
 *
 * @param id 提示 ID。
 */
export function dismissToast(id: number): void {
  items.value = items.value.filter((item) => item.id !== id);
}

/**
 * 取提示列表（供 ToastHost 渲染）。
 *
 * @returns 响应式提示数组。
 */
export function useToasts(): Ref<ToastItem[]> {
  return items;
}
