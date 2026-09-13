<script setup lang="ts">
/**
 * 应用侧栏（深色 brand 面板）：品牌 + 菜单分组 + 身份 / 配色。
 *
 * 对齐视觉 PRD（docs/fishcloud-design.md §三）与 wireframe S01 的 Q1 裁决：
 * 一级导航与二级页面合并为一列分组菜单，全部来自 navigation/nav.ts
 * （单一数据源，避免侧栏与路由表各写一份而走偏）。
 *
 * 权限：菜单按 authStore.role 过滤 —— 非 admin 看不到「系统管理」条目，条目数量
 * 由 nav.ts 的 roles 决定；详情页（资产 / 事件）不在 nav.ts 里，天然不进菜单。
 *
 * 管理身份：admin 且当前处于「系统管理」分组时，身份区出现「管理员」徽章
 * （渐变底见 layout.css 的 .sn-badge），系统管理组的标题与激活条也换成管理渐变；
 * 业务模块一律用各自模块色，差异靠对比而非全场渲染。
 *
 * 无障碍：
 * - 菜单是 nav 地标，分组用 h3 标题，读屏可按键跳转；
 * - 当前页用 aria-current="page"（与 .sn-item 的高亮互为印证）；
 * - 收起 / 窄屏时文案只做视觉裁剪，链接的可访问名不会退化成「一个图标」。
 */
import { computed } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import AppIcon from "@/components/AppIcon.vue";
import BrandSwitch from "@/components/BrandSwitch.vue";
import { useShell } from "@/composables/useShell";
import { ROLE_LABELS, labelOf } from "@/lib/labels";
import { isItemActive, visibleGroups } from "@/navigation/nav";
import { useAuthStore } from "@/stores/auth";

const props = defineProps<{
  /** 当前模块 key（由外壳按路由推导，用于管理身份徽章的显隐）。 */
  module: string;
}>();

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const { collapsed, toggleNav } = useShell();

/** 可见菜单分组（角色过滤后，顺序即展示顺序）。 */
const groups = computed(() => visibleGroups(auth.role));
/** 身份行角色文案（缺失时回退角色码）。 */
const roleLabel = computed(() => labelOf(ROLE_LABELS, auth.role));
/** 是否展示「管理员」徽章：admin 且正在系统管理分组内。 */
const showAdminBadge = computed(() => auth.isAdmin && props.module === "system");

/** 登出并回登录页。 */
async function logout(): Promise<void> {
  auth.logout();
  await router.replace({ name: "login" });
}
</script>

<template>
  <aside id="app-sidenav" class="sidenav" :data-collapsed="collapsed ? 'true' : 'false'">
    <!-- 品牌面板：鱼印章 + 名称 -->
    <div class="brand">
      <div class="brand-yin" aria-hidden="true">鱼</div>
      <div class="brand-text">
        <h1>FishCloud</h1>
        <p>Agentic RAG 智能运维</p>
      </div>
    </div>

    <!-- 菜单分组 -->
    <nav class="sn-body" aria-label="主导航">
      <section v-for="group in groups" :key="group.key" class="sn-group" :data-group="group.key">
        <h3 class="sn-group-title">
          <span class="sn-group-txt">{{ group.title }}</span>
          <span v-if="group.tag" class="sn-tag" :title="`${group.tag} 规划中`">{{ group.tag }}</span>
        </h3>
        <div class="sn-list">
          <RouterLink
            v-for="item in group.items"
            :key="item.to"
            class="sn-item"
            :to="item.to"
            :aria-current="isItemActive(item.to, route.path) ? 'page' : undefined"
          >
            <AppIcon :name="item.icon" :size="14" class="sn-ico" />
            <span class="sn-txt">{{ item.title }}</span>
            <span v-if="item.tag" class="sn-tag" :title="`${item.tag} 规划中`">{{ item.tag }}</span>
          </RouterLink>
        </div>
      </section>
    </nav>

    <!-- 底部：身份 + 设置/退出 + 配色切换 + 收起开关 -->
    <div class="sn-foot">
      <div class="sn-user">
        <div class="un">
          <span class="un-name">
            <b :title="auth.displayName">{{ auth.displayName || "未登录" }}</b>
            <span v-if="showAdminBadge" class="sn-badge">管理员</span>
          </span>
          <span>{{ roleLabel }}</span>
        </div>
        <RouterLink to="/settings" class="sn-ibtn" title="设置" aria-label="设置">
          <AppIcon name="settings" :size="14" />
        </RouterLink>
        <button class="sn-ibtn" type="button" title="退出登录" aria-label="退出登录" @click="logout">
          <AppIcon name="logout" :size="14" />
        </button>
      </div>

      <p v-if="!auth.isOperator" class="sn-note">只读角色：写操作按钮已隐藏</p>

      <div class="sn-foot-bar">
        <BrandSwitch label="配色" />
        <button
          class="sn-toggle"
          type="button"
          :aria-expanded="!collapsed"
          aria-controls="app-sidenav"
          :title="collapsed ? '展开侧栏' : '收起侧栏'"
          :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
          @click="toggleNav"
        >
          <AppIcon name="panel" :size="14" />
        </button>
      </div>
    </div>
  </aside>
</template>
