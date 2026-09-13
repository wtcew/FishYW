"""CMDB 测试：业务线/环境/资产 CRUD、唯一冲突、分页过滤与列表契约。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_assets.py
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import Asset, AuditLog, BusinessLine, Environment


def _detail(response: Any) -> str:
    """取错误详情文本（断言失败时便于定位）。"""
    return str(response.json().get("detail", ""))


# ═══════════ 业务线 ═══════════


def test_create_business_line_returns_created(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """建业务线 201。"""
    response = client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": "bl-1", "name": "支付线"},
    )
    assert (response.status_code, response.json()["status"]) == (201, "created")


def test_create_business_line_rejects_duplicate_code(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """业务编码重复 409。"""
    client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": "bl-dup", "name": "线一"},
    )
    response = client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": "bl-dup", "name": "线二"},
    )
    assert (response.status_code, _detail(response)) == (409, "业务编码已存在")


def test_create_business_line_rejects_duplicate_name(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """业务名称重复 409。"""
    client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": "bl-a", "name": "同名线"},
    )
    response = client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": "bl-b", "name": "同名线"},
    )
    assert response.status_code == 409


def test_list_business_lines_reports_environment_count(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """业务线列表带环境计数（前端树形展示用）。"""
    body = client.get("/api/v1/assets/business-lines", headers=headers["viewer"]).json()
    target = next(line for line in body if line["id"] == resources["business_line_id"])
    assert target["environmentCount"] == 1


def test_update_business_line_renames(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """业务线可改名（编码保持唯一）。"""
    client.put(
        f"/api/v1/assets/business-lines/{resources['business_line_id']}",
        headers=headers["admin"],
        json={"code": "bl-res", "name": "改名后的线"},
    )
    body = client.get("/api/v1/assets/business-lines", headers=headers["admin"]).json()
    assert next(line for line in body if line["id"] == resources["business_line_id"])["name"] == "改名后的线"


def test_update_missing_business_line_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """更新不存在的业务线 404。"""
    response = client.put(
        "/api/v1/assets/business-lines/9999",
        headers=headers["admin"],
        json={"code": "x", "name": "x"},
    )
    assert response.status_code == 404


def test_delete_business_line_with_environments_is_409(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """存在下级环境时禁止删除，错误信息列出依赖。"""
    response = client.delete(
        f"/api/v1/assets/business-lines/{resources['business_line_id']}", headers=headers["admin"]
    )
    assert (response.status_code, "下级环境" in _detail(response)) == (409, True)


def test_delete_business_line_without_children_succeeds(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """无下级环境时可删除。"""
    line_id = client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": "bl-empty", "name": "空线"},
    ).json()["id"]
    response = client.delete(f"/api/v1/assets/business-lines/{line_id}", headers=headers["admin"])
    assert (response.status_code, response.json()["status"]) == (200, "deleted")


def test_delete_missing_business_line_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """删除不存在的业务线 404。"""
    assert client.delete(
        "/api/v1/assets/business-lines/9999", headers=headers["admin"]
    ).status_code == 404


# ═══════════ 环境 ═══════════


def test_create_environment_returns_created(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """建环境 201。"""
    response = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": resources["business_line_id"], "name": "staging"},
    )
    assert (response.status_code, response.json()["status"]) == (201, "created")


def test_create_environment_rejects_duplicate_name_in_same_line(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """同一业务线内环境名唯一（409）。"""
    response = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": resources["business_line_id"], "name": "prod"},
    )
    assert response.status_code == 409


def test_create_environment_allows_same_name_in_other_line(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """不同业务线可用同名环境。"""
    other = client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": "bl-other", "name": "另一条线"},
    ).json()["id"]
    response = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": other, "name": "prod"},
    )
    assert response.status_code == 201


def test_create_environment_requires_existing_business_line(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """业务线不存在时 404。"""
    response = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": 9999, "name": "prod"},
    )
    assert response.status_code == 404


def test_list_environments_reports_asset_count(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """环境列表带资产计数。"""
    body = client.get("/api/v1/assets/environments", headers=headers["viewer"]).json()
    target = next(env for env in body if env["id"] == resources["environment_id"])
    assert target["assetCount"] == 2


def test_delete_environment_with_assets_is_409(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """存在资产时禁止删除环境。"""
    response = client.delete(
        f"/api/v1/assets/environments/{resources['environment_id']}", headers=headers["admin"]
    )
    assert (response.status_code, "台资产" in _detail(response)) == (409, True)


def test_delete_environment_without_assets_succeeds(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """空环境可删除。"""
    env_id = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": resources["business_line_id"], "name": "to-drop"},
    ).json()["id"]
    response = client.delete(f"/api/v1/assets/environments/{env_id}", headers=headers["admin"])
    assert response.status_code == 200


def test_delete_missing_environment_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """删除不存在的环境 404。"""
    assert client.delete(
        "/api/v1/assets/environments/9999", headers=headers["admin"]
    ).status_code == 404


# ═══════════ 资产写与唯一性 ═══════════


def test_create_asset_returns_created(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """登记资产 201。"""
    response = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "db-01",
            "asset_type": "db",
            "identifier": "mysql-3306",
        },
    )
    assert (response.status_code, response.json()["status"]) == (201, "created")


def test_create_asset_rejects_duplicate_identifier_triple(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """同环境+同类型+同标识 409（唯一三元组）。"""
    response = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "副本",
            "asset_type": "host",
            "identifier": resources["asset_identifier"],
        },
    )
    assert (response.status_code, "已存在" in _detail(response)) == (409, True)


def test_create_asset_allows_same_identifier_with_other_type(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """同环境同标识、不同类型可共存（三元组含类型）。"""
    response = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "同标识不同类型",
            "asset_type": "app",
            "identifier": resources["asset_identifier"],
        },
    )
    assert response.status_code == 201


def test_create_asset_allows_same_identifier_in_other_environment(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """不同环境可复用标识（三元组含环境）。"""
    env_id = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": resources["business_line_id"], "name": "stage-x"},
    ).json()["id"]
    response = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={
            "environment_id": env_id,
            "name": "stage-host",
            "asset_type": "host",
            "identifier": resources["asset_identifier"],
        },
    )
    assert response.status_code == 201


def test_create_asset_requires_existing_environment(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """环境不存在时 404。"""
    response = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={"environment_id": 9999, "name": "x", "asset_type": "host", "identifier": "x"},
    )
    assert response.status_code == 404


def test_create_asset_rejects_unknown_type(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """非法资产类型被 pydantic 拦截（422）。"""
    response = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "x",
            "asset_type": "toaster",
            "identifier": "x-1",
        },
    )
    assert response.status_code == 422


def test_get_asset_expands_lineage(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """资产详情展开环境与业务线名。"""
    body = client.get(
        f"/api/v1/assets/{resources['asset_id']}", headers=headers["viewer"]
    ).json()
    assert (body["environmentName"], body["businessLineName"]) == ("prod", "资源线")


def test_get_missing_asset_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """资产不存在 404。"""
    assert client.get("/api/v1/assets/9999", headers=headers["viewer"]).status_code == 404


def test_update_asset_changes_status(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """更新资产状态（下线替代删除的真实路径）。"""
    client.put(
        f"/api/v1/assets/{resources['asset_id']}",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "web-01",
            "asset_type": "host",
            "identifier": resources["asset_identifier"],
            "status": "maintenance",
        },
    )
    body = client.get(f"/api/v1/assets/{resources['asset_id']}", headers=headers["viewer"]).json()
    assert body["status"] == "maintenance"


def test_update_asset_keeps_own_identifier(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """更新自身不触发唯一冲突（排除自身 id）。"""
    response = client.put(
        f"/api/v1/assets/{resources['asset_id']}",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "web-01-renamed",
            "asset_type": "host",
            "identifier": resources["asset_identifier"],
        },
    )
    assert response.status_code == 200


def test_update_asset_to_conflicting_triple_is_409(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """改成与同环境另一资产相同的三元组 → 409。"""
    response = client.put(
        f"/api/v1/assets/{resources['asset_id']}",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "web-01",
            "asset_type": "host",
            "identifier": "10.1.1.9",
        },
    )
    assert response.status_code == 409


def test_update_missing_asset_is_404(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """更新不存在的资产 404。"""
    response = client.put(
        "/api/v1/assets/9999",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": "x",
            "asset_type": "host",
            "identifier": "x-9",
        },
    )
    assert response.status_code == 404


def test_delete_asset_with_linked_event_is_409(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """有事件引用的资产禁止删除，提示先下线。"""
    response = client.delete(f"/api/v1/assets/{resources['asset_id']}", headers=headers["admin"])
    assert (response.status_code, "decommissioned" in _detail(response)) == (409, True)


def test_delete_asset_without_events_succeeds(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """无事件引用的资产可删除。"""
    response = client.delete(
        f"/api/v1/assets/{resources['deletable_asset_id']}", headers=headers["admin"]
    )
    assert (response.status_code, response.json()["status"]) == (200, "deleted")


def test_deleted_asset_is_gone(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    resources: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    """删除后库内确实不存在（不是软删除的错觉）。"""
    client.delete(f"/api/v1/assets/{resources['deletable_asset_id']}", headers=headers["admin"])
    with session_factory() as session:
        assert session.get(Asset, resources["deletable_asset_id"]) is None


# ═══════════ 列表契约（分页/过滤/扁平对象） ═══════════


def test_asset_list_items_are_flat_objects(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """P0 回归：items 必须是**扁平对象数组**（曾因 Page[list[dict]] 注解全部 500）。"""
    response = client.get("/api/v1/assets", headers=headers["viewer"])
    body = response.json()
    assert (response.status_code, isinstance(body["items"][0], dict)) == (200, True)


def test_asset_list_total_is_int(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """分页字段类型正确（total 为整数、page/page_size 回显）。"""
    body = client.get("/api/v1/assets?page=1&page_size=5", headers=headers["viewer"]).json()
    assert (isinstance(body["total"], int), body["page_size"]) == (True, 5)


def test_asset_list_page_size_limits_items(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """page_size 限制单页条数但 total 仍是全量。"""
    body = client.get("/api/v1/assets?page=1&page_size=1", headers=headers["viewer"]).json()
    assert (len(body["items"]), body["total"]) == (1, 2)


def test_asset_list_page_two_is_disjoint(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """第二页与第一页不重复（分页偏移正确）。"""
    first = client.get("/api/v1/assets?page=1&page_size=1", headers=headers["viewer"]).json()
    second = client.get("/api/v1/assets?page=2&page_size=1", headers=headers["viewer"]).json()
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_asset_list_filters_by_type(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """按类型过滤。"""
    body = client.get("/api/v1/assets?asset_type=db", headers=headers["viewer"]).json()
    assert (body["total"], body["items"]) == (0, [])


def test_asset_list_filters_by_status(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """按状态过滤。"""
    body = client.get("/api/v1/assets?status=maintenance", headers=headers["viewer"]).json()
    assert body["total"] == 0


def test_asset_list_filters_by_keyword_on_identifier(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """keyword 命中标识（name 或 identifier 模糊匹配）。"""
    body = client.get("/api/v1/assets?keyword=10.1.1.9", headers=headers["viewer"]).json()
    assert body["total"] == 1


def test_asset_list_filters_by_business_line(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """按业务线过滤（经环境 join）。"""
    body = client.get(
        f"/api/v1/assets?business_line_id={resources['business_line_id']}", headers=headers["viewer"]
    ).json()
    assert body["total"] == 2


def test_asset_list_filters_by_environment(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """按环境过滤。"""
    body = client.get(
        f"/api/v1/assets?environment_id={resources['environment_id']}", headers=headers["viewer"]
    ).json()
    assert body["total"] == 2


def test_asset_list_rejects_oversized_page_size(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """page_size 上限 100（防一次拉爆库）。"""
    assert client.get("/api/v1/assets?page_size=1000", headers=headers["viewer"]).status_code == 422


def test_asset_list_empty_shape_on_blank_db(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """空库返回空数组而非 500（空列表最容易掩盖注解错误）。"""
    body = client.get("/api/v1/assets", headers=headers["viewer"]).json()
    assert (body["items"], body["total"]) == ([], 0)


# ═══════════ 审计 ═══════════


def test_create_asset_writes_audit(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    resources: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    """资产登记写 asset.create 审计（含操作者与明细）。"""
    uid = uuid4().hex[:6]
    asset_id = client.post(
        "/api/v1/assets",
        headers=headers["operator"],
        json={
            "environment_id": resources["environment_id"],
            "name": f"audit-{uid}",
            "asset_type": "host",
            "identifier": f"host-{uid}",
        },
    ).json()["id"]
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "asset.create", AuditLog.resource_id == str(asset_id))
            .one()
        )
    assert row.username == "operator1"


def test_business_line_create_writes_audit(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """业务线创建写审计。"""
    client.post(
        "/api/v1/assets/business-lines",
        headers=headers["admin"],
        json={"code": f"bl-{uuid4().hex[:6]}", "name": f"线-{uuid4().hex[:6]}"},
    )
    with session_factory() as session:
        assert session.query(AuditLog).filter(
            AuditLog.action == "asset.business_line.create"
        ).count() == 1


def test_environment_create_writes_audit(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    resources: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    """环境创建写审计。"""
    env_id = client.post(
        "/api/v1/assets/environments",
        headers=headers["admin"],
        json={"business_line_id": resources["business_line_id"], "name": f"env-{uuid4().hex[:6]}"},
    ).json()["id"]
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "asset.environment.create",
                AuditLog.resource_id == str(env_id),
            )
            .one()
        )
    assert row.user_id is not None


def test_audit_records_forwarded_ip(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    resources: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    """审计记录来源 IP（取 X-Forwarded-For 首项）。"""
    uid = uuid4().hex[:6]
    client.post(
        "/api/v1/assets",
        headers={**headers["operator"], "X-Forwarded-For": "198.51.100.7, 10.0.0.5"},
        json={
            "environment_id": resources["environment_id"],
            "name": f"ip-{uid}",
            "asset_type": "host",
            "identifier": f"ip-{uid}",
        },
    )
    with session_factory() as session:
        row = session.query(AuditLog).filter(
            AuditLog.resource_id == str(session.query(Asset).filter_by(identifier=f"ip-{uid}").one().id)
        ).one()
    assert row.ip == "198.51.100.7"


def test_cmdb_tables_are_isolated_per_test(
    session_factory: sessionmaker[Session],
) -> None:
    """夹具隔离：本用例看不到其他用例的业务线/环境（tmp_path 逐用例独立）。"""
    with session_factory() as session:
        assert (
            session.query(BusinessLine).count(),
            session.query(Environment).count(),
        ) == (0, 0)
