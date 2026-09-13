<script setup lang="ts">
/**
 * Phase 2 模块占位页。
 *
 * 内容全部来自路由 meta.placeholder（写在路由表里，改文案不必改组件）；
 * 明确标注「未实现」，不伪造任何指标或数据。
 */
import { computed } from "vue";
import { useRoute } from "vue-router";

import AppIcon from "@/components/AppIcon.vue";

const route = useRoute();

/** 占位配置（路由表中定义）。 */
const config = computed(
  () => route.meta.placeholder ?? { module: "未命名模块", phase: "Phase 2", items: [] as string[] },
);
</script>

<template>
  <section class="page">
    <div class="page-head">
      <div class="ph-left">
        <div class="crumb">FishCloud / {{ config.module }}</div>
        <h2>{{ config.module }}</h2>
        <p>该模块尚未实现，规划在 {{ config.phase }} 交付。</p>
      </div>
      <div class="ph-actions">
        <RouterLink to="/">
          <button class="btn btn-mo" type="button">返回运行概览</button>
        </RouterLink>
      </div>
    </div>

    <div class="card card--lift">
      <div class="placeholder">
        <div class="ph-face"><AppIcon name="tasks" :size="22" /></div>
        <h3>{{ config.module }} · {{ config.phase }}</h3>
        <p class="pl-sub">
          当前版本（Phase 1）已交付：登录与 RBAC、资产登记、事件中心、AI 研判、系统管理。<br />
          本页在 Phase 2 接入真实数据与操作能力前保持空白，不展示演示数据。
        </p>
        <ul v-if="config.items.length">
          <li v-for="item in config.items" :key="item">{{ item }}</li>
        </ul>
      </div>
    </div>
  </section>
</template>
