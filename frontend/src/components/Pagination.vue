<script setup lang="ts">
/**
 * 通用分页控件。
 *
 * 纯展示 + 事件，不持有数据；页码窗口最多 5 个按钮，首尾始终可达。
 * 无障碍：nav 语义 + aria-current 标记当前页，禁用态用 disabled 属性。
 */
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    /** 当前页（1 起）。 */
    page: number;
    /** 每页条数。 */
    pageSize: number;
    /** 总条数。 */
    total: number;
    /** 可选的每页条数候选；传空数组则不显示该下拉。 */
    sizeOptions?: number[];
    /** 是否禁用交互（加载中）。 */
    disabled?: boolean;
  }>(),
  { sizeOptions: () => [10, 20, 50], disabled: false },
);

const emit = defineEmits<{
  (event: "update:page", value: number): void;
  (event: "update:pageSize", value: number): void;
}>();

/** 总页数（至少 1，避免空列表显示 0/0）。 */
const pageCount = computed(() => Math.max(1, Math.ceil(props.total / Math.max(1, props.pageSize))));

/** 页码窗口：当前页居中，最多 5 个。 */
const pages = computed<number[]>(() => {
  const count = pageCount.value;
  const window = 5;
  let start = Math.max(1, props.page - Math.floor(window / 2));
  const end = Math.min(count, start + window - 1);
  start = Math.max(1, end - window + 1);
  const list: number[] = [];
  for (let index = start; index <= end; index += 1) list.push(index);
  return list;
});

/**
 * 跳页（越界忽略）。
 *
 * @param target 目标页码。
 */
function go(target: number): void {
  if (props.disabled || target === props.page) return;
  if (target < 1 || target > pageCount.value) return;
  emit("update:page", target);
}

/**
 * 切换每页条数：回到第 1 页，避免落在越界页。
 *
 * @param event 下拉 change 事件。
 */
function changeSize(event: Event): void {
  const value = Number((event.target as HTMLSelectElement).value);
  if (!Number.isFinite(value) || value <= 0) return;
  emit("update:pageSize", value);
  emit("update:page", 1);
}
</script>

<template>
  <nav class="pager" aria-label="分页">
    <span class="pg-info">共 {{ total }} 条 · 第 {{ page }} / {{ pageCount }} 页</span>

    <span v-if="sizeOptions.length" class="pg-info">
      每页
      <select
        class="ctl"
        style="height: 24px; min-width: 62px; margin-left: 4px"
        :value="pageSize"
        :disabled="disabled"
        aria-label="每页条数"
        @change="changeSize"
      >
        <option v-for="size in sizeOptions" :key="size" :value="size">{{ size }}</option>
      </select>
    </span>

    <span class="pg-ctrl">
      <button class="pg-btn" type="button" :disabled="disabled || page <= 1" aria-label="上一页" @click="go(page - 1)">
        ‹
      </button>
      <button
        v-for="item in pages"
        :key="item"
        class="pg-btn"
        type="button"
        :disabled="disabled"
        :aria-current="item === page ? 'page' : undefined"
        @click="go(item)"
      >
        {{ item }}
      </button>
      <button
        class="pg-btn"
        type="button"
        :disabled="disabled || page >= pageCount"
        aria-label="下一页"
        @click="go(page + 1)"
      >
        ›
      </button>
    </span>
  </nav>
</template>
