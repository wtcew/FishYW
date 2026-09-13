"""平台路由契约测试：端点清单、公共端点边界、列表出参形状与分页契约。

这是"实际可用"的守门测试：所有列表端点都经真实路由 + 真实序列化返回，
因此 `Page[...]` 之类的注解错误会在这里立刻暴露（曾导致资产/事件列表全 500）。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_routes.py
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.routes import app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 设计文档 §3 的平台端点（实现侧须全部存在）。
#: 2026-09-13 Q 裁决补齐：registration-status、事件研判列表、SSE 观察端点。
EXPECTED_PLATFORM_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    "/api/v1/auth/change-password",
    "/api/v1/auth/register",
    "/api/v1/auth/registration-status",
    "/api/v1/assets/business-lines",
    "/api/v1/assets/business-lines/{line_id}",
    "/api/v1/assets/environments",
    "/api/v1/assets/environments/{env_id}",
    "/api/v1/assets",
    "/api/v1/assets/{asset_id}",
    "/api/v1/events",
    "/api/v1/events/{event_id}",
    "/api/v1/events/webhook/{source}",
    "/api/v1/events/{event_id}/acknowledge",
    "/api/v1/events/{event_id}/resolve",
    "/api/v1/events/{event_id}/close",
    "/api/v1/events/{event_id}/comments",
    "/api/v1/events/{event_id}/diagnose",
    "/api/v1/events/{event_id}/diagnose/stream",
    "/api/v1/events/{event_id}/diagnoses",
    "/api/v1/events/diagnoses/{diagnosis_id}",
    "/api/v1/admin/users",
    "/api/v1/admin/users/{user_id}",
    "/api/v1/admin/users/{user_id}/reset-password",
    "/api/v1/admin/roles",
    "/api/v1/admin/alert-rules",
    "/api/v1/admin/alert-rules/{rule_id}",
    "/api/v1/admin/audit-logs",
    "/api/v1/admin/system/health",
}


def test_platform_paths_are_registered() -> None:
    """平台端点清单齐备（30 条，含 RAG 端点的 OpenAPI 子集断言不受影响）。"""
    paths = set(app.openapi()["paths"])
    assert EXPECTED_PLATFORM_PATHS <= paths


def test_openapi_has_no_download_link_in_platform_paths() -> None:
    """平台端点不含任何文件下载响应（与 RAG 侧同一零落盘契约）。"""
    schema = app.openapi()
    platform_blob = "".join(
        str(schema["paths"][path]) for path in EXPECTED_PLATFORM_PATHS if path in schema["paths"]
    )
    assert "FileResponse" not in platform_blob


def test_public_endpoints_are_limited() -> None:
    """无需登录即可触达的端点仅限：登录/注册/注册预检/webhook 四类。"""
    client = TestClient(app)  # 不进 with：不触发 lifespan，不加载模型
    assert (
        client.get("/api/v1/auth/me").status_code,
        client.get("/api/v1/auth/registration-status").status_code,
        client.get("/api/v1/assets").status_code,
        client.post("/api/v1/auth/login", json={"username": "x", "password": "y"}).status_code,
        client.post(
            "/api/v1/events/webhook/nosuch", json={"title": "x"}
        ).status_code,
    ) == (401, 200, 401, 401, 401)


def test_sse_observer_endpoint_contract() -> None:
    """SSE 观察端点已注册（Q10 裁决按 PRD §5.4 落地，取代旧"未实现"偏差锁定）。

    Note:
        该端点 2026-09-13 前未实现（旧用例锁定"不存在"并提醒同步）；落地后
        本用例反向锁定"必须存在"，行为契约见 test_platform_diagnosis.py 末节。
    """
    paths = set(app.openapi()["paths"])
    assert "/api/v1/events/{event_id}/diagnose/stream" in paths


def test_event_diagnoses_list_endpoint_contract() -> None:
    """事件研判列表端点已注册（Q10 裁决按 PRD §3.3 补齐）。"""
    paths = set(app.openapi()["paths"])
    assert "/api/v1/events/{event_id}/diagnoses" in paths


def test_diagnosis_detail_lives_under_events_prefix() -> None:
    """研判详情实际路径为 /events/diagnoses/{id}（设计文档写 /diagnoses/{id}）。"""
    paths = set(app.openapi()["paths"])
    assert "/api/v1/events/diagnoses/{diagnosis_id}" in paths


# ═══════════ 列表出参形状（真实序列化） ═══════════

#: 全部返回分页信封的端点；信封字段 2026-09-12 已统一为 snake_case page_size。
ALL_PAGE_ENDPOINTS = [
    "/api/v1/assets",
    "/api/v1/events",
    "/api/v1/admin/audit-logs",
]

ARRAY_ENDPOINTS = [
    "/api/v1/assets/business-lines",
    "/api/v1/assets/environments",
    "/api/v1/admin/roles",
    "/api/v1/admin/users",
    "/api/v1/admin/alert-rules",
]


@pytest.mark.parametrize("path", ALL_PAGE_ENDPOINTS)
def test_paged_endpoints_return_items_array(
    path: str, client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """分页端点的 items 是扁平对象数组（注解写成 list[list[...]] 时会 500）。"""
    response = client.get(path, headers=headers["admin"])
    body = response.json()
    assert (response.status_code, isinstance(body["items"], list), isinstance(body["total"], int)) == (
        200, True, True,
    )


@pytest.mark.parametrize("path", ALL_PAGE_ENDPOINTS)
def test_paged_endpoints_have_snake_case_pagination_fields(
    path: str, client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """分页信封字段统一为 items,total,page,page_size（含审计日志）。

    Note:
        ``src/platform/schemas.py`` 顶部文档仍写着 camelCase ``pageSize``；
        实现（含审计日志端点）已统一为 ``page_size``，文档待同步。
    """
    body = client.get(path, headers=headers["admin"]).json()
    assert set(body) == {"items", "total", "page", "page_size"}


def test_all_paged_endpoints_share_one_envelope_shape(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """所有分页端点信封形状完全一致（防字段名再次分叉）。"""
    envelopes = {
        path: tuple(sorted(client.get(path, headers=headers["admin"]).json()))
        for path in ALL_PAGE_ENDPOINTS
    }
    assert len(set(envelopes.values())) == 1


@pytest.mark.parametrize("path", ARRAY_ENDPOINTS)
def test_array_endpoints_return_plain_arrays(
    path: str, client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """不分页的配置类列表返回纯数组（与前端下拉/树形组件约定一致）。"""
    response = client.get(path, headers=headers["admin"])
    assert (response.status_code, isinstance(response.json(), list)) == (200, True)


@pytest.mark.parametrize("path", ALL_PAGE_ENDPOINTS + ARRAY_ENDPOINTS)
def test_all_list_endpoints_reject_anonymous(
    path: str, client: TestClient
) -> None:
    """所有列表端点对匿名请求 401（不得泄露运维数据）。"""
    assert client.get(path).status_code == 401


def test_system_health_reports_storage_backend(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """系统健康返回存储后端与表行数（现场排查第一入口）。"""
    body = client.get("/api/v1/admin/system/health", headers=headers["admin"]).json()
    assert (body["status"], body["storage"]) == ("ok", "sqlite-fallback")


def test_system_health_reports_row_counts(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """系统健康回报用户/资产/事件行数。"""
    body = client.get("/api/v1/admin/system/health", headers=headers["admin"]).json()
    assert set(body["rows"]) == {"users", "assets", "events"}


# ═══════════ 零磁盘静态扫描（平台源码） ═══════════

#: 真正会写文件的 API（`model_dump` / `json.dumps` 等内存序列化不在其列）。
DISK_WRITE_ATTRS = {
    "write_text", "write_bytes", "NamedTemporaryFile", "mkstemp", "mkdtemp",
    "savetxt", "to_csv", "savez", "to_disk", "shelve", "dump",
}
DISK_WRITE_NAMES = {"open", "pickle"}

PLATFORM_SOURCE_ROOTS = ("src/api/platform", "src/platform")


def _scan_disk_writes(path: pathlib.Path) -> list[str]:
    """AST 扫描单个文件的落盘 API 调用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in DISK_WRITE_ATTRS:
            found.append(f"{path}:attr:{func.attr}")
        if isinstance(func, ast.Name) and func.id in DISK_WRITE_NAMES:
            found.append(f"{path}:name:{func.id}")
    return found


def test_platform_source_has_no_disk_write_apis() -> None:
    """平台源码不得调用文件写入 API（唯一落盘例外是用户批准的 SQLite 库路径）。

    Note:
        平台唯一的落盘行为是通过 SQLAlchemy 建/写用户批准的库文件
        （``PLATFORM_DB``，默认 D 盘，见后端设计文档附注与 tests/test_zero_disk.py
        的口径），代码中不得出现任何直接文件写入调用。
    """
    found: list[str] = []
    for root in PLATFORM_SOURCE_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            found.extend(_scan_disk_writes(path))
    assert found == []


def test_platform_source_has_no_tempfile_import() -> None:
    """平台源码不导入 tempfile（杜绝绕道临时文件的落盘）。"""
    offenders: list[str] = []
    for root in PLATFORM_SOURCE_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "import tempfile" in source:
                offenders.append(str(path))
    assert offenders == []
