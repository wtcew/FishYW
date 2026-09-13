<script setup lang="ts">
/**
 * 应用骨架：深色侧栏（brand 面板）+ 内容区。
 *
 * 对齐视觉 PRD docs/fishcloud-design.md §三 与 wireframe S01 的 Q1 裁决：
 * 回归「左侧深色侧栏 + 右侧主区」两级结构（此前的「白底顶栏一级导航 +
 * 可折叠二级导航」方案已下线）。侧栏承载品牌、菜单分组、身份与配色切换，
 * 内容区独占剩余宽度；面包屑 / 页头仍由各视图（S05–S28）自绘，外壳不介入。
 *
 * 模块配色：把当前分组 key 写到 `[data-module]` 上（主区与侧栏各一份），
 * 由 styles/layout.css 的渐变规则按模块取色 —— 视图一行不动即可拿到
 * 各自模块的页头条渐变、系统管理组的管理专属渐变。
 *
 * 登录 / 注册页仍走独立布局（route.meta.blank），不渲染骨架。
 */
import { computed, onMounted } from "vue";
import { RouterView, useRoute } from "vue-router";

import AppSidebar from "./components/shell/AppSidebar.vue";
import ToastHost from "./components/ToastHost.vue";
import { applyAppearance, loadAppSettings } from "@/api/appSettings";
import { activeGroup, visibleGroups } from "@/navigation/nav";
import { useAuthStore } from "@/stores/auth";

const route = useRoute();
const auth = useAuthStore();

/** 登录页等独立布局：不渲染骨架。 */
const blankLayout = computed(() => Boolean(route.meta.blank));

/** 当前模块 key（决定页头条与侧栏强调用的渐变，见 layout.css）。 */
const moduleKey = computed(() => activeGroup(route.path, visibleGroups(auth.role)).key);

onMounted(() => {
  // 启动即应用全局外观设置（明暗主题 / 品牌配色 / 字号 / 密度 / 动效）。
  applyAppearance(loadAppSettings());
  // 有 token 时回源刷新用户档案（角色可能已被管理员修改 → 影响菜单与按钮显隐）。
  void auth.fetchProfile(true);
});
</script>

<template>
  <!-- 登录 / 注册：独立布局 -->
  <RouterView v-if="blankLayout" />

  <!-- 主工作台：侧栏 + 内容 -->
  <template v-else>
    <AppSidebar :module="moduleKey" />

    <main class="content" :data-module="moduleKey">
      <div class="content-scroll">
        <RouterView />
      </div>
    </main>
  </template>

  <ToastHost />
</template>
