<script setup lang="ts">
/**
 * 品牌配色切换器（紧凑色块按钮组）。
 *
 * 出现在侧栏底部与「设置 → 外观」；写入走 composables/useBrand（单一状态源）。
 * 无障碍：role="group" + aria-pressed 标注当前项，按钮带 title/aria-label。
 */
import { BRAND_OPTIONS } from "@/api/appSettings";
import { useBrand } from "@/composables/useBrand";

withDefaults(defineProps<{ label?: string }>(), { label: "配色" });

const { brand, setBrand } = useBrand();
</script>

<template>
  <div class="brand-switch" role="group" :aria-label="`品牌${label}`">
    <span class="bs-label">{{ label }}</span>
    <button
      v-for="item in BRAND_OPTIONS"
      :key="item.value"
      type="button"
      :aria-pressed="brand === item.value"
      :title="`${item.label}配色`"
      :aria-label="`${item.label}配色`"
      @click="setBrand(item.value)"
    >
      <i :style="{ backgroundImage: item.swatch }" aria-hidden="true"></i>
    </button>
  </div>
</template>
