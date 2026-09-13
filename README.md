# FishCloud · 智能运维平台

> 面向真实运维现场的智能运维平台：把 **资产登记、事件中心、AI 研判、RBAC 权限与审计** 组织成可审计、可确认、可执行的工作流。
> 后端 FastAPI + SQLAlchemy 2.x（MySQL 优先 / SQLite 兜底），前端 Vue 3.5 + TypeScript，AI 链路复用自研 LangGraph Agent（混合检索 + 重排 + 幻觉校验）。

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white">
  <img alt="Vue" src="https://img.shields.io/badge/Vue-3.5-42B883?logo=vuedotjs&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5.6-3178C6?logo=typescript&logoColor=white">
  <img alt="MySQL" src="https://img.shields.io/badge/MySQL-8.0-4479A1?logo=mysql&logoColor=white">
  <img alt="tests" src="https://img.shields.io/badge/tests-731%20passed-brightgreen">
</p>

---

## 目录

- [一、项目定位](#一项目定位)
- [二、功能地图](#二功能地图)
- [三、系统架构](#三系统架构)
- [四、核心流程：告警 → AI 研判 → 事件闭环](#四核心流程告警--ai-研判--事件闭环)
- [五、事件状态机](#五事件状态机)
- [六、数据模型](#六数据模型)
- [七、权限矩阵（RBAC）](#七权限矩阵rbac)
- [八、目录结构（前端 / 后端标注）](#八目录结构前端--后端标注)
- [九、技术栈](#九技术栈)
- [十、快速开始](#十快速开始)
- [十一、测试](#十一测试)
- [十二、打包与发布](#十二打包与发布)
- [十三、安全说明](#十三安全说明)
- [十四、许可](#十四许可)

---

## 一、项目定位

FishCloud 面向**软件工程向的运维场景**：应用服务与接口、发布与变更、依赖链路、日志与性能、数据库与中间件。

平台把一次运维处置拆成可追溯的链路：

```
外部告警（Zabbix / Grafana / 自研脚本）
      │  webhook（令牌鉴权）
      ▼
  事件中心（状态机驱动，全程留痕）
      │  一键触发
      ▼
  AI 研判（混合检索知识库 + 重排 + 幻觉校验 → 根因 / 建议 / 证据编号 / 置信度）
      │  人工确认
      ▼
  处置闭环（恢复 / 关闭，写回时间线与审计）
```

设计原则：

| 原则 | 说明 |
| --- | --- |
| **实际可用** | 每个页面接真实接口，空态即空态；**无后端 / 无模型时明确报错，绝不返回伪造答案** |
| **可审计** | 登录成败、状态流转、研判触发、CMDB 写操作全部落审计；事件时间线只追加不修改 |
| **证据约束** | 研判结论受证据钳制：有知识库证据置信度 ≤ 0.9；仅有资产证据 ≤ 0.5 并标记降级；**无证据不出根因** |
| **人机边界** | AI 只产出根因 / 建议 / 证据，**恢复（resolved）永远由人工触发** |
| **零侵入共存** | 平台路由以子路由挂载在既有 RAG 服务之上，两套链路共享同一进程、互不影响 |

---

## 二、功能地图

```mermaid
graph LR
    subgraph AUTH["认证与权限"]
        A1[登录 / 注册]
        A2[JWT 会话]
        A3[三级角色 RBAC]
        A4[修改密码]
    end

    subgraph CMDB["资源中心"]
        C1[业务线]
        C2[环境]
        C3[资产登记]
        C4[标识三元组唯一]
    end

    subgraph EVENT["事件中心"]
        E1[webhook 接入]
        E2[手工登记]
        E3[状态机流转]
        E4[复盘时间线]
        E5[告警规则引擎]
    end

    subgraph AI["AI 研判"]
        I1[混合检索]
        I2[重排]
        I3[根因 / 建议]
        I4[证据编号 + 置信度]
        I5[幻觉校验]
    end

    subgraph ADMIN["系统管理"]
        S1[用户管理]
        S2[角色只读]
        S3[告警规则]
        S4[审计日志]
        S5[系统健康]
    end

    subgraph OPS["AIOps 工具"]
        O1[智能问答]
        O2[知识库]
        O3[检索调参]
        O4[效果评估]
        O5[Agent 链路]
    end

    C3 --> E1
    E1 --> E3
    E5 --> E1
    E3 --> I1
    I1 --> I2 --> I3 --> I4 --> I5
    I4 --> E4
    A3 -.约束.- ADMIN
    A3 -.约束.- CMDB
    A3 -.约束.- EVENT
    O2 --> I1
```

---

## 三、系统架构

```mermaid
graph TB
    subgraph CLIENT["浏览器 / 桌面壳"]
        UI["Vue 3.5 + TS<br/>（顶栏 + 深色侧栏 · 三主题换肤）"]
        DESK["桌面启动器<br/>FishCloud.exe / 安装版"]
    end

    subgraph API["FastAPI 单体服务（uvicorn）"]
        direction TB
        subgraph PLATFORM["平台链路 /api/v1/{auth,assets,events,admin}"]
            P1["认证 · JWT + RBAC"]
            P2["CMDB 资产"]
            P3["事件状态机 + 时间线"]
            P4["审计"]
            P5["研判编排"]
        end
        subgraph RAG["AIOps 链路 /api/v1/{diagnose,retrieve,upload,evaluate,trace}"]
            R1["LangGraph Agent<br/>拆解→检索→反思→生成→幻觉校验"]
            R2["混合检索 BM25 + 向量 + RRF"]
            R3["重排序 bge-reranker"]
        end
    end

    subgraph DATA["存储与模型"]
        D1[("MySQL 8.0<br/>优先")]
        D2[("SQLite<br/>兜底")]
        D3["向量库 / 图谱（内存）+ 快照"]
        D4["bge-m3 / bge-reranker"]
    end

    LLM["大模型服务<br/>GLM 免费档 / DeepSeek（可选）"]

    UI -->|REST + JWT| PLATFORM
    UI -->|REST + JWT| RAG
    DESK --> UI
    P5 --> R1
    P3 --> P5
    R1 --> R2 --> R3
    R3 --> D4
    P1 --> D1
    P2 --> D1
    P3 --> D1
    D1 -.连接失败时回落.-> D2
    R2 --> D3
    R1 -->|OpenAI 兼容协议| LLM
```

**分层职责**

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 前端 SPA | `frontend/` | 视图 / 组件 / 状态管理 / 路由守卫 / SSE 消费 |
| 平台 API | `src/api/platform/` | 认证、CMDB、事件、管理域路由（依赖注入 + RBAC） |
| 平台领域层 | `src/platform/` | 模型、状态机、审计、研判编排、事务边界 |
| AIOps API | `src/api/routes.py` | 诊断 / 检索 / 上传 / 评估 / 链路（读写分档鉴权） |
| Agent 编排 | `src/agent/` | LangGraph 状态机、工具、幻觉校验 |
| 检索与知识 | `src/retrieval/` `src/knowledge/` | 向量库、BM25、重排、图谱 |
| 核心库 | `core/` | 预处理、混合检索实现 |

---

## 四、核心流程：告警 → AI 研判 → 事件闭环

```mermaid
sequenceDiagram
    autonumber
    participant EXT as 外部告警系统
    participant API as 平台 API
    participant RULE as 规则引擎
    participant EV as 事件状态机
    participant DIAG as 研判编排
    participant AGENT as LangGraph Agent
    participant LLM as 大模型

    EXT->>API: POST /events/webhook/{source}（X-Webhook-Token）
    API->>API: 令牌校验（失败 401）
    API->>RULE: 匹配告警规则（字段 / 操作符 / 值）
    RULE-->>API: 命中规则（severity / auto_diagnose）
    API->>EV: 建事件（open）+ 时间线（created / rule_matched）
    API-->>EXT: 202 Accepted

    alt 规则开启自动研判
        API->>DIAG: 入队研判（子系统身份）
    end

    Note over EV: 人工触发研判时：open 自动认领 → acknowledged → diagnosing

    DIAG->>AGENT: 组装配确定性证据 + 资产上下文
    AGENT->>LLM: 意图拆解 → 生成 → 幻觉校验
    LLM-->>AGENT: 根因 / 建议 / 置信度（JSON 三段式）
    AGENT-->>DIAG: 答案 + 证据来源

    DIAG->>DIAG: 证据钳制（≤0.9 / ≤0.5 / 无证据不出根因）
    DIAG->>EV: 落研判记录 + 时间线（diagnosis_finished）
    EV->>EV: 完成 / 失败 / 超时 → 回退 acknowledged

    Note over EV: 人工确认后：acknowledged → resolved → closed
```

研判结果的证据与置信度约束：

| 证据情况 | 置信度处理 | 降级标记 |
| --- | --- | --- |
| 存在 ≥1 条知识库证据 | 模型给值，**钳制 ≤ 0.9** | — |
| 仅有资产证据（无知识命中） | 强制 **≤ 0.5** | `insufficient_evidence` |
| 无任何证据 | **不出根因** | `insufficient_evidence` |
| 模型未给置信度 / 解析失败 | 显示「未知」，**不得臆造** | `unparsed` |

---

## 五、事件状态机

```mermaid
stateDiagram-v2
    [*] --> open: webhook 接入 / 手工登记
    open --> acknowledged: 认领（或触发研判时系统自动认领）
    open --> closed: 直接关闭
    acknowledged --> diagnosing: 触发 AI 研判
    acknowledged --> resolved: 人工恢复（需处置说明）
    acknowledged --> closed: 关闭
    diagnosing --> acknowledged: 研判完成 / 失败 / 超时回退
    diagnosing --> resolved: 人工恢复
    diagnosing --> closed: 关闭
    resolved --> closed: 关闭
    closed --> [*]: 终态（不可再流转 / 评论）
```

约束：

- **每次流转**都写时间线（`from → to` + 操作者）与审计，同一事务提交；
- 非法流转返回 **409** 且状态不变；
- 事件关闭（`closed`）为终态，之后任何流转与评论均 409；
- **`resolved` 只能人工触发**——AI 不自动处置。

---

## 六、数据模型

```mermaid
erDiagram
    ROLES ||--o{ USERS : "拥有"
    USERS ||--o{ AUDIT_LOGS : "产生"
    BUSINESS_LINES ||--o{ ENVIRONMENTS : "包含"
    ENVIRONMENTS ||--o{ ASSETS : "包含"
    ASSETS ||--o{ EVENTS : "关联"
    EVENTS ||--o{ EVENT_TIMELINE_ENTRIES : "留痕"
    EVENTS ||--o{ DIAGNOSIS_RECORDS : "研判"

    ROLES {
        int id PK
        string code "admin/operator/viewer"
        string name
    }
    USERS {
        int id PK
        string username UK
        string password_hash "bcrypt"
        int role_id FK
        bool is_active "停用代替删除"
    }
    AUDIT_LOGS {
        int id PK
        string action "user.login / event.transition ..."
        string resource_type
        string resource_id
        json detail
        string ip
    }
    BUSINESS_LINES {
        int id PK
        string code UK
        string name UK
        int owner_id FK
    }
    ENVIRONMENTS {
        int id PK
        int business_line_id FK
        string name "业务线内唯一"
    }
    ASSETS {
        int id PK
        int environment_id FK
        string asset_type "host/db/middleware/app/network"
        string identifier "环境+类型+标识 三元组唯一"
        string status "active/maintenance/decommissioned"
    }
    EVENTS {
        int id PK
        string event_no UK "EV-YYYYMMDD-####"
        string title
        string source "webhook:xx / manual"
        string severity "critical/major/minor/info"
        string status "状态机字段"
        int asset_id FK
        json payload
    }
    EVENT_TIMELINE_ENTRIES {
        int id PK
        int event_id FK
        string entry_type "created/status_change/comment/rule_matched/diagnosis_*"
        string actor_type "user/system/agent"
        text content
    }
    DIAGNOSIS_RECORDS {
        int id PK
        int event_id FK
        string status "running/completed/failed/timeout"
        text query "送入 agent 的问题文本"
        text root_cause
        text suggestion
        float confidence "CHECK 0~1"
        json evidence "seq/kind/ref/title/snippet"
        bool degraded
        string degraded_reason
        string trace_id
    }
    ALERT_RULES {
        int id PK
        string match_field "title/source/asset.identifier"
        string match_op "contains/eq/regex"
        string severity
        bool auto_diagnose
        bool enabled
    }
```

---

## 七、权限矩阵（RBAC）

角色三档：`admin`（管理员）、`operator`（运维工程师）、`viewer`（只读观察员）。

| 能力域 | admin | operator | viewer |
| --- | :---: | :---: | :---: |
| 登录 / 个人档案 / 改密 / 评论查看 | ✓ | ✓ | ✓ |
| CMDB 读（业务线 / 环境 / 资产 / 事件 / 研判） | ✓ | ✓ | ✓ |
| AIOps 读（检索 / 链路 / 知识库查看 / 评估结果） | ✓ | ✓ | ✓ |
| 资产写、事件登记 / 认领 / 恢复 / 关闭 / 评论 | ✓ | ✓ | ✗ |
| 触发 AI 研判、问答与诊断、知识库上传 / 删除、触发评估 | ✓ | ✓ | ✗ |
| 业务线 / 环境维护、资产删除、用户与告警规则管理 | ✓ | ✗ | ✗ |
| 审计日志查询、系统健康 | ✓ | ✗ | ✗ |
| 告警 webhook 入口 | 不走 RBAC，走 `X-Webhook-Token` | | |

实现要点：JWT 只承载身份，**每次请求回库校验**（角色变更 / 停用即时生效）；能力判定走代码内能力矩阵（`ROLE_MATRIX`），而非角色名白名单。

---

## 八、目录结构（前端 / 后端标注）

```
FishYW/
├── frontend/                   ★ 前端（Vue 3.5 + TypeScript + Vite）
│   ├── src/
│   │   ├── api/                接口层（fetch 封装 / 401 统一处理 / 类型契约）
│   │   ├── components/         通用组件（壳层、分页、Toast、品牌切换、图标）
│   │   ├── composables/        组合式逻辑（主题、外壳、计数缓存、通知）
│   │   ├── navigation/         侧栏导航定义（8 组 14 项，按角色过滤）
│   │   ├── router/             路由表 + 登录守卫
│   │   ├── stores/             Pinia（认证、全局筛选）
│   │   ├── styles/             设计体系（tokens / layout / platform / modules）
│   │   └── views/              页面
│   │       ├── assets/         资源中心（列表 / 详情 / 表单 / 业务线环境）
│   │       ├── events/         事件中心（列表 / 详情 / 研判面板）
│   │       ├── admin/          系统管理（用户 / 角色 / 规则 / 审计 / 健康）
│   │       ├── chat/ knowledge/ retrieval/ eval/ agent/   AIOps 工具组
│   │       ├── help/           使用指南
│   │       └── settings/       设置（外观 / 模型接入 / 账户安全）
│   ├── index.html
│   └── package.json
│
├── src/                        ★ 后端（Python / FastAPI）
│   ├── api/                    HTTP 层
│   │   ├── routes.py           AIOps 端点（诊断/检索/上传/评估/链路）+ 应用装配
│   │   ├── jobs.py             内存任务注册表（摄取任务的取消语义）
│   │   └── platform/           平台端点：auth / assets / events / admin
│   ├── platform/               ★ 平台领域层
│   │   ├── models/             SQLAlchemy 模型（用户/审计/CMDB/事件/研判）
│   │   ├── services/           事件状态机、时间线、规则匹配、研判编排
│   │   ├── db.py               MySQL 优先 + SQLite 兜底 + 惰性初始化 + 种子
│   │   ├── security.py         bcrypt 口令 + PyJWT 签发校验
│   │   ├── deps.py             当前用户解析 + 角色能力矩阵
│   │   └── audit.py            审计写入（与业务同事务）
│   ├── agent/                  LangGraph Agent（状态机 / 工具 / 幻觉校验）
│   ├── retrieval/              向量库 / BM25 / 重排
│   ├── knowledge/              图谱构建与存储
│   ├── evaluation/             RAGAS 评估
│   ├── data/                   文档加载与切分
│   └── settings.py             配置单例（含缓存目录重定向、离线开关）
│
├── core/                       ★ 核心算法（预处理 / 混合检索实现）
├── tests/                      ★ 测试
│   ├── platform/               平台测试（认证 / RBAC / CMDB / 事件 / 研判 / 审计 / SSE）
│   └── test_*.py               RAG 链路、检索、Agent、零磁盘约束等
├── docs/                       ★ 文档
│   ├── HANDOFF.md              交接总纲（进度 / 决策 / 踩坑 / 速查）
│   ├── platform-backend-design.md    后端设计（端点 / 状态机 / 研判约束）
│   ├── platform-frontend-design.md   前端设计（路由 / 视图 / 主题 / 契约）
│   ├── fishcloud-design.md     视觉设计（品牌 / 色族 / 布局蓝图）
│   ├── platform-wireframe-lowfi.html 低保真线框图（全流程平铺审阅稿）
│   ├── page-walkthrough.html   页面走查系统（38 屏实拍 + 反馈回填）
│   └── walkthrough-shots/      走查截图
├── desktop/                    ★ 桌面打包
│   ├── fishcloud_desktop.py    单进程桌面壳（uvicorn + 原生窗口）
│   ├── launcher_exe.py         免安装版 exe 启动器源码
│   └── installer/              安装版向导（Inno Setup 脚本 + 许可条款）
├── release/                    ★ 打包产物（已在 .gitignore 排除，见第十二节）
└── requirements.txt            Python 依赖
```

> `release/` 目录**不进版本库**：安装包约 276MB、免安装包约 1.4GB，均超出 Git 单文件限制；它们通过 **GitHub Releases** 分发（见下节）。

---

## 九、技术栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 后端框架 | FastAPI + Uvicorn | 单体服务，平台链路与 AIOps 链路同进程共存 |
| ORM / 存储 | SQLAlchemy 2.0（typed）+ PyMySQL | **MySQL 优先，连接失败自动回落 SQLite**（WAL + busy_timeout） |
| 认证 | PyJWT（HS256）+ bcrypt | 无状态令牌 + 每请求回库校验（停用 / 改角色即时生效） |
| AI 编排 | LangGraph | 五节点状态机：拆解 → 检索 → 反思 → 生成 → 幻觉校验 |
| 检索 | bge-m3 + bge-reranker-v2-m3 | 向量 + BM25 混合召回，RRF 融合后重排 |
| 大模型 | GLM 免费档（默认）/ DeepSeek（可选） | OpenAI 兼容协议；**无凭据时明确报错，不伪造答案** |
| 前端 | Vue 3.5 + TypeScript + Pinia + Vue Router 4 + Vite | 手写设计体系（tokens 驱动，三主题 × 明暗正交） |
| 桌面 | pywebview + 内嵌 Python 便携目录 | 免安装版（单 exe 启动）与安装版（Inno Setup 向导） |
| 测试 | pytest | 731 用例：平台域 + RAG 链路 + 零磁盘约束 |

---

## 十、快速开始

### 方式一：免安装 / 安装版（推荐，零配置）

1. 从 [Releases](../../releases) 下载安装包或免安装包；
2. 双击启动，桌面窗口自动打开；
3. 使用管理员账号登录（口令由部署时配置，见 `PLATFORM_ADMIN_PASSWORD`）；
4. 首次使用建议在 **设置 → 账户安全** 自助改密，并在 **系统管理 → 用户** 维护账号。

安装包内置便携 Python 与全部依赖，**无需安装 Python、Node 或数据库**（未配置 MySQL 时自动使用 SQLite）。

### 方式二：源码运行（开发）

```bash
# 依赖（Python 3.12）
pip install -r requirements.txt

# 配置：复制模板并按需填写模型凭据
cp .env.example .env

# 启动后端（首次启动自动建表；加载检索模型约需数十秒）
python -m uvicorn src.api.routes:app --host 127.0.0.1 --port 8000

# 启动前端（另开终端）
cd frontend
npm install
npm run dev          # 默认 http://127.0.0.1:5173
```

> 检索模型（bge-m3 / bge-reranker）首次使用需下载约 4.4GB，可通过 `HF_HOME` 指定缓存目录。
> 若暂时不启用检索：模型缺失时服务会**降级启动**（检索相关端点返回 503 并说明原因），登录、资产、事件、RBAC 等平台功能不受影响。

---

## 十一、测试

```bash
# 全量测试（零磁盘落盘：临时目录与缓存均在项目外）
python -B -m pytest -p no:cacheprovider -q tests/
```

**最近一次实测结果：**

```
731 passed, 1 skipped in 103.58s
```

（1 个 skip 为需真实重排序权重的门控用例，默认跳过以避免大体积下载。）

### 测试覆盖

```mermaid
graph LR
    subgraph PLAT["tests/platform/ —— 平台域（458 例）"]
        P1["认证：登录成败审计 / JWT 过期伪造篡改 / 停用<br/>注册开关三态 / 改密"]
        P2["RBAC：端点 × 角色全矩阵 / 能力语义 / 自我保护"]
        P3["CMDB：CRUD / 三元组 409 / 删除依赖 409 / 分页过滤"]
        P4["事件：编号自增 / 状态机全组合 / webhook 三态<br/>规则匹配 / 无条件审计"]
        P5["研判：证据钳制矩阵 / 超时回退 / 永不自动恢复<br/>三段式解析 / 死锁与列表序列化回归"]
        P6["观测：SSE 观察窗口 / 审计同事务 / 路由清单"]
    end
    subgraph RAGL["tests/ —— RAG 链路与工程约束"]
        R1["检索：RRF 融合名次语义 / 重排 / 向量库并发"]
        R2["Agent：节点行为 / 幻觉校验 / 多轮对话"]
        R3["摄取：PDF / DOCX 内存解析 / 分批实体抽取"]
        R4["工程：零磁盘落盘 AST 扫描 / 配置单例 / 取消语义"]
    end
```

### 回归发现的典型缺陷（已修复，作为质量证据）

| 级别 | 缺陷 | 根因 |
| --- | --- | --- |
| P0 | 平台首个请求永久挂起 | 非重入锁在初始化路径上自死锁 |
| P0 | 列表端点有数据即 500 | 泛型注解多套一层 list，响应校验失败（空库恰好通过） |
| P0 | AIOps 接口完全匿名可访问 | 路由组未挂鉴权依赖 |
| P1 | 资产登记提交必失败 | 前端发 camelCase，后端契约为 snake_case |
| P1 | webhook 同标识多资产 500 | `one_or_none()` 遇多命中抛异常 |

---

## 十二、打包与发布

### 产物位置约定（重要）

| 类型 | 位置 | 原因 |
| --- | --- | --- |
| **源代码 / 文档 / 测试** | 本仓库 | — |
| **安装版 `FishCloud-Setup-*.exe`（约 276MB）** | **GitHub Releases** | 超出 Git 单文件 100MB 限制，不进版本库 |
| **免安装版（约 1.4GB）** | **GitHub Releases** | 同上；内含便携 Python 运行时 |

> 仓库中的 `release/` 目录已在 `.gitignore` 中排除，**不会**被提交；可执行包请从 Releases 页面获取。

### 自行构建

```bash
# 1) 构建前端产物
cd frontend && npm run build && cd ..

# 2) 免安装版：准备便携 Python 目录并安装依赖
#    release/免安装版/App/python                      ← 便携 Python 基座
#    release/免安装版/App/{src,core,desktop,frontend_dist}
#    pip install --target release/免安装版/App/python/Lib/site-packages -r requirements.txt

# 3) 免安装版启动器（单文件 exe）
pyinstaller --onefile --name FishCloud --distpath release/免安装版 desktop/launcher_exe.py

# 4) 安装版（Inno Setup 6；脚本含许可条款页与自定义安装位置页）
ISCC.exe desktop/installer/FishCloud.iss
```

---

## 十三、安全说明

- **仓库不含任何密钥**：所有凭据通过 `.env`（已在 `.gitignore` 中）或系统环境变量注入；`.env.example` 仅含占位符。
- **默认口令仅用于本地开箱体验**，生产部署请务必修改管理员口令并配置独立 `JWT_SECRET`。
- **AIOps 接口已鉴权**：读操作需登录（只读角色即可），写操作（问答 / 诊断 / 上传 / 删除 / 评估）需运维角色及以上；健康检查保持公开（供启动器与监控探活）。
- **审计留痕**：登录成败、事件流转、研判触发与完成、CMDB 与用户管理写操作全部落审计（含操作人与来源 IP）。
- 平台数据全部存储于自有数据库，不上传任何第三方；调用外部大模型服务时，仅发送研判 / 问答所需的上下文文本。

---

## 十四、许可

本项目采用 **MIT License** 开源。

内置或依赖的第三方组件（Python / PyTorch / FastAPI / SQLAlchemy / Vue / Vite / Transformers / sentence-transformers / FAISS / jieba 等）遵循各自原始许可；安装版向导内置完整第三方组件许可说明。

---

<p align="center"><sub>FishCloud · 面向真实运维现场的智能运维平台</sub></p>
