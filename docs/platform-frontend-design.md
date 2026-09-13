# FishCloud 前端改造设计文档（Phase 1）

> 制定：辅助平台（子代理配额耗尽期间由主会话完成）。视觉规范见 `docs/fishcloud-design.md`（dsq），
> 后端接口见 `docs/platform-backend-design.md`。技术栈不变：Vue 3.5 + TS + Pinia + Router4 + Vite + 自移植设计体系。

## 1. 路由表（全量）

守卫：`router.beforeEach` —— 无 token 且目标非 `/login` → `/login`；有 token 访问 `/login` → `/`。
角色标记 `meta.roles`：未标注=登录即可；`['admin']`=仅管理员。

| 路径 | 名称 | 组件 | 菜单分组 | 角色 |
| --- | --- | --- | --- | --- |
| /login | login | views/LoginView.vue | 无（独立布局） | 公开 |
| / | overview | views/OverviewView.vue（新写：真实 KPI） | 运行概览 | 登录 |
| /aiops/chat | aiops-chat | views/ChatView.vue（迁移） | AIOps | 登录 |
| /aiops/knowledge | aiops-knowledge | views/KnowledgeView.vue（迁移） | AIOps | 登录 |
| /aiops/retrieval | aiops-retrieval | views/RetrievalView.vue（迁移） | AIOps | 登录 |
| /aiops/eval | aiops-eval | views/EvalView.vue（迁移） | AIOps | 登录 |
| /aiops/agent | aiops-agent | views/AgentView.vue（迁移） | AIOps | 登录 |
| /assets | asset-list | views/assets/AssetListView.vue | 资源中心 | 登录 |
| /assets/:id | asset-detail | views/assets/AssetDetailView.vue | 资源中心(隐藏菜单) | 登录 |
| /events | event-list | views/events/EventListView.vue | 事件中心 | 登录 |
| /events/:id | event-detail | views/events/EventDetailView.vue | 事件中心(隐藏菜单) | 登录 |
| /admin | admin | views/admin/AdminView.vue | 系统管理 | admin |
| /settings | settings | views/SettingsView.vue（迁移） | 系统管理 | 登录 |
| /observability | observability | views/PlaceholderView.vue | 可观测性 | 登录 |
| /tasks | tasks | views/PlaceholderView.vue | 任务中心 | 登录 |
| /tickets | tickets | views/PlaceholderView.vue | 工单系统 | 登录 |
| /:pathMatch(.*)* | — | redirect / | — | — |

菜单按 `authStore.role` 过滤：系统管理仅 admin；操作按钮（登记/编辑/流转）按 `can(action)` 判定。

## 2. views 目录重组

```
views/
  LoginView.vue            # 独立布局（左品牌面板/右表单，见 fishcloud-design.md §3.1）
  OverviewView.vue         # 重写：运行概览（Phase 1 先接 /health + 事件 KPI + 资产计数，无假数据）
  PlaceholderView.vue      # 占位页（模块名 + Phase 2 说明，复用 card 体系）
  assets/
    AssetListView.vue      # 筛选栏 + tbl + 分页 + ＋登记资产（admin/operator）
    AssetDetailView.vue    # 概览卡 + 变更历史（Phase 1 用审计日志回填）
    components/AssetForm.vue   # 新建/编辑表单（弹窗，字段校验）
  events/
    EventListView.vue      # KPI 条 + 状态 Tab + 严重度色条表格
    EventDetailView.vue    # 信息卡 + 复盘时间线 + AI 研判按钮/记录
    components/DiagnosisPanel.vue  # 研判记录展示（置信度/证据编号/降级原因）
  admin/
    AdminView.vue          # 三 Tab：用户 / 角色 / 审计
    components/UserForm.vue
chat/ knowledge/ retrieval/ eval/ agent/ settings/   # 迁移视图各自独立目录（保持现有文件平移）
```

删除：PipelineView / MonitorView / FeedbackView / StackView / AlertsView / TraceView（含路由与菜单项）。
`data/prototype.ts` 中仅被删除页引用的演示数组一并清理；被保留页引用的（如流水线阶段数据若 Agent 页用）保留。

## 3. Pinia store

```
stores/auth.ts      # state: token(localStorage 'fc.token')/user{username,role}/
                    # actions: login()/logout()/fetchMe()；getter: isAdmin/isOperator、can(action)
stores/filter.ts    # 业务上下文：当前环境/业务线全局筛选（跨页持久，localStorage 'fc.filter'）
```

模块级 store 暂不建（列表页本地 ref 足够），避免过度设计。

## 4. API 客户端层

新增 `src/api/http.ts`：统一 `request<T>(method, path, {query, body})` ——
自动附 `Authorization: Bearer <token>`；401/403 统一处理（401 清 token 跳 /login，403 toast）；
返回解析 `detail` 字段做错误提示。**选 fetch 封装而非 axios**：零新依赖（SSE 已用 fetch 流）、与 rag.ts 风格一致。

```
src/api/auth.ts    # login(username,password) / me() / changePassword(old,new)
src/api/assets.ts  # listAssets(params)/getAsset(id)/createAsset/updateAsset/deleteAsset/
                   # listBusinessLines/createBusinessLine/.../listEnvironments/...
src/api/events.ts  # listEvents(params)/getEvent(id)/createEvent/acknowledge/resolve/close/
                   # addComment/triggerDiagnosis/getDiagnosis/listDiagnoses
                   # (SSE 观察端点为 Phase 1 增强，默认轮询 getDiagnosis)
src/api/admin.ts   # listUsers/createUser/updateUser/resetPassword/listRoles/
                   # listAlertRules/CRUD/listAuditLogs
```

类型定义：`src/api/types.ts` —— User/Role/Asset/BusinessLine/Environment/Event/
TimelineEntry/DiagnosisRecord/Page<T>（与后端 Pydantic schema 字段一一对应，camelCase）。

## 5. 现有 13 视图迁移映射

| 视图 | 处置 | 去向 |
| --- | --- | --- |
| OverviewView | **重写** | 运行概览（真实数据） |
| ChatView / KnowledgeView / RetrievalView / EvalView / AgentView | 保留迁移 | AIOps 分组 |
| TraceView | **删除**（能力并入 EventDetail 时间线） |
| AlertsView | **删除**（告警规则 CRUD 并入 EventList 头部入口 + /admin） |
| MonitorView / FeedbackView / StackView / PipelineView | **删除** |
| SettingsView | 保留迁移 | 系统管理分组 |

## 6. rag.ts 登录态改造

- `live` 模式 fetch `/api/v1/diagnose` 时附 `Authorization` 头（从 authStore/localStorage 取）；
- 401 响应 → 清 token → 跳 `/login`（与 http.ts 共用处理函数）；
- `direct`/`demo` 模式不受影响。

## 7. 实施顺序（每步独立验证）

1. http.ts + auth store + 登录页 + 路由守卫（后端 auth 就绪后联调）
2. App.vue 菜单重构 + 新路由表 + 删除演示视图（Vite 构建通过）
3. 资产列表/详情 + assets.ts（联调 CRUD）
4. 事件列表/详情 + events.ts + DiagnosisPanel（联调状态流转与研判轮询）
5. AdminView 三 Tab + admin.ts
6. OverviewView 重写（真实 KPI）+ 占位页
7. `npm run build` 全量回归 + type-check

> 上述 1–7 已全部实施完成（Phase 1 基线提交 `f8a2ef0`）。以下为 2026-09-13 用户 Q 裁决后的落地增补。

## 8. Q 裁决落地增补（2026-09-13，本文档正文与实现同步）

### 8.1 应用外壳：深色侧栏（Q1 裁决）
- 组件：`components/shell/AppSidebar.vue`（AppTopBar/AppSideNav 已删除）；`#app` 纵向改横向：侧栏 + 内容区。
- 侧栏 = 品牌面板（鱼印章 32px + FishCloud 宋体 + 副标题）→ 分组菜单 → 底部（身份/角色 + 设置 + 退出 + 配色切换器 + 收起开关）。
- 三主题侧栏由 `--side-*` token 派生（blue-pink `#3B82F6→#2563EB→#1E3A8A`+scrim；teal `#2DD4BF→#14B8A6→#0F766E`；zhu `#30302E→…` 墨色）；分组标题前景取 `--side-fg`（AA 达标），`--side-fg-dim` 只给装饰文本。
- 宽度 `--sidenav-w:224px` / 收起 `--sidenav-w-min:60px`（收起形态文案用 clip 裁剪，保可访问名；≤860px 强制图标栏）。

### 8.2 导航 8 组（按 PRD 路由表分组列）
`运行概览` / `资源中心`(资产列表+隐藏详情) / `事件中心`(列表+隐藏详情) / `AIOps`(问答·知识库·检索·评估·链路) / `系统管理`(平台管理+设置+使用指南；admin 项条目级门禁——非 admin 隐藏该条目而非整组，保住设置/指南可见) / `可观测性`(P2) / `任务中心`(P2) / `工单系统`(P2)。

### 8.3 登录页改版 + 注册（Q2/Q3 裁决）
- 登录页：整页品牌渐变 + 居中白色表单卡（偏离原 §三.1 左 44% 双栏——单组字段双栏必留大片空面板）；Enter 提交、`:focus-visible` 焦点环、提交中禁用防重复、错误位固定占位。
- 注册页 `/register` 与登录页共用视图；预检 `GET /api/v1/auth/registration-status`（8s 超时、静默）。
- **fail-open 契约**：仅明确 200 且 `allow_registration:false` 才隐藏入口/禁用表单；请求失败一律照常显示（fail-closed 曾导致注册入口消失，被用户打回）。提交侧 403 降级文案保留。
- 成功文案以 `auto_active` 为准：false→「注册已提交，待管理员启用」、true→「注册成功，请登录」。

### 8.4 AdminView 五 Tab（Q13 裁决）
用户 / 角色 / 告警规则 / 审计日志 / 系统健康 五 Tab（原三 Tab 为超集）；事件页"告警规则"按钮跳 `/admin?tab=rules`。

### 8.5 品牌与主题
`BrandSwitch` 三处入口（侧栏底部/设置页，共用 `useBrand` 单一状态源）；`html[data-brand]`（blue-pink 默认 / teal / zhu）与 `html[data-theme]`（light/dark）正交，组合选择器 (0,2,1)；`theme-color` meta 随动；持久化 `fishcloud.app.settings.v1`。

### 8.6 模型选择器
- `providers.ts` GLM 预设 `models:["glm-4.7-flash","glm-4-flash"]`（均免费；note 标明 4.7 偶发 429/1305 可切 4-flash）。
- 设置页模型接入：GLM 时模型字段为下拉（两免费档 + 自定义文本），baseUrl 内置，用户只填 API Key。
- 智能问答输入区紧凑切换器：选中即写 `fishcloud.llm.settings.v1`（只改 model；切免费档连带 providerId/baseUrl，防"别家地址+GLM 模型名"）；旁注当前模式（直连 `<model>` / 后端模型）。

### 8.7 历史研判取数
`events.ts` 新增 `listEventDiagnoses(eventId)`（`GET /events/{id}/diagnoses`，信封 `{items,total}` 兼容裸数组，按 `startedAt` 倒序）；EventDetailView 以此替换"时间线回填"过渡实现；单条轮询（2s×60）不变；SSE 观察端点后端已就绪，前端默认仍轮询（PRD §5.4 轮询为推荐路径）。

### 8.8 渐变体系与模块色族（第二轮迭代进行中）
- `tokens.css` `--grad-*` 全套（brand/overview/asset/event/aiops/observability/tasks/tickets/success/danger/admin/admin-side），全部由 accent token 派生，data-brand/data-theme 自动跟随。
- 已落地：`[data-module] .page-head::before` 页头条、`--grad-admin-side` 管理侧栏。
- 第二轮迭代（用户反馈"太单一"）：模块色族扩大到 KPI 卡、区块标题、表头、primary 按钮、徽章、侧栏八组激活态；管理员区域统一 `--grad-admin`（深蓝→紫）族与业务页隔离；正文阅读区保持纯净。状态见交付报告。
