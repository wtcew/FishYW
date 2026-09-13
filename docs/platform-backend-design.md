现状已核实完毕（routes.py 的 OpenAPI 断言是子集匹配、`with TestClient(routes.app)` 会触发 lifespan、agent mock 走 monkeypatch 替身、tests/ 无 conftest.py、settings.py 自我声明“只读不写盘”、requirements.txt 无 SQLAlchemy/JWT/密码库）。以下为后端改造设计文档。

---

# FishCloud 后端改造设计文档（Phase 1）

## 0. 架构总览与技术栈

```
                        ┌────────────────────────────────────────────────┐
                        │              FastAPI 单体 (src/api/routes.py)   │
  现有 RAG 链路（不动）  │  /api/v1/health diagnose query upload retrieve │
  ◀─────────────────────│  evaluate trace feedback knowledge/*           │
                        │  ─── 同一 app，include 平台子路由 ───────────   │
  新平台链路（本期）     │  /api/v1/auth/*  /assets/*  /events/*  /admin/*│
  ◀─────────────────────│  (src/api/platform/*)                          │
                        └───────┬───────────────────────┬────────────────┘
                                │ sync Session          │ asyncio
                        ┌───────▼────────┐      ┌───────▼─────────────────┐
                        │ src/platform   │      │ 复用内存 AIOps 引擎       │
                        │ models/services│      │ AppState.agent_graph     │
                        │ security/audit │      │ (LangGraph+检索+幻觉校验) │
                        └───────┬────────┘      └─────────────────────────┘
                                │ SQLAlchemy 2.0
                        ┌───────▼────────────────────┐
                        │ SQLite D:\xingzhi-platform\platform.db (WAL) │
                        └────────────────────────────┘
```

技术栈增量（requirements.txt 追加，含版本建议与理由）：

| 依赖 | 版本 | 理由 / 权衡 |
|---|---|---|
| sqlalchemy | >=2.0 | 2.0 typed style（`Mapped`/`mapped_column`）；SQLite 用同步 Session（短事务），不为 SQLite 引入 aiosqlite 的复杂度 |
| pyjwt | >=2.8 | python-jose 维护停滞且有 CVE 历史，PyJWT 是当前事实标准 |
| bcrypt | >=4.1 | 直接用 bcrypt 原生 API；不用 passlib（1.7.4 与 bcrypt>=4.1 有兼容告警，多一层无谓依赖） |

设计理由汇总（贯穿全文的三个原则）：
1. **共存不侵入**：现有端点零改动、零鉴权（Phase 1 显式决策，Phase 2 再评估给 `/diagnose` 等加鉴权）；平台路由以子路由挂载。
2. **写入口唯一**：事件状态流转、时间线追加、审计写入都收敛在 `src/platform/services/`，路由层不得直改状态。
3. **持久化与推理解耦**：平台层只做 CRUD/状态机/编排；AI 引擎维持内存态，研判结果落 `diagnosis_records`，重启不丢结论但丢不了引擎上下文（可接受：研判是一次性任务，非会话）。

---

## 1. 文件布局

```
src/platform/
  __init__.py
  db.py                  # get_engine()/get_session()/get_db 依赖/init_platform_db()
                         # 惰性初始化（首次 get_db 才建引擎+建表+种子），线程锁保护
  security.py            # hash_password/verify_password、create_access_token/
                         # decode_token（PyJWT HS256）
  deps.py                # get_current_user、require_roles() 工厂、oauth2_scheme
  audit.py               # write_audit(session, actor, action, resource, detail, ip)
  models/
    __init__.py          # 汇总 re-export 全部模型（供 Base.metadata.create_all）
    user.py              # User, Role
    audit.py             # AuditLog
    cmdb.py              # BusinessLine, Environment, Asset
    alerting.py          # AlertRule, Event, EventTimelineEntry
    diagnosis.py         # DiagnosisRecord
  schemas/
    __init__.py
    auth.py  cmdb.py  events.py  diagnosis.py  common.py   # common: Page[T]/PageParams
  services/
    __init__.py
    events.py            # 事件状态机 TRANSITIONS 常量 + create/transition/append_timeline
    rules.py             # 规则匹配（keyword/regex/eq），规则列表内存缓存+变更失效
    diagnosis.py         # 研判编排：build_query→证据装配→调 agent→约束落库

src/api/
  routes.py              # 现有，仅在 app 构建后加一行 app.include_router(platform_router)
  jobs.py                # 现有，不动
  platform/
    __init__.py          # platform_router = APIRouter(prefix="/api/v1", tags=["platform"])
    auth.py  assets.py  events.py  admin.py   # 各域路由，依赖注入来自 src/platform/deps

tests/
  platform/              # 平台测试独立子目录，conftest 只作用于本目录
    conftest.py          # tmp_path SQLite、dependency_overrides(get_db)、种子用户、假 agent
    test_platform_auth.py ...
```

**为什么路由放 `src/api/platform/` 而不是 `src/platform/routes/`**：API 层职责集中在 src/api（与现有 routes.py 同层），src/platform 保持“可被测试直接 import 的领域包”，不含 FastAPI 请求对象。

---

## 2. SQLAlchemy 模型设计

通用约定：SQLite；`DateTime(timezone=True)` 存 UTC（`default=func.now()`）；主键 `id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)`；下表仅列业务字段。JSON 列用 `sqlalchemy.JSON`（SQLite 原生支持）。

### 2.1 roles / users

**roles**（简化方案：不做 permissions 表）

| 字段 | 类型 | 约束 |
|---|---|---|
| id | Integer | PK |
| code | String(32) | unique, not null（`admin`/`operator`/`viewer`，能力判定依据） |
| name | String(64) | not null（显示名） |
| description | String(255) | — |

**users**

| 字段 | 类型 | 约束 / 索引 |
|---|---|---|
| id | Integer | PK |
| username | String(64) | not null, **unique index** |
| password_hash | String(255) | not null |
| display_name | String(64) | — |
| role_id | FK roles.id | not null, index |
| is_active | Boolean | default True（停用代替删除，保审计引用完整） |
| last_login_at | DateTime | nullable |
| created_at / updated_at | DateTime | — |

关系：`users.role`（many-to-one）、`users.audit_logs`（one-to-many）。

### 2.2 audit_logs

| 字段 | 类型 | 约束 / 索引 |
|---|---|---|
| id | Integer | PK |
| user_id | FK users.id | **nullable**（系统/agent 动作、匿名登录失败时为空），index |
| username | String(64) | 冗余快照，用户停用后仍可读 |
| action | String(64) | not null, index（`user.login` / `event.transition` / `asset.create` / `diagnosis.trigger`…） |
| resource_type | String(64) | index；与 resource_id 组成复合索引 |
| resource_id | String(64) | 存字符串以兼容不同主键形态 |
| detail | JSON | before/after、匹配到的规则等 |
| ip | String(64) | — |
| created_at | DateTime | index（审计按时间倒序查询） |

### 2.3 business_lines / environments / assets（CMDB）

**business_lines**：`id PK`；`code String(64) unique not null`；`name String(128) unique not null`；`owner_id FK users.id nullable`（业务负责人）；`description Text`；时间戳。

**environments**：`id PK`；`business_line_id FK not null, index`；`name String(64) not null`；`UniqueConstraint(business_line_id, name)`；时间戳。关系：`assets` one-to-many。

**assets**

| 字段 | 类型 | 约束 / 索引 |
|---|---|---|
| id | Integer | PK |
| environment_id | FK environments.id | not null, index |
| name | String(128) | not null |
| asset_type | String(32) | not null（host/db/middleware/app/network，Pydantic 层 Literal 校验） |
| identifier | String(128) | not null（IP/主机名/实例号）；**UniqueConstraint(environment_id, asset_type, identifier)** |
| owner_id | FK users.id | nullable（运维负责人） |
| owner_contact | String(128) | nullable |
| status | String(16) | default "active"（active/maintenance/decommissioned） |
| tags | JSON | default list |
| remark | Text | — |
| created_at / updated_at | DateTime | — |

### 2.4 alert_rules / events / event_timeline_entries

**alert_rules**

| 字段 | 类型 | 约束 / 索引 |
|---|---|---|
| id | Integer | PK |
| name | String(128) | not null |
| enabled | Boolean | default True, index |
| match_field | String(32) | not null（`title` / `source` / `payload_key:<key>` / `asset.identifier`） |
| match_op | String(16) | not null（`contains` / `eq` / `regex`） |
| match_value | String(255) | not null |
| severity | String(16) | not null（critical/major/minor/info，命中后事件默认级别） |
| auto_diagnose | Boolean | default False（命中后自动触发 AI 研判） |
| description | String(255) | — |
| created_at / updated_at | DateTime | — |

**events**

| 字段 | 类型 | 约束 / 索引 |
|---|---|---|
| id | Integer | PK |
| event_no | String(32) | **unique index**，人可读编号 `EV-YYYYMMDD-<4位序号>`（当日序号查库递增，包在事务内） |
| title | String(255) | not null |
| source | String(64) | not null（`webhook:<source>` / `manual` / `rule:<rule_id>`），index |
| severity | String(16) | not null, index |
| status | String(16) | not null default "open", index；**复合索引 (status, created_at)**（事件中心默认视图） |
| asset_id | FK assets.id | nullable, index |
| environment_id / business_line_id | FK | nullable, index（从 asset 反查后冗余存储，供跨资产过滤；手工事件允许无资产） |
| payload | JSON | 外部告警原文 |
| created_by_id | FK users.id | nullable |
| acknowledged_by_id / acknowledged_at | FK / DateTime | nullable |
| resolved_at / closed_at | DateTime | nullable |
| created_at / updated_at | DateTime | — |

关系：`timeline`（one-to-many，按 created_at 升序）、`diagnoses`（one-to-many DiagnosisRecord）。

**event_timeline_entries**（复盘时间线，只追加不修改）

| 字段 | 类型 | 约束 / 索引 |
|---|---|---|
| id | Integer | PK |
| event_id | FK events.id | not null；**复合索引 (event_id, created_at)** |
| entry_type | String(32) | not null（created / status_change / comment / webhook / rule_matched / diagnosis_started / diagnosis_finished） |
| actor_type | String(16) | not null（user / system / agent） |
| actor_id | FK users.id | nullable；actor_name String(64) 冗余 |
| content | Text | — |
| detail | JSON | nullable（如 `{"from": "open", "to": "acknowledged"}`） |
| created_at | DateTime | — |

### 2.5 diagnosis_records（研判记录）

| 字段 | 类型 | 约束 / 索引 |
|---|---|---|
| id | Integer | PK |
| event_id | FK events.id | not null, index |
| status | String(16) | not null, index（running / completed / failed / timeout） |
| trigger_type | String(16) | not null（manual / rule） |
| triggered_by_id | FK users.id | nullable |
| query | Text | not null（实际送入 agent 的问题文本，含资产上下文） |
| root_cause | Text | nullable |
| suggestion | Text | nullable |
| confidence | Float | nullable；**CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)")** |
| evidence | JSON | not null default `[]`；元素 `{"seq": 1, "kind": "asset"\|"knowledge", "ref": "<asset_id|chunk_id>", "title": ..., "snippet": ...}`，seq 即证据编号 [E1]… |
| evidence_sufficient | Boolean | nullable（复用 agent reflection.sufficient） |
| answer_summary | Text | agent 原始答案（含 [E n] 标注） |
| agent_iterations | Integer | — |
| degraded | Boolean | default False；degraded_reason String(255) nullable（timeout / llm_unavailable / insufficient_evidence） |
| error | Text | nullable |
| latency_ms | Float | — |
| trace_id | String(64) | 关联现有 `state.traces`（内存链路）与 `/api/v1/trace/{trace_id}` |
| started_at / finished_at | DateTime | — |

### 2.6 默认管理员引导策略（不得硬编码弱口令）

`init_platform_db()` 在 `create_all` 后执行，幂等（带 `threading.Lock` + 已种子标志）：
1. 无 roles 行则插入 admin/operator/viewer 三行；
2. 无任何 user 时：`PLATFORM_ADMIN_PASSWORD` 环境变量非空则用它创建 `admin`；为空则用 `secrets.token_urlsafe(12)` 生成随机密码，以 `logger.warning` 一次性打印到控制台（`print` 会被 uvicorn 吞进缓冲，logger.warning 走 lastResort 可见）。绝不内置 "admin123" 之类默认值。

---

## 3. API 端点清单

权限列含义：`公开`=无需令牌；`viewer+`/`operator+`/`admin`=登录且角色达标（admin 恒通过）。所有写端点经 `deps.require_roles(...)` 依赖，审计见第 4 节。

### 3.1 auth（src/api/platform/auth.py）

| 方法 路径 | 权限 | 要点 |
|---|---|---|
| POST /api/v1/auth/login | 公开 | `{username,password}` → `{access_token, token_type:"bearer", expires_in, user{username,role}}`；失败 401；写审计（成功/失败都写，失败审计 user_id 为空、username 记尝试值）；更新 last_login_at |
| GET /api/v1/auth/me | 登录 | 当前用户档案（含 role.code） |
| POST /api/v1/auth/change-password | 登录 | `{old_password,new_password}`；改密后旧 token 仍有效（无状态 JWT 的已知取舍，Phase 2 引 jti 黑名单） |
| GET /api/v1/auth/registration-status | 公开 | `{allow_registration, auto_active}`（Q2 裁决 2026-09-13）；供登录/注册页预检，**前端契约 fail-open**：仅明确 200 且 allow=false 才隐藏注册入口，请求失败一律照常显示 |
| POST /api/v1/auth/register | 公开 + 开关 | `PLATFORM_ALLOW_REGISTRATION=False` → 403；用户名重复 409。新账号一律 viewer 角色，`is_active=PLATFORM_REGISTRATION_AUTO_ACTIVE`（默认 False → pending 待管理员启用）；成败均写审计 `user.register`；email 暂不落库（users 表无该列） |

### 3.2 assets（src/api/platform/assets.py）

| 方法 路径 | 权限 | 要点 |
|---|---|---|
| GET /api/v1/assets/business-lines | viewer+ | 分页列表（含 owner 概要、环境/资产计数） |
| POST / PUT /api/v1/assets/business-lines[/{id}] | admin | 唯一性冲突 409 |
| DELETE /api/v1/assets/business-lines/{id} | admin | 存在下级环境或资产 → 409，错误信息列出依赖 |
| GET / POST / PUT / DELETE /api/v1/assets/environments… | GET viewer+ / 写 admin | 同上，删除校验资产引用 |
| GET /api/v1/assets | viewer+ | 过滤：business_line_id、environment_id、asset_type、status、keyword（name/identifier LIKE）；`?page=&page_size=`，返回 `Page[AssetOut]` |
| GET /api/v1/assets/{id} | viewer+ | 含 environment/business_line 展开与负责人 |
| POST / PUT /api/v1/assets[/{id}] | operator+ | 标识三元组唯一校验 409 |
| DELETE /api/v1/assets/{id} | admin | 物理删除；存在关联事件 → 409（建议先置 decommissioned） |

### 3.3 events（src/api/platform/events.py）

| 方法 路径 | 权限 | 要点 |
|---|---|---|
| GET /api/v1/events | viewer+ | 过滤 status/severity/asset_id/business_line_id/source/时间区间/keyword；按 created_at 倒序分页 |
| GET /api/v1/events/{id} | viewer+ | 明细 + timeline（升序）+ latest_diagnosis 概要 |
| POST /api/v1/events | operator+ | 手工建事件（可关联 asset_id）；source="manual" |
| POST /api/v1/events/webhook/{source} | 公开 + 校验 `X-Webhook-Token` | token 不符 401；token 来自 settings.WEBHOOK_TOKENS（`source:token` 逗号分隔）。响应 202 `{event_id, event_no, matched_rules, auto_diagnosis_queued}`；写审计 actor_type="system" |
| POST /api/v1/events/{id}/acknowledge | operator+ | open→acknowledged，记 acknowledged_by/at + 时间线 |
| POST /api/v1/events/{id}/resolve | operator+ | `{resolution_note}`；diagnosing/acknowledged→resolved |
| POST /api/v1/events/{id}/close | operator+ | 按 2.4 状态矩阵；closed 为终态，再操作 409 |
| POST /api/v1/events/{id}/comments | operator+（viewer 只读） | 追加 comment 时间线 |
| POST /api/v1/events/{id}/diagnose | operator+ | 202 `{diagnosis_id, status:"running"}`；见第 5 节 |
| GET /api/v1/events/{id}/diagnose/stream | operator+ | SSE，观察窗口（5 节） |
| GET /api/v1/events/{id}/diagnoses | viewer+ | 该事件研判记录列表 |
| GET /api/v1/diagnoses/{id} | viewer+ | 单条研判（前端轮询用） |

状态机合法性矩阵（`services/events.py` 常量，非法流转 409）：
`open → acknowledged|closed`；`acknowledged → diagnosing|resolved|closed`；`diagnosing → acknowledged|resolved|closed`；`resolved → closed`；`closed` 终态。

### 3.4 admin（src/api/platform/admin.py）

| 方法 路径 | 权限 | 要点 |
|---|---|---|
| GET / POST /api/v1/admin/users，PUT /api/v1/admin/users/{id}（role/is_active/display_name） | admin | 建用户密码必填；**禁止停用自己或最后一个 admin**（400） |
| POST /api/v1/admin/users/{id}/reset-password | admin | 返回一次性新密码（响应中） |
| GET /api/v1/admin/roles | 登录 | 只读，供前端下拉 |
| GET/POST/PUT/DELETE /api/v1/admin/alert-rules[/{id}] | GET viewer+ / 写 admin | 写后失效 rules 缓存 |
| GET /api/v1/admin/audit-logs | admin | 过滤 action/username/resource_type/时间区间，倒序分页 |
| GET /api/v1/admin/system/health | admin | DB 连通、表行数、平台版本 |

**与现有路由的共存方式**：`routes.py` 的 `app = FastAPI(...)` 之后追加一行 `app.include_router(platform_router)`。现有 `router`（prefix="/api/v1"）及其行为完全不动；OpenAPI 断言 `expected <= set(paths)` 是子集检查，新增路径不影响 269 个存量测试。

---

## 4. RBAC 机制

### 4.1 JWT 发放与校验

- `security.py`：`create_access_token(user) -> (token, expires_in)`。claims：`sub=str(user_id)`、`username`、`role=role.code`、`iat`、`exp`、`jti=uuid4().hex`（预留 Phase 2 撤销）。HS256，密钥与有效期见第 6 节。
- `decode_token(token) -> TokenPayload`：过期/签名错抛 401（`detail="token expired"`/`"invalid token"`），不泄露原因细节给前端展示层。
- `deps.py`：
  - `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")`；
  - `get_current_user(token=Depends(oauth2_scheme), db=Depends(get_db)) -> User`：解码后**回库校验** user 存在且 `is_active`（role 变更/停用即时生效，不信任 token 里的 role 做判定——token 内 role 仅用于日志）；
  - `require_roles(*allowed) -> Callable`：依赖工厂，`user.role.code not in allowed and role != "admin"` 时 403。路由写作：`Depends(require_roles("operator"))`。

### 4.2 角色矩阵（代码内常量 `ROLE_MATRIX`，roles 表只存显示名）

| 能力 | admin | operator | viewer |
|---|---|---|---|
| 登录 / me / 改密 / 评论查看 | Y | Y | Y |
| CMDB 读（业务线/环境/资产/事件/研判读） | Y | Y | Y |
| 资产写、事件创建/ack/resolve/close/评论 | Y | Y | N |
| 触发 AI 研判、webhook 之外的告警登记 | Y | Y | N |
| 业务线/环境写、资产删除、用户/角色管理、告警规则管理 | Y | N | N |
| 审计日志查询、系统健康 | Y | N | N |
| webhook 告警入口 | 不走 RBAC，走 X-Webhook-Token | | |

### 4.3 审计写入点（两条通道，保证与业务同事务）

1. **service 层同事务审计（主通道）**：`audit.write_audit(db, actor, action, resource_type, resource_id, detail, ip)` 在 service 函数内、与业务变更同一 `db.commit()` 提交。事件状态流转、研判触发/完成、登录成败都走此通道——审计与状态永不脱节（这正是复盘时间线与审计可互证的前提）。
2. **路由装饰器（简单 CRUD 辅助）**：`@audited(action="asset.create", resource_type="asset")` 包装路由函数，成功返回后从响应/路径参数取 resource_id 补写一条（用独立短事务）。仅用于无状态机语义的 CMDB 写操作。

请求上下文（`request.client.host`、操作者 User）通过 FastAPI 依赖注入传入，不在全局中间件里做（避免触碰现有路由）。

---

## 5. 事件 → AI 研判闭环

### 5.1 触发方式与数据流

```
外部系统 ──X-Webhook-Token──▶ POST /events/webhook/{source}
                                 │ 1. 鉴 token；2. rules.evaluate(title,source,payload)
                                 ▼
                        命中规则? ──是──▶ severity 取规则值 + timeline(rule_matched)
                                 │        否则默认 severity=min(告警自带, major)
                                 ▼
                        events(status=open) + timeline(created/webhook)
                                 │ rule.auto_diagnose=True → 入内存队列
人工按钮 ──POST /events/{id}/diagnose──┐
                                       ▼
                            asyncio.Queue（单 worker 协程，串行防打爆 LLM）
                                       ▼
                            diagnosis_service.run(event_id, diagnosis_id)
                             1. events → diagnosing + timeline(diagnosis_started)
                             2. 装配确定性证据（kind="asset"，来自 DB，不过模型）
                             3. await asyncio.wait_for(run_agent(query, graph), TIMEOUT)
                             4. 置信度/证据约束 → 落 diagnosis_records
                             5. events → acknowledged + timeline(diagnosis_finished)
```

- **query 组装**（`diagnosis_service.build_query`）：把资产（名称/标识/类型）、环境、业务线、负责人、告警标题、payload 摘要、最近时间线拼成确定性问题文本——这就是“确定性候选”：事实部分全部来自 DB，不由模型生成。
- **知识证据**：`run_agent` 返回的 `retrieved_docs`（含 chunk_id/filename/score）映射为 `kind="knowledge"` 证据并编号；agent 图内已含 `check_hallucination` 幻觉校验与重生成，研判层不重复实现，只消费结果。
- **模型整理**：`answer` 即模型整理后的根因/建议叙述；研判层再按分隔符/JSON 结构抽取 `root_cause`/`suggestion`（提示词层面要求模型输出三段式），抽取失败则原文入 `answer_summary` 并置 `degraded_reason="unparsed"`。

### 5.2 置信度上限约束（“证据编号 + 置信度上限”）

| 条件 | 处理 |
|---|---|
| 存在 ≥1 条 knowledge 证据 | 模型给的 confidence 可信，但 **clamp 到 ≤0.9** |
| 仅有 asset 证据、无 knowledge 命中 | confidence 强制 ≤0.5，`degraded_reason="insufficient_evidence"` |
| 模型未给 confidence 或解析失败 | null（前端显示“未知”，不得臆造） |

答案文本中的引用统一改写为 `[E1][E2]` 形式，E 序号与 `evidence` 数组 seq 一一对应。

### 5.3 超时降级与状态回退

- `asyncio.TimeoutError` → `status="timeout"`、`degraded=True`、已收集的部分证据照常落库、error 写明；事件从 diagnosing **回退到 acknowledged**（避免事件卡死在中间态），时间线记录“研判超时，已回退”。
- agent 图构建失败/`state.ready=False` → 202 受理后研判立即 failed（`llm_unavailable`），不抛 500 给前端按钮。
- **resolved 永远由人工触发**：AI 只产出根因/建议/证据，不自动处置。闭环 = 告警进来 → AI 给可验证的结论 → 人确认后 resolve。

### 5.4 与 SSE 的关系

- **落库与 SSE 解耦**：`POST /events/{id}/diagnose` 创建 `diagnosis_records(status=running)` 并把任务交给后台 worker，研判结果一定落库，与是否有观察者无关。
- `GET /events/{id}/diagnose/stream` 是纯观察窗口：worker 每完成一个节点向 `asyncio.Queue` 广播（diagnosis_id → 订阅者列表注册表），SSE 端点转发，事件名复用现有 `/diagnose` 契约（`decompose/retrieve/reflect/generate/hallucination/done/error`），前端组件可复用现有 SSE 解析逻辑。断开只影响观察，不影响研判。
- 非流式路径：前端也可只轮询 `GET /diagnoses/{id}`（默认推荐，实现最简单，SSE 作为增强）。
- **节点事件来源（Q10 落地 2026-09-13）**：`run_agent` 新增 `on_node` 回调（LangGraph `astream(stream_mode="updates")` 逐节点推送，语义缓存行为不变）；观察者注册表 `_notify_observers` 改为**完成时才清空订阅**（此前首推即清空，后续节点事件会丢）。
- `GET /events/{id}/diagnoses` 返回 `{items,total}`（`started_at` 倒序），前端历史研判列表以此回填，不再解析时间线。

### 5.5 触发研判的状态流（Q11 裁决 2026-09-13）

研判期间事件**真实进入 diagnosing**，由路由层助手 `_prepare_event_for_diagnosis`（`src/api/platform/events.py`）统一执行，状态机矩阵本身不变：

| 事件当前状态 | 行为 |
|---|---|
| `closed` | 409（终态不可研判） |
| `resolved` | 409（已恢复，无需研判） |
| `open` | 系统自动认领（open→acknowledged，时间线/审计同事务留痕，note="触发研判，系统自动认领"）→ acknowledged→diagnosing |
| `acknowledged` | 直接流转 diagnosing |
| `diagnosing` | 保持（允许并发追加研判记录，不重复流转） |

- `transition_event` 新增 `actor_type` 参数（user/system/agent），自动认领路径以系统身份留痕。
- 研判完成/失败/超时的回退目标仍是 `acknowledged`（见 5.3）；`resolved` 永远人工触发。

---

## 6. 配置增量（src/settings.py）

```python
# 平台持久化（用户决策 2026-09-12：DB 文件放项目外 D 盘）
PLATFORM_DB: str = os.environ.get("PLATFORM_DB", r"D:\xingzhi-platform\platform.db")
# JWT：绝不硬编码密钥。三段策略：
#   1) .env/环境变量提供 JWT_SECRET（长度 >= 32，启动时校验，过短直接拒绝启动）；
#   2) 未提供时每次进程启动用 secrets.token_urlsafe(48) 生成临时密钥 + logger.warning
#      （开发可用，代价是重启后所有 token 失效——这是显式的安全默认值，而非弱默认值）；
#   3) 注释中写明生产部署必须提供 .env 里的 JWT_SECRET。
JWT_SECRET: str = ""
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRE_MINUTES: int = 720          # 12 小时
PLATFORM_ADMIN_PASSWORD: str = ""      # 引导期管理员密码；为空则随机生成并打印
WEBHOOK_TOKENS: str = ""               # "zabbix:token1,grafana:token2" → db.py 解析为 dict
DIAGNOSIS_TIMEOUT_SECONDS: int = 120
AUTO_DIAGNOSIS_ENABLED: bool = True
# 自助注册（Q2 裁决 2026-09-13：默认开放，新账号 viewer 待管理员启用）
PLATFORM_ALLOW_REGISTRATION: bool = True
PLATFORM_REGISTRATION_AUTO_ACTIVE: bool = False
# 底层模型（2026-09-13 用户裁决：免费文本档 glm-4.7-flash；实测平台容量繁忙时
# 429/1305「访问量过大」秒拒，研判自动降级、恢复即用；备选 glm-4-flash（200/0.8s），
# 切换=改 .env 一行）
GLM_MODEL_NAME: str = "glm-4.7-flash"
```

注意点：
- `settings.py` 的模块文档声明“零磁盘约束：仅读取配置，不写入任何文件”——**目录创建（`D:\xingzhi-platform` 的 `mkdir(parents=True, exist_ok=True)`）放在 `src/platform/db.py` 的惰性初始化里，不放 settings.py**。
- 复用现有 `get_settings()` 的 `lru_cache` 单例；测试里 monkeypatch 环境变量后必须 `get_settings.cache_clear()`（test_zero_disk.py 已有先例）。
- `SettingsConfigDict(extra="ignore")` 不变，新增字段对存量部署零影响。

---

## 7. 测试计划

平台测试独立放在 `tests/platform/`（目录级 conftest，**不新建全局 tests/conftest.py**，避免影响 269 个存量测试的收集与夹具）。全部沿用 `python -B -m pytest -p no:cacheprovider -q tests/platform/...` 约定。

`tests/platform/conftest.py` 核心夹具：
- `db_session`：`tmp_path / "platform.db"`；monkeypatch `PLATFORM_DB` + `get_settings.cache_clear()`；构造独立 engine，`dependency_overrides[get_db]` 指向测试 session factory——**测试永不触碰 D:\xingzhi-platform 真实路径**；
- `seeded`：跑 `init_platform_db(seed=True)` 得到 admin/operator/viewer 三个账号（密码固定测试值）；
- `client(seeded)`：`TestClient(app)`（lifespan 触发无妨：平台引擎已被 override，现有组件由既有 `_init_components` mock 替身接管）；
- `fake_agent`：monkeypatch `src/platform/services/diagnosis` 的 `run_agent` 入口为 async 假函数（返回固定 answer/sources/confidence），沿用 test_api.py 的 `mock.patch.object(routes, "_init_components", ...)` 同款替身思路。

新增测试文件与用例要点：

| 文件 | 用例要点 |
|---|---|
| test_platform_db.py | 惰性初始化幂等（两次 init 不重复建表/种子）；无 admin 密码环境变量时生成随机口令并只创建一次 |
| test_platform_auth.py | 登录成功/密码错 401/停用用户 401；`/auth/me` 带与不带 token；过期 token（mock `JWT_EXPIRE_MINUTES=0` 或伪造 iat）401；改密后新密码可登录 |
| test_platform_rbac.py | **参数化权限矩阵**：`@pytest.mark.parametrize("role,method,path,expect")` 覆盖 “端点 × 角色”（如 viewer POST /assets → 403；operator POST /business-lines → 403；admin → 200/201）；无 token 全部 401 |
| test_platform_assets.py | CMDB CRUD 正反例；标识三元组重复 409；删除有关联资产的业务线/环境 409；分页与 keyword 过滤 |
| test_platform_events.py | 手工建事件；webhook token 正确/缺失/错误；**状态机矩阵**（每条合法流转 + 每条非法流转 409）；每次流转产生对应 timeline 条目；event_no 当日自增唯一 |
| test_platform_rules.py | contains/eq/regex 命中与不命中；enabled=False 不参与；auto_diagnose 命中后 fake worker 被调用；规则增删后缓存失效 |
| test_platform_diagnosis.py | mock agent：成功 → 记录 completed、evidence 编号连续、knowledge 证据来自 sources、confidence clamp ≤0.9；无知识证据 → confidence ≤0.5 且 degraded_reason=insufficient_evidence；`wait_for` 超时 → timeout + 事件回 acknowledged + 时间线；agent 抛异常 → failed 落库不抛 500；事件状态随研判 diagnosing→acknowledged 联动 |
| test_platform_audit.py | 写操作（建资产/状态流转/登录失败）各产生一条审计；actor 为空时 username 记录正确；audit-logs 过滤参数生效 |
| test_platform_sse.py | 研判 SSE 事件名契约与现有 /diagnose 一致；断开连接不中断后台落库（fake worker 慢速推进 + 客户端提前断开 + 轮询 GET /diagnoses/{id} 最终 completed） |

---

## 8. 实施顺序与风险

### 8.1 实施步骤（每步独立可验证，每步跑全量测试）

1. **依赖与配置**：requirements.txt 追加 sqlalchemy/pyjwt/bcrypt；settings.py 加第 6 节字段（纯增量）。验证：269 测试全绿。
2. **db.py + models/**：惰性 `get_engine`/`get_db`、`create_all`、roles+admin 种子。验证：tests/platform/test_platform_db.py。
3. **security.py + auth 路由 + deps.py + audit.py**，并把 `platform_router`（此时仅含 auth）挂进 routes.py。验证：auth 测试 + 全量测试（确认 include 不破坏 OpenAPI 子集断言与现有端点）。
4. **RBAC 矩阵落地**：require_roles 工厂 + admin/users + admin/audit-logs。验证：rbac/audit 测试。
5. **CMDB**：assets 路由 + schemas。验证：test_platform_assets.py。
6. **事件中心**：services/events.py 状态机 + timeline + events 路由 + webhook + services/rules.py + alert-rules。验证：events/rules 测试。
7. **研判闭环**：services/diagnosis.py + diagnose 端点 + 后台 worker + SSE 观察端点。验证：diagnosis/sse 测试（全 mock LLM，不依赖真实凭据）。
8. **收尾**：扩展 test_api.py 的 AST 禁写扫描到 `src/api/platform/*.py`；更新文档（本项目有 docs/ 与 docstring 传统）。

### 8.2 风险与存量兼容性注意点

| 风险 | 应对 |
|---|---|
| 现有 `with TestClient(routes.app)` 触发 lifespan，若平台 DB 初始化放 lifespan，269 个测试会向 D:\xingzhi-platform 写文件 | **引擎惰性初始化**：只在 `get_db` 依赖首次被调用时建引擎/建表/种子；现有测试不请求平台端点，零副作用。平台端点在 DB 不可用时统一 503（沿用 `_require`/ready 模式语义） |
| test_api.py 对 routes.py 做 AST 禁写扫描（`open`/`write`/`pickle` 等黑名单） | include_router 一行不触发；但平台新文件不在扫描范围，第 8 步显式扩展扫描目标，防平台代码绕过零落盘审查（SQLite 落盘属用户已批准的例外，扫描应白名单 `platform.db` 路径） |
| 现有端点无鉴权（/diagnose、/upload 仍匿名） | Phase 1 显式决策保持不动（前端 rag.ts 未带 token，改动会破坏 13 个视图）；在 /admin/system/health 与文档中标注该边界，Phase 2 再评估全站鉴权与 token 注入 |
| SQLite 单写者与研判后台任务并发写 | 短事务 + `PRAGMA busy_timeout=5000` + WAL（connect 事件回调设置）；worker 串行（单协程消费队列）天然降低写冲突 |
| passlib/bcrypt 兼容坑、python-jose 停滞 | 直接用 bcrypt 与 PyJWT（第 0 节已述），不引入 passlib |
| JWT 无状态导致改密/停用不即时踢人 | `get_current_user` 每请求回库校验 is_active；改密不撤销旧 token 记为已知取舍，Phase 2 引 jti 黑名单表（模型已预留 jti） |
| 自动研判打爆 LLM（规则风暴） | 单 worker 串行队列 + `AUTO_DIAGNOSIS_ENABLED` 总开关 + 语义缓存（agent 内 TTLCache）兜底；Phase 2 可视需要加每资产冷却窗口 |
| `get_settings` 的 lru_cache 在测试与生产混用环境变量 | 平台 conftest 统一 cache_clear；文档写明该约定（settings 模块 docstring 已有此模式的先例说明） |
| Windows 路径与目录不存在 | db.py 惰性初始化时 `Path(PLATFORM_DB).parent.mkdir(parents=True, exist_ok=True)`，失败时平台端点 503 并日志告警，不影响 RAG 主链路 |

关键文件路径（本次核实依据）：
- `D:\Agentic-Rag\agentic-rag-ops\src\api\routes.py`（1114 行，现有路由与 lifespan/OpenAPI 断言）
- `D:\Agentic-Rag\agentic-rag-ops\src\api\jobs.py`（内存任务注册表，研判 worker 可参考其协作式取消语义）
- `D:\Agentic-Rag\agentic-rag-ops\src\agent\state_machine.py`（`run_agent`/`initial_state`/astream 契约，研判层复用入口）
- `D:\Agentic-Rag\agentic-rag-ops\src\settings.py`（配置单例与零磁盘声明）
- `D:\Agentic-Rag\agentic-rag-ops\tests\test_api.py`（TestClient + `_init_components` mock 模式、AST 禁写扫描、OpenAPI 子集断言）
---

## 附注：存储后端变更（2026-09-12 用户决策，优先级高于本文档 SQLite 方案）

用户本地已有 MySQL 8.0.36（HeidiSQL 管理），Phase 1 存储改为 **MySQL 优先 + SQLite 兜底**：

- `src/platform/db.py` 构造连接串顺序：
  1. `.env` 提供 `MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB`（本机已配：127.0.0.1:3306, root, 库名 `fish`，utf8mb4_unicode_ci）→ 用 `mysql+pymysql://...`；
  2. 未提供 MySQL 配置或连接失败 → 回退 `sqlite:///D:\xingzhi-platform\platform.db`（WAL + busy_timeout），日志告警但不阻断启动。
- 依赖追加 `pymysql`（已装）与 `cryptography`（MySQL 8 caching_sha2_password 认证需要）；SQLite 兜底无需额外驱动。
- 连接池：MySQL 用 `pool_pre_ping=True` + `pool_recycle=3600`（防空闲断连）；其余模型/迁移/审计设计不变。
- `.env` 已写入连接信息（gitignore 覆盖）；本地 root/弱口令仅限开发环境，生产部署必须换专用账号。
