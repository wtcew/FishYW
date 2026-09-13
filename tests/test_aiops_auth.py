"""AIOps 接口默认鉴权行为验证（2026-09-13 安全加固回归）。

在**独立子进程**中导入应用（不设 ``RAG_REQUIRE_AUTH``，即走默认"开启鉴权"），
断言：/health 不在鉴权之内（桌面启动器探活依赖），其余 RAG 端点匿名 401。

关于 /health 的期望值：本探针不进入 TestClient 上下文（不执行 lifespan），
故 ``app.state.rag`` 未被初始化，/health 可能返回 500（取决于实现细节）；
关键不变量是**它绝不是 401**——即未被鉴权拦截。完整启动后的 200 由
运行期探活与其它测试覆盖。

之所以用子进程：``tests/conftest.py`` 为兼容存量测试把 ``RAG_REQUIRE_AUTH``
置为 false，而该开关在 ``src.api.routes`` **导入期**生效，进程内无法回退。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import sys
sys.path.insert(0, r"{root}")
from fastapi.testclient import TestClient
from src.api.routes import app

client = TestClient(app)  # 不进入 with：不触发 lifespan

def probe(name, fn):
    try:
        print("RESULT", name, fn().status_code)
    except Exception as exc:  # noqa: BLE001 - /health 在无 lifespan 时可能内部异常
        print("RESULT", name, "EXC", type(exc).__name__)

probe("health", lambda: client.get("/api/v1/health"))
probe("retrieve", lambda: client.post("/api/v1/retrieve", json={{"query": "probe", "top_k": 3}}))
probe("knowledge", lambda: client.get("/api/v1/knowledge/documents"))
probe("upload", lambda: client.post("/api/v1/upload", files={{"files": ("a.txt", b"x", "text/plain")}}))
probe("evaluate", lambda: client.post("/api/v1/evaluate"))
probe("trace", lambda: client.get("/api/v1/trace/whatever"))
probe("diagnose", lambda: client.post("/api/v1/diagnose", json={{"query": "probe"}}))
"""


def test_aiops_endpoints_require_auth_by_default() -> None:
    """默认（未设 RAG_REQUIRE_AUTH）时：/health 不经鉴权，其余 AIOps 端点 401。"""
    env = os.environ.copy()
    env.pop("RAG_REQUIRE_AUTH", None)  # 确保走默认值（开启鉴权）
    env["TMP"] = r"D:\build_tmp"
    env["TEMP"] = r"D:\build_tmp"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE.format(root=str(PROJECT_ROOT))],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        cwd=str(PROJECT_ROOT),
    )
    out = proc.stdout
    tail = (proc.stdout + proc.stderr)[-600:]

    assert "RESULT health 401" not in out, f"/health 必须匿名可达（启动器探活依赖），实际被鉴权拦截\n{tail}"
    for name in ("retrieve", "knowledge", "upload", "evaluate", "trace", "diagnose"):
        assert f"RESULT {name} 401" in out, f"{name} 应要求登录（401）\n{tail}"
