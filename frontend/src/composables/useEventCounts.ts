/**
 * 事件计数（KPI 与状态 Tab 徽标共用）。
 *
 * 为什么单独抽出来：
 * - 后端暂无聚合接口，计数只能靠 `page_size=1` 的 total 拼；一次「全套」是 6 个请求，
 *   而切 Tab 时若连带重算就纯属浪费。这里按「计数相关过滤条件」缓存 20s，
 *   切 Tab（仅 status 变化）不再触发计数请求；写操作后调用 invalidate 立即失效。
 * - 请求失败时保留上一次可用值，并把 ready 置 false（界面显示「—」而不是 0，
 *   不把「没取到」伪装成「没有事件」）。
 *
 * 状态放模块级：同一时刻只有事件页在用，不需要每个组件各持一份。
 */
import { ref, type Ref } from "vue";

import { listEvents } from "@/api/events";

/** 计数结果（键与状态机状态同名，便于按 Tab 取值）。 */
export interface EventCounts {
  all: number;
  open: number;
  acknowledged: number;
  diagnosing: number;
  resolved: number;
  closed: number;
}

/** 影响计数的过滤条件（注意：不含 status —— 状态是 Tab 本身）。 */
export interface CountFilter {
  businessLineId: number | null;
  assetId: number | null;
  severity: string;
  keyword: string;
}

const EMPTY: EventCounts = { all: 0, open: 0, acknowledged: 0, diagnosing: 0, resolved: 0, closed: 0 };

/** 缓存 TTL（毫秒）。 */
const TTL = 20_000;

/** 缓存表：key = 规范化后的过滤条件。 */
const cache = new Map<string, { at: number; data: EventCounts }>();

const counts = ref<EventCounts>({ ...EMPTY });
const ready = ref(false);
const loading = ref(false);

/**
 * 规范化缓存键。
 *
 * @param filter 过滤条件。
 * @returns 稳定字符串键。
 */
function keyOf(filter: CountFilter): string {
  return JSON.stringify([filter.businessLineId, filter.assetId, filter.severity, filter.keyword]);
}

/** 让全部计数缓存立即失效（写操作后调用）。 */
export function invalidateEventCounts(): void {
  cache.clear();
}

/**
 * 加载计数（命中缓存则直接返回）。
 *
 * @param filter 过滤条件。
 * @param force 为 true 时忽略缓存。
 */
export async function loadEventCounts(filter: CountFilter, force = false): Promise<void> {
  const key = keyOf(filter);
  const hit = cache.get(key);
  if (!force && hit && Date.now() - hit.at < TTL) {
    counts.value = hit.data;
    ready.value = true;
    return;
  }
  loading.value = true;
  try {
    const base = {
      businessLineId: filter.businessLineId,
      assetId: filter.assetId,
      severity: filter.severity || undefined,
      keyword: filter.keyword || undefined,
    };
    const [all, open, acknowledged, diagnosing, resolved, closed] = await Promise.all([
      listEvents({ ...base, page: 1, pageSize: 1 }),
      listEvents({ ...base, page: 1, pageSize: 1, status: "open" }),
      listEvents({ ...base, page: 1, pageSize: 1, status: "acknowledged" }),
      listEvents({ ...base, page: 1, pageSize: 1, status: "diagnosing" }),
      listEvents({ ...base, page: 1, pageSize: 1, status: "resolved" }),
      listEvents({ ...base, page: 1, pageSize: 1, status: "closed" }),
    ]);
    const data: EventCounts = {
      all: all.total,
      open: open.total,
      acknowledged: acknowledged.total,
      diagnosing: diagnosing.total,
      resolved: resolved.total,
      closed: closed.total,
    };
    cache.set(key, { at: Date.now(), data });
    counts.value = data;
    ready.value = true;
  } catch {
    // 保留旧值，仅标记不可信
    ready.value = false;
  } finally {
    loading.value = false;
  }
}

/**
 * 组合式取用。
 *
 * @returns 计数状态与加载/失效函数。
 */
export function useEventCounts(): {
  counts: Ref<EventCounts>;
  ready: Ref<boolean>;
  loading: Ref<boolean>;
  load: (filter: CountFilter, force?: boolean) => Promise<void>;
  invalidate: () => void;
} {
  return { counts, ready, loading, load: loadEventCounts, invalidate: invalidateEventCounts };
}
