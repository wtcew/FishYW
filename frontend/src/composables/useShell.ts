/**
 * 应用骨架状态：二级导航折叠与否（持久化到 localStorage `fc.shell`）。
 *
 * 规则：
 * - 用户显式折叠/展开优先；
 * - 从未设置过时，窄屏（≤1280px）默认折叠，宽屏默认展开；
 * - 无论何时都记住用户的选择（刷新/重启后保持）。
 */
import { ref, watch } from "vue";

/** 存储键。 */
const STORAGE_KEY = "fc.shell";

/** 窄屏阈值（与 layout.css 的响应式断点一致）。 */
const NARROW_WIDTH = 1280;

interface ShellState {
  collapsed: boolean;
}

/**
 * 读取初始折叠状态。
 *
 * @returns 折叠状态。
 */
function resolveInitial(): boolean {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ShellState>;
      if (typeof parsed.collapsed === "boolean") return parsed.collapsed;
    }
  } catch {
    /* 隐私模式：走宽度默认值 */
  }
  return typeof window !== "undefined" ? window.innerWidth <= NARROW_WIDTH : false;
}

/** 折叠状态（模块级单例，供壳层与需要感知布局的组件共用）。 */
const collapsed = ref<boolean>(resolveInitial());

watch(collapsed, (value) => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ collapsed: value }));
  } catch {
    /* 忽略写入失败 */
  }
});

/**
 * 组合式取用。
 *
 * @returns 折叠状态与切换函数。
 */
export function useShell(): {
  collapsed: typeof collapsed;
  toggleNav: () => void;
  setCollapsed: (value: boolean) => void;
} {
  return {
    collapsed,
    toggleNav: (): void => {
      collapsed.value = !collapsed.value;
    },
    setCollapsed: (value: boolean): void => {
      collapsed.value = value;
    },
  };
}
