/**
 * 业务上下文过滤 store：跨页持久的「当前业务线 / 当前环境」。
 *
 * 资产列表、事件列表共用同一上下文，切换后其它页面保持一致（localStorage `fc.filter`）。
 */
import { ref, watch } from "vue";
import { defineStore } from "pinia";

/** 持久化键。 */
const STORAGE_KEY = "fc.filter";

interface FilterState {
  businessLineId: number | null;
  environmentId: number | null;
}

/**
 * 读取本地过滤上下文。
 *
 * @returns 过滤状态；无缓存时为全 null。
 */
function loadFilter(): FilterState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { businessLineId: null, environmentId: null };
    const parsed = JSON.parse(raw) as Partial<FilterState>;
    return {
      businessLineId: typeof parsed.businessLineId === "number" ? parsed.businessLineId : null,
      environmentId: typeof parsed.environmentId === "number" ? parsed.environmentId : null,
    };
  } catch {
    return { businessLineId: null, environmentId: null };
  }
}

export const useFilterStore = defineStore("filter", () => {
  const initial = loadFilter();
  const businessLineId = ref<number | null>(initial.businessLineId);
  const environmentId = ref<number | null>(initial.environmentId);

  watch([businessLineId, environmentId], () => {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ businessLineId: businessLineId.value, environmentId: environmentId.value }),
      );
    } catch {
      /* 隐私模式：忽略 */
    }
  });

  /**
   * 设置业务线；业务线变化时清空环境（环境从属于业务线）。
   *
   * @param id 业务线 ID；null 表示全部。
   */
  function setBusinessLine(id: number | null): void {
    businessLineId.value = id;
    environmentId.value = null;
  }

  /**
   * 设置环境。
   *
   * @param id 环境 ID；null 表示全部。
   */
  function setEnvironment(id: number | null): void {
    environmentId.value = id;
  }

  /** 清空全部过滤条件。 */
  function reset(): void {
    businessLineId.value = null;
    environmentId.value = null;
  }

  return { businessLineId, environmentId, setBusinessLine, setEnvironment, reset };
});
