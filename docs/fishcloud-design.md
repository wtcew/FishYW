# FishCloud 视觉设计交付书（dsq 设计子代理 · 2026-09-12）

> 交付人：dsq（DeepSeek V4.1 Flash 角色化设计子代理）。本文件为实施基线，落地时与
> `frontend/src/styles/tokens.css`、`prototype.css`、`layout.css` 对照执行。
> 现状结论：纯白主区（--zhi-2）+ 深墨侧栏（--shu-1 渐变），朱砂红 `#B03A2E` 主色，
> 黛青/松绿/藤黄辅助，圆角 2–4px、1px 细边框、13px 正文、宋体标题 + 等宽数字。
> 本次设计全部为**扩展**，不改既有 token 定义，仅追加。

## 一、品牌与图标

### 1a. “鱼”字 brand 方块（印章质感）

```html
<div class="brand">
  <div class="brand-yin" aria-hidden="true">鱼</div>
  <div class="brand-text">
    <h1>FishCloud</h1>
    <p>智能运维平台</p>
  </div>
</div>
```

```css
.brand-yin{
  width:32px;height:32px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  line-height:1;
  font-family:var(--f-song);font-size:17px;font-weight:700;
  color:#F7F4EE;
  background-color:var(--zhu-1);
  background-image:linear-gradient(180deg,#C74B3A 0%,#B03A2E 58%,#A53226 100%);
  border:1px solid var(--zhu-3);
  border-radius:var(--r-1);
  box-shadow:inset 0 1px 0 rgba(255,240,220,.22),
             inset 0 -1px 2px rgba(70,15,8,.35),
             0 1px 3px rgba(30,10,5,.5);
  text-shadow:0 1px 1px rgba(70,15,8,.45);
  user-select:none;
}
.brand-text h1{
  font-family:var(--f-song);font-size:15px;font-weight:700;
  color:#F2F2EE;letter-spacing:.8px;line-height:1.2;
}
.brand-text p{
  font-size:10px;color:rgba(255,255,255,.42);margin-top:3px;
  letter-spacing:.6px;font-family:var(--f-hei);
}
```

| 参数 | 值 |
| --- | --- |
| 方块尺寸 | 32 × 32px |
| 字符 | 鱼 · 宋体 17px · 700 |
| 底色 | 渐变 `#C74B3A → #B03A2E → #A53226`，描边 `#8B2A1F` |
| 字色 | `#F7F4EE`（暖白） |

### 1b. 鱼形 SVG（24px 网格，向右游动语义）

**线性版（AppIcon `name="fish"` / 侧栏 logo）**

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24"
     fill="none" stroke="currentColor" stroke-width="1.6"
     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M4.6 12 Q12 5.6 20.2 12 Q12 18.4 4.6 12 Z"/>
  <path d="M4.6 12 L1.9 8.9 L1.9 15.1 Z"/>
  <path d="M13.9 9.5 Q15.7 12 13.9 14.5"/>
  <circle cx="16.7" cy="11.2" r="1.05" fill="currentColor" stroke="none"/>
</svg>
```

**填充版（evenodd 镂空眼，随 currentColor 变色）**

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path fill-rule="evenodd" clip-rule="evenodd"
        d="M4.6 12 Q12 5.6 20.2 12 Q12 18.4 4.6 12 Z
           M17.7 11.2 A1.05 1.05 0 1 1 15.6 11.2 A1.05 1.05 0 1 1 17.7 11.2 Z"/>
  <path d="M5.3 12 L2 8.9 L2 15.1 Z"/>
</svg>
```

**徽章版（favicon，已落 `frontend/public/favicon.svg`）**：圆角方印底 + 暖白鱼 +
渐变 `#C74B3A → #B03A2E → #A53226`。

### 1c. index.html 头部

`<title>FishCloud · 智能运维平台</title>` + `theme-color: #B03A2E` +
`<link rel="icon" type="image/svg+xml" href="/favicon.svg">`（已落地）。

## 二、Design Token 扩展（追加到 tokens.css）

模块色族（每族五档 tint/soft/base/hover/deep）：

| 模块 | 色族 | base |
| --- | --- | --- |
| 资产中心 | 黛青 | `--acc-asset:#2C4A5E` |
| 事件中心 | 藤黄 | `--acc-event:#B8860B` |
| 工单处置 | 松绿 | `--acc-ticket:#3E5C4A` |
| 系统管理 | 玄墨 | `--acc-sys:#5A5A54` |
| 品牌/危险 | 朱砂 | `#B03A2E`（CTA 与 danger 专用，保持稀缺） |

状态色体系（success/warning/danger/info/neutral，tint+soft+base+hover+text 五件套）与
事件严重度映射 `--sev-p1..p4`（danger/warning/info/neutral）见 dsq 原始交付的 CSS
代码块（实施时整段追加进 tokens.css，含 `[data-theme="dark"]` 等值反转、密度档
`[data-density]`、间距 `--sp-7..9`、边框梯度 `--bd-*`、阴影梯度 `--sh-1..4/--sh-pop`）。

## 三、页面布局蓝图（区块级）

1. **登录页** `/login`：左 44% 深墨品牌面板（48px 印章 + FishCloud 宋体 26px + 朱红短线）
   / 右 56% 白色表单区（360px 居中列，输入高 `--ctl-h-lg` 36px）；<860px 单列。
2. **资产列表**：crumb + page-head（计数副标题 + ＋登记资产/导出）→ 筛选栏 44px →
   `.tbl` 表（行高 `--row-h`，首列资产名 600 + mono IP 小字，左缘 3px 健康色条）→ 分页。
   列宽建议：名称 auto/类型 96/环境 72/负责人 96/状态 96/最近变更 120/操作 72。
3. **资产详情**：`grid 380px 1fr`；左 sticky 概览卡（类型徽章/spec 行/标签/元数据）+
   右侧依赖关系卡（32px 关系行）与变更历史时间线（`.timeline` 复用）。
4. **事件列表**：KPI 四卡（88px 高）→ 下划线式状态 Tab（高 40px，激活 2px 朱红下划线 +
   计数徽章）→ 事件表（严重度列 `P1..P4` mono+徽章双编码，左缘 `--sev-*` 色条）。
5. **事件详情**：`grid 420px 1fr`；左信息卡（字段行 32px + AI 摘要引用块
   `st-info-tint` 底/黛青左缘）+ 右处置复盘时间线（复用 `.pipeline/.stage`：
   告警触发→AI 研判→处置动作→恢复确认；P1 未恢复时“关闭”禁用）。
6. **系统管理**：用户/角色/审计三 Tab（激活线 `--acc-sys`）；角色 Tab 双栏
   `260px 1fr` 权限矩阵（勾选格 18px，选中松绿实底白勾）；审计表时间/IP/对象全 mono。

## 四、质感规范要点（完整 CSS 见 dsq 原始交付）

- 卡片层次：`.card--lift/--modal/--toast` + `.card-interactive`（hover 仅提升一档，位移 ≤1px）。
- 表格：行 hover 用 `--zhi-3`；`tr[data-sev]` 左缘色条 inset 方案；`:focus-visible` 朱红 2px 外环。
- 状态徽章：`.bdg--success/--warning/--danger/--info/--neutral`（tint 底 + soft 描边 +
  deep 文字）；`.bdg--danger-solid` 实底仅用于“严重/运行中”。
- 时间线：`.tl-item.err`（朱红）、`.tl-item.ai`（菱形节点 + "AI" 小徽标）；
  `.stage[data-state="pending"]` 虚线卡。
- 按钮：主/次沿用 btn-zhu/btn-mo；新增 `.btn-danger`（实底朱深，破坏性操作）与
  `.btn-ghost-danger`（行内低噪声危险）。

**质感总原则**：层次=1px 线+留白+暖灰轻阴影（禁纯黑）；实底彩色仅三处
（朱红主按钮/danger 实底/藤黄脉动节点）；数字一律 mono + tabular-nums；
新组件只用 token，禁止硬编码色值（否则暗色主题静默失效）。
