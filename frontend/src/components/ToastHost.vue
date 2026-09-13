<script setup lang="ts">
/**
 * 全局提示容器（挂载于 App 根，与布局无关）。
 *
 * class 前缀 fc-toast 独立于原型 .toast（避免继承其 fixed/opacity 规则）；
 * 读屏播报走 aria-live，错误类提示用 role="alert" 提高优先级。
 */
import { useToasts } from "@/composables/useToast";

const toasts = useToasts();
</script>

<template>
  <div class="fc-toast-host" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast">
      <div
        v-for="item in toasts"
        :key="item.id"
        class="fc-toast"
        :class="`fc-toast--${item.kind}`"
        :role="item.kind === 'error' ? 'alert' : 'status'"
      >
        {{ item.text }}
      </div>
    </TransitionGroup>
  </div>
</template>
