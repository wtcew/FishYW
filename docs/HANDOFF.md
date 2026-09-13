# FishCloud 项目交接文档（新会话必读）

> 最后更新：2026-09-13。Phase 1 已完成并提交（基线 `f8a2ef0`），Q 裁决大部分落地。
> 阅读顺序：本文档 → `docs/platform-backend-design.md` / `docs/platform-frontend-design.md`（已回写 Q 裁决）→ `docs/fishcloud-design.md`（视觉）→ `docs/platform-wireframe-lowfi.html`（线框审阅稿，Q1-Q14 登记表）。
> **最新待办以记忆 `next-session-todos.md` 为准（每次收盘同步）。**

## 一、项目定位与品牌

- **FishCloud**：面向真实运维现场的智能运维平台（可审计、可确认、可执行的工作流：资产登记 / 事件中心 / AI 研判 / RBAC / 审计）。铁律：**实际可用、无演示假数据、无后端/无模型时明确报错**；运维方向以**软件项目/软件工程**为核心（用户 2026-09-12 明确，勿偏硬件）。
- 技术栈：FastAPI + SQLAlchemy 2.x（MySQL 优先/SQLite 兜底）+ Vue 3.5 + TS + Pinia + Router4 + Vite；设计体系 tokens/prototype/layout/platform 四层 CSS。
- 品牌：鱼字印章 + FishCloud + favicon.svg；三套配色主题（blue-pink 默认 / teal / zhu 朱砂）+ 明暗正交。

## 二、关键路径

| 路径 | 用途 |
| --- | --- |
| `D:\Agentic-Rag\agentic-rag-ops` | 项目根（git；基线提交 `f8a2ef0`，此后 Q 裁决改动未提交） |
| `D:\hf-cache` | 模型权重 4.3GB（bge-m3 + bge-reranker-v2-m3） |
| `D:\rag-data` | 知识库快照；`D:\ai-cache\{torch,matplotlib,xdg,ms-playwright}` 应用级缓存 |
| `D:\fishcloud-data\platform.db` | SQLite 兜底库（至今零触碰，测试全部 tmp_path） |
| `D:\build_tmp` | 一切命令的 TMP/TEMP；联调脚本 `fishcloud_e2e.py`（33 项）在此 |
| `release/免安装版/` | 便携打包产物（App/python 1.33GB + `启动FishCloud.bat` + `.env.example`） |
| `docs/` | HANDOFF + 前后端设计文档（已回写 Q 裁决）+ 视觉设计 + 线框图 |

**环境**：Python 3.12 @ `D:\Python\Python312`（全 D 盘）、Node v24（npm 缓存 `D:\典籍\.cache\npm`）、MySQL 8.0.36（本机 3306，库 `fish`）。

## 三、协作与子代理

- 主会话（ZCode+GLM）亲自后端/文档/整合；前端改造与测试同步派 Frontend/Tester 子代理（今日已多次成功；**出现过一次 "Model request failed" 瞬态失败，重试派发即可**）。
- 代理派发要点：任务书必须含"只改某目录"边界 + C 盘 TMP/TEMP 约束 + 交付验收命令；并行代理按目录隔离（frontend / tests / docs 互不重叠）。

## 四、参考网址

| 网址 | 说明 |
| --- | --- |
| https://api-docs.deepseek.com/zh-cn | DeepSeek V4.1 Flash：正式 API ID=`deepseek-flash`，旧名 deepseek-chat 已于 2026-07-24 停用 |

## 五、踩坑清单（重复踩代价高，改码前过一遍）

完整版在记忆 `testing-pitfalls-and-fixes.md`。最高频几条：

1. **平台库死锁**：`db.py` 的 `_init_lock` 必须 `RLock`（曾因非重入锁自死锁，首个请求永久挂起）。
2. **分页注解**：列表端点返回类型必须 `Page[dict[...]]`；写成 `Page[list[dict]]` 会在**有数据时 500**（空库恰好通过，极隐蔽）。
3. **HF 缓存劫持**：`SENTENCE_TRANSFORMERS_HOME` 必须指向 `$HF_HOME/hub`（指向独立空目录会让离线加载失败、检索器 503）。
4. **GLM thinking 透传**：`ChatOpenAI(model_kwargs={"thinking":...})` 会让所有 LLM 节点报 `unexpected keyword argument 'thinking'`——GLM 思考默认开启，不要传。
5. **研判三段式解析**：`根因：/建议：/置信度：` 三个中文标记必须在 prompt 里显式约束，否则字段解析为 None 且 degraded=False 掩盖。
6. **注册预检 fail-open**：前端预检 registration-status 失败时必须照常显示注册入口（fail-closed 曾导致注册入口消失，被用户打回）。
7. uvicorn "Started server process" ≠ 就绪，判据是 `Uvicorn running on ...`；TestClient 下 BackgroundTasks 在 post 返回前已执行（无法经 HTTP 观察研判中间态）。
8. Git Bash curl 中文 JSON 报错 → 用 httpx；robocopy 放 powershell -Command；pytest 一律 `python -B -m pytest -p no:cacheprovider`；尾部 resource_tracker RLock 噪音无害。

## 六、Phase 1 状态（✅ 基线 f8a2ef0）

- 后端：auth（含注册）/资产 CMDB/事件状态机/AI 研判（证据钳制）/告警规则/审计/系统健康，MySQL 自动建表 10 张。
- 测试：tests/platform 4776 行（554 用例），全量曾 702 passed；真实 MySQL 联调 33/33（脚本 `D:\build_tmp\fishcloud_e2e.py`）。
- 账号（2026-09-13 决策）：默认管理员 `admin`（口令固定，见 §九 配置表）；测试用普通账号与观察员账号在本地 `.env`/数据库中维护，**凭据不写入任何文档与代码**。管理员隔离：侧栏条目级门禁 + RBAC 后端 403 + 管理页五 Tab。
- 前端：深色侧栏（8 组导航）+ 登录页改版 + 注册页 + 使用指南(/help) + 三主题 + 渐变体系 + AdminView 五 Tab + 去演示化（全部 build/type-check 双过）。
- 桌面免安装版：`release/免安装版/` 交付（冒烟通过；GUI 双击验收待用户）。

## 七、Q 裁决落地（2026-09-13，详见线框图登记表）

Q1 深色侧栏✅ / Q2 注册默认开✅（`GET /auth/registration-status` + fail-open 预检）/ Q10 `GET /events/{id}/diagnoses` + SSE 观察端点✅（run_agent on_node 回调、观察者完成时清空）/ Q11 触发研判状态流✅（closed/resolved 409；open 自动认领→diagnosing；完成回退 acknowledged；`_prepare_event_for_diagnosis`）/ Q13 五 Tab✅ / 模型选择器✅（免费双模型切换+自定义，GLM 预设内置 baseURL）。

**底层模型**：用户裁决切 `glm-4.7-flash`（免费文本档）。**实测 429/1305「访问量过大」（平台容量，秒拒非 key 问题）**；繁忙期研判自动降级，恢复即用；备选 `glm-4-flash`（200/0.8s），切换=改 .env 一行。

**剩余待办（快照，权威版在记忆 next-session-todos.md）**：①测试同步+全量回归（Tester 代理进行中：conftest 假签名、register 默认开、Q10/Q11 用例）；②渐变配色第二轮迭代（用户反馈"太单一"→模块色族扩大到 KPI 卡/标题/表头/按钮/徽章/侧栏激活态，admin 深蓝→紫隔离；Frontend 代理进行中）；③页面巡查系统（实跑截图全状态+本地走查页：反馈框/三标记/本地保存/MD+JSON 导出/轮次回归）；④HANDOFF/记忆同步（本文档即产物）；⑤git 提交；⑥glm-4.7-flash 恢复后跑真实研判。

## 八、桌面打包（✅ 已交付）

`release/免安装版/`：`App/`（便携 python + 156 包 + core/src/frontend_dist/desktop/.env.example）+ `启动FishCloud.bat`（自动注入 TMP/HF/缓存环境变量）。旧 PyInstaller 路线已弃（spec 已删）。分发新机：模型不随包（首启联网下载），需 WebView2 Runtime；首次使用复制 `.env.example`→`.env` 填凭据。

## 九、配置（agentic-rag-ops/.env，gitignore 覆盖，禁止提交）

| 键 | 状态 |
| --- | --- |
| MYSQL_HOST/PORT/USER/PASSWORD/DB | ✅ 本机 fish 库 |
| JWT_SECRET（43 字符）/ PLATFORM_ADMIN_PASSWORD / WEBHOOK_TOKENS（zabbix:…） | ✅ 已配 |
| GLM_API_KEY（免费档）/ GLM_MODEL_NAME=glm-4.7-flash | ✅；DEEPSEEK_API_KEY 可选（配了优先） |
| PLATFORM_ALLOW_REGISTRATION=true / AUTO_ACTIVE=false | ✅（Q2 裁决） |
| DIAGNOSIS_TIMEOUT_SECONDS=240 | ✅（免费档余量） |

## 十、测试与运行速查

```bash
cd /d/Agentic-Rag/agentic-rag-ops
# 全量测试（零 C 盘）
TMP="D:\build_tmp" TEMP="D:\build_tmp" python -B -m pytest -p no:cacheprovider -q tests/
# 后端（~55s 就绪，判据 "Uvicorn running"；8000 起服前先 netstat 查占用）
TMP="D:\build_tmp" TEMP="D:\build_tmp" python -m uvicorn src.api.routes:app --host 127.0.0.1 --port 8000
# 端到端联调（33 项；凭据从 .env 读）
TMP="D:\build_tmp" TEMP="D:\build_tmp" python D:\build_tmp\fishcloud_e2e.py
# 前端
cd frontend && TMP="D:\build_tmp" TEMP="D:\build_tmp" npm run build && npx vue-tsc --noEmit
```

**C 盘硬约束**：所有产出与依赖落 D 盘；每条命令带 `TMP/TEMP=D:\build_tmp`；增量超 4GB 或余量 <3GB 立停禀报。C 盘现存大块均非本项目不可删：`Temp\DiagOutputDir`（远程桌面追踪，被锁）、`Temp\q1y54xlo`（VS 安装器，被锁）。