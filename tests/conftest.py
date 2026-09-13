"""tests/ 根级夹具。

AIOps 接口鉴权兼容（2026-09-13 安全加固）：RAG 链路（diagnose/query/upload/
retrieve/evaluate/trace/feedback/knowledge/*）现默认要求登录。本仓库存量测试
（Phase 1 之前编写）未携带认证头，故在此显式关闭该开关——**这是测试便利，
不代表生产行为**；生产默认开启（见 ``src/api/routes.py`` 的 ``RAG_REQUIRE_AUTH``）。

默认行为（开启鉴权）的回归由 ``tests/test_aiops_auth.py`` 以独立子进程验证，
不受本文件影响。

注意：必须在模块顶层设置——pytest 会在导入任何测试模块之前先导入 conftest，
而 ``src.api.routes`` 是在导入期读取该环境变量并决定是否挂鉴权依赖的。
"""

import os

os.environ.setdefault("RAG_REQUIRE_AUTH", "false")
