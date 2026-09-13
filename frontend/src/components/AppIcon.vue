<script setup lang="ts">
// 应用图标系统：自绘 16px 线性图标，统一 1.4px 描边与圆角端点。
// 取代 emoji —— emoji 字形随平台而变、颜色不可控、无法与文字统一灰度，
// 且是界面「AI 生成感」最主要的来源。
//
// 结构：每个图标 = { box?: 视口, shapes: 形状[] }。绝大多数图标在 16 网格内自绘；
// 品牌鱼形沿用设计稿的 24 网格 G 代码（fishcloud-design.md §1b），靠 box 覆盖视口。
type Shape =
  | { t: "path"; d: string }
  | { t: "rect"; x: number; y: number; w: number; h: number; rx?: number }
  | { t: "circle"; cx: number; cy: number; r: number; fill?: boolean };

interface IconDef {
  box?: string;
  shapes: Shape[];
}

const ICONS: Record<string, IconDef> = {
  // 仪表盘：概览
  overview: {
    shapes: [
      { t: "path", d: "M2.5 11.5a5.5 5.5 0 0 1 11 0" },
      { t: "path", d: "M8 11.5 11 7.5" },
      { t: "path", d: "M2.5 11.5h11" },
    ],
  },
  // 两个工位 + 传送线：流水线
  pipeline: {
    shapes: [
      { t: "rect", x: 1.5, y: 6, w: 4, h: 4, rx: 1 },
      { t: "rect", x: 10.5, y: 6, w: 4, h: 4, rx: 1 },
      { t: "path", d: "M5.5 8h5" },
    ],
  },
  // 循环箭头：状态机流转
  agent: {
    shapes: [
      { t: "path", d: "M13.5 8a5.5 5.5 0 1 1-1.6-3.9" },
      { t: "path", d: "M12.2 1.5v3h-3" },
    ],
  },
  // 数据库：知识库
  knowledge: {
    shapes: [
      { t: "path", d: "M8 5.6c3.3 0 6-1 6-2.2S11.3 1.2 8 1.2 2 2.2 2 3.4s2.7 2.2 6 2.2z" },
      { t: "path", d: "M2 3.4v9.2c0 1.2 2.7 2.2 6 2.2s6-1 6-2.2V3.4" },
      { t: "path", d: "M2 8c0 1.2 2.7 2.2 6 2.2s6-1 6-2.2" },
    ],
  },
  // 漏斗：三路召回
  retrieval: { shapes: [{ t: "path", d: "M2.5 3.5h11l-4.3 5.2v4.3l-2.4-1.5V8.7z" }] },
  // 时间轴 + 长短不一的 Span
  trace: {
    shapes: [
      { t: "path", d: "M3.5 2.5v11" },
      { t: "path", d: "M6 5h6.5" },
      { t: "path", d: "M6 8h4.5" },
      { t: "path", d: "M6 11h6.5" },
    ],
  },
  // 三根柱：评分
  eval: {
    shapes: [
      { t: "path", d: "M3.5 13V8.5" },
      { t: "path", d: "M8 13V4" },
      { t: "path", d: "M12.5 13v-3" },
    ],
  },
  // 脉动波形：延迟监控
  monitor: { shapes: [{ t: "path", d: "M1.5 8.5h2.8l1.7-5 2.5 9.5 1.7-4.5h3.8" }] },
  // 对话气泡：人工反馈
  feedback: {
    shapes: [{ t: "path", d: "M13 4.2A2.2 2.2 0 0 0 10.8 2H5.2A2.2 2.2 0 0 0 3 4.2v6.4l3-2.4h4.8A2.2 2.2 0 0 0 13 6z" }],
  },
  // 气泡内两行文字：智能问答
  chat: {
    shapes: [
      { t: "path", d: "M13.5 8.2a2.4 2.4 0 0 1-2.4 2.4H6.6L3 13.4V4.4A2.4 2.4 0 0 1 5.4 2h5.7a2.4 2.4 0 0 1 2.4 2.4z" },
      { t: "path", d: "M6 5.6h4.6" },
      { t: "path", d: "M6 8.2h3" },
    ],
  },
  // 层叠：技术栈
  stack: {
    shapes: [
      { t: "path", d: "M8 2 14 5.2 8 8.4 2 5.2z" },
      { t: "path", d: "M2 8 8 11.2 14 8" },
      { t: "path", d: "M2 10.8 8 14 14 10.8" },
    ],
  },
  // 齿轮：全局设置
  settings: {
    shapes: [
      { t: "path", d: "M8 10.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4z" },
      { t: "path", d: "M12.6 9.4l1.1.6-1.2 2.1-1.2-.5a4.6 4.6 0 0 1-1.1.7l-.2 1.3H7.7l-.2-1.3a4.6 4.6 0 0 1-1.1-.7l-1.2.5-1.2-2.1 1.1-.6a4.7 4.7 0 0 1 0-1.4l-1.1-.6 1.2-2.1 1.2.5a4.6 4.6 0 0 1 1.1-.7l.2-1.3h2.5l.2 1.3a4.6 4.6 0 0 1 1.1.7l1.2-.5 1.2 2.1-1.1.6a4.7 4.7 0 0 1 0 1.4z" },
    ],
  },
  // 铃铛：告警
  alerts: {
    shapes: [
      { t: "path", d: "M8 2.2a4.2 4.2 0 0 1 4.2 4.2v2.9l1.3 2.2H2.5l1.3-2.2V6.4A4.2 4.2 0 0 1 8 2.2z" },
      { t: "path", d: "M6.5 12.6a1.6 1.6 0 0 0 3 0" },
    ],
  },
  // 三角警示：事件中心
  events: {
    shapes: [
      { t: "path", d: "M8 2.8 14 12.6H2z" },
      { t: "path", d: "M8 6.6v3" },
      { t: "circle", cx: 8, cy: 11.2, r: 0.5, fill: true },
    ],
  },
  // 机架：资产登记
  assets: {
    shapes: [
      { t: "rect", x: 2.5, y: 2.4, w: 11, h: 4.2, rx: 1 },
      { t: "rect", x: 2.5, y: 9.4, w: 11, h: 4.2, rx: 1 },
      { t: "circle", cx: 4.9, cy: 4.5, r: 0.5, fill: true },
      { t: "circle", cx: 4.9, cy: 11.5, r: 0.5, fill: true },
    ],
  },
  // 剪贴板 + 勾：任务中心
  tasks: {
    shapes: [
      { t: "rect", x: 3.4, y: 3, w: 9.2, h: 10.6, rx: 1 },
      { t: "path", d: "M6.2 3V1.9h3.6V3" },
      { t: "path", d: "M6.1 8.7l1.6 1.6 3-3.1" },
    ],
  },
  // 吊牌：工单
  tickets: {
    shapes: [
      { t: "path", d: "M7.4 2.2H13a.9.9 0 0 1 .9.9v5.6l-5.6 5.6a1 1 0 0 1-1.4 0L2.6 10.5a1 1 0 0 1 0-1.4z" },
      { t: "circle", cx: 10.8, cy: 5.2, r: 0.85 },
    ],
  },
  // 双人：平台管理
  admin: {
    shapes: [
      { t: "circle", cx: 5.8, cy: 5.6, r: 2.2 },
      { t: "path", d: "M2 13.2c0-2.3 1.7-3.8 3.8-3.8s3.8 1.5 3.8 3.8" },
      { t: "circle", cx: 11.6, cy: 5.2, r: 1.6 },
      { t: "path", d: "M10.9 9.8c1.9-.3 3.6 1 3.6 3.2" },
    ],
  },
  // 单人：当前用户
  user: {
    shapes: [
      { t: "circle", cx: 8, cy: 5.2, r: 2.3 },
      { t: "path", d: "M3.6 13.4c0-2.6 2-4.3 4.4-4.3s4.4 1.7 4.4 4.3" },
    ],
  },
  // 出门箭头：退出登录
  logout: {
    shapes: [
      { t: "path", d: "M6 13.6H3.5a.9.9 0 0 1-.9-.9V3.3a.9.9 0 0 1 .9-.9H6" },
      { t: "path", d: "M10.4 11.2 13.6 8l-3.2-3.2" },
      { t: "path", d: "M13.6 8H6.6" },
    ],
  },
  plus: {
    shapes: [
      { t: "path", d: "M8 3.4v9.2" },
      { t: "path", d: "M3.4 8h9.2" },
    ],
  },
  minus: { shapes: [{ t: "path", d: "M3.4 8h9.2" }] },
  close: {
    shapes: [
      { t: "path", d: "M4.2 4.2l7.6 7.6" },
      { t: "path", d: "M11.8 4.2l-7.6 7.6" },
    ],
  },
  check: { shapes: [{ t: "path", d: "M3.4 8.4l3 3 6.2-6.9" }] },
  search: {
    shapes: [
      { t: "circle", cx: 7.1, cy: 7.1, r: 4.3 },
      { t: "path", d: "M10.3 10.3 13.6 13.6" },
    ],
  },
  refresh: {
    shapes: [
      { t: "path", d: "M13.4 8a5.4 5.4 0 1 1-1.9-4.1" },
      { t: "path", d: "M12.4 1.4v3h-3" },
    ],
  },
  back: {
    shapes: [
      { t: "path", d: "M6.4 3.6 2 8l4.4 4.4" },
      { t: "path", d: "M2 8h11.4" },
    ],
  },
  chevron: { shapes: [{ t: "path", d: "M6.4 4l4 4-4 4" }] },
  // 四角星：AIOps 模块
  ai: {
    shapes: [
      { t: "path", d: "M8 2.2l1.35 3.45L12.8 7l-3.45 1.35L8 11.8 6.65 8.35 3.2 7l3.45-1.35z" },
      { t: "path", d: "M12.4 11.2l.5 1.25 1.25.5-1.25.5-.5 1.25-.5-1.25L10.65 13l1.25-.5z" },
    ],
  },
  // 问号圈：使用指南
  help: {
    shapes: [
      { t: "circle", cx: 8, cy: 8, r: 5.6 },
      { t: "path", d: "M6.3 6.4a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.7-.8 1.2v.35" },
      { t: "circle", cx: 8, cy: 11.6, r: 0.5, fill: true },
    ],
  },
  // 2×2 方格：平台模块总览
  grid: {
    shapes: [
      { t: "rect", x: 2.5, y: 2.5, w: 4.6, h: 4.6, rx: 1 },
      { t: "rect", x: 8.9, y: 2.5, w: 4.6, h: 4.6, rx: 1 },
      { t: "rect", x: 2.5, y: 8.9, w: 4.6, h: 4.6, rx: 1 },
      { t: "rect", x: 8.9, y: 8.9, w: 4.6, h: 4.6, rx: 1 },
    ],
  },
  // 带分隔线的面板：侧栏折叠开关
  panel: {
    shapes: [
      { t: "rect", x: 1.8, y: 3, w: 12.4, h: 10, rx: 1 },
      { t: "path", d: "M6.4 3v10" },
    ],
  },
  lock: {
    shapes: [
      { t: "rect", x: 3.6, y: 7, w: 8.8, h: 6.6, rx: 1 },
      { t: "path", d: "M5.9 7V5.4a2.1 2.1 0 0 1 4.2 0V7" },
    ],
  },
  shield: { shapes: [{ t: "path", d: "M8 1.9 13.4 4v4.2c0 3.2-2.2 5.4-5.4 6.4-3.2-1-5.4-3.2-5.4-6.4V4z" }] },
  // 品牌鱼形（24 网格，线性版，向右游动）
  fish: {
    box: "0 0 24 24",
    shapes: [
      { t: "path", d: "M4.6 12 Q12 5.6 20.2 12 Q12 18.4 4.6 12 Z" },
      { t: "path", d: "M4.6 12 L1.9 8.9 L1.9 15.1 Z" },
      { t: "path", d: "M13.9 9.5 Q15.7 12 13.9 14.5" },
      { t: "circle", cx: 16.7, cy: 11.2, r: 1.05, fill: true },
    ],
  },
};

withDefaults(defineProps<{ name: string; size?: number }>(), { size: 16 });

/**
 * 取图标定义。
 *
 * @param name 图标名。
 * @returns 图标定义；未知名返回空 shapes（渲染为空白，不报错）。
 */
function iconOf(name: string): IconDef {
  return ICONS[name] ?? { shapes: [] };
}
</script>

<template>
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    :viewBox="iconOf(name).box ?? '0 0 16 16'"
    fill="none"
    stroke="currentColor"
    stroke-width="1.4"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-for="(s, i) in iconOf(name).shapes" :key="i">
      <rect v-if="s.t === 'rect'" :x="s.x" :y="s.y" :width="s.w" :height="s.h" :rx="s.rx ?? 0" />
      <circle v-else-if="s.t === 'circle'" :cx="s.cx" :cy="s.cy" :r="s.r" :fill="s.fill ? 'currentColor' : 'none'" :stroke="s.fill ? 'none' : 'currentColor'" />
      <path v-else :d="s.d" />
    </template>
  </svg>
</template>

<style scoped>
.app-icon{display:block;flex-shrink:0}
</style>
