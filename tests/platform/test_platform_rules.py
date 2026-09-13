"""告警规则测试：匹配算子、启用开关、缓存语义与 auto_diagnose 自动研判路径。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_rules.py
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import AlertRule, AuditLog, DiagnosisRecord, Event, EventTimelineEntry
from src.platform.services import events as event_service
from tests.platform.constants import WEBHOOK_TOKENS

ZABBIX_TOKEN = WEBHOOK_TOKENS.split(",")[0].split(":", 1)[1]
WEBHOOK_HEADERS = {"X-Webhook-Token": ZABBIX_TOKEN}


def _rule(
    session_factory: sessionmaker[Session],
    *,
    match_field: str = "title",
    match_op: str = "contains",
    match_value: str = "磁盘",
    severity: str = "major",
    enabled: bool = True,
    auto_diagnose: bool = False,
) -> int:
    """直接落一条规则（服务层单测用）。"""
    with session_factory() as session:
        rule = AlertRule(
            name=f"rule-{uuid4().hex[:6]}",
            enabled=enabled,
            match_field=match_field,
            match_op=match_op,
            match_value=match_value,
            severity=severity,
            auto_diagnose=auto_diagnose,
        )
        session.add(rule)
        session.commit()
        return rule.id


def _evaluate(
    session_factory: sessionmaker[Session],
    *,
    title: str = "磁盘告警",
    source: str = "zabbix",
    payload: dict[str, Any] | None = None,
    asset_identifier: str | None = None,
) -> tuple[list[Any], str]:
    """在真实库上跑规则匹配。"""
    with session_factory() as session:
        return event_service.evaluate_rules(
            session, title=title, source=source, payload=payload, asset_identifier=asset_identifier
        )


# ═══════════ 匹配算子 ═══════════


def test_contains_match_is_case_insensitive(session_factory: sessionmaker[Session]) -> None:
    """contains 命中（大小写不敏感）。"""
    _rule(session_factory, match_value="DISK")
    matched, _ = _evaluate(session_factory, title="disk full on web-01")
    assert len(matched) == 1


def test_contains_miss_returns_no_match(session_factory: sessionmaker[Session]) -> None:
    """contains 未命中。"""
    _rule(session_factory, match_value="磁盘")
    matched, _ = _evaluate(session_factory, title="CPU 使用率过高")
    assert matched == []


def test_eq_match_requires_exact_value(session_factory: sessionmaker[Session]) -> None:
    """eq 需要完全相等。"""
    _rule(session_factory, match_op="eq", match_value="磁盘告警")
    matched, _ = _evaluate(session_factory, title="磁盘告警")
    assert len(matched) == 1


def test_eq_does_not_match_substring(session_factory: sessionmaker[Session]) -> None:
    """eq 不做子串匹配。"""
    _rule(session_factory, match_op="eq", match_value="磁盘告警")
    matched, _ = _evaluate(session_factory, title="磁盘告警（生产）")
    assert matched == []


def test_regex_match(session_factory: sessionmaker[Session]) -> None:
    """regex 命中（正则提取）。"""
    _rule(session_factory, match_op="regex", match_value=r"disk\s+usage\s+9\d%")
    matched, _ = _evaluate(session_factory, title="alert: disk usage 95% on db-01")
    assert len(matched) == 1


def test_invalid_regex_is_ignored(session_factory: sessionmaker[Session]) -> None:
    """非法正则不中断摄取（记 warning，规则视为不命中）。"""
    _rule(session_factory, match_op="regex", match_value="([unclosed")
    matched, _ = _evaluate(session_factory, title="磁盘告警")
    assert matched == []


def test_payload_key_match(session_factory: sessionmaker[Session]) -> None:
    """payload_key:<key> 匹配原始载荷字段。"""
    _rule(session_factory, match_field="payload_key:host", match_value="db-01")
    matched, _ = _evaluate(session_factory, payload={"host": "db-01"})
    assert len(matched) == 1


def test_asset_identifier_match(session_factory: sessionmaker[Session]) -> None:
    """asset.identifier 匹配资产标识。"""
    _rule(session_factory, match_field="asset.identifier", match_value="10.9.9.9")
    matched, _ = _evaluate(session_factory, asset_identifier="10.9.9.9")
    assert len(matched) == 1


def test_source_match(session_factory: sessionmaker[Session]) -> None:
    """source 匹配来源名。"""
    _rule(session_factory, match_field="source", match_value="grafana")
    matched, _ = _evaluate(session_factory, source="grafana")
    assert len(matched) == 1


def test_unknown_match_field_never_matches(session_factory: sessionmaker[Session]) -> None:
    """未知匹配字段视为不命中（不抛错）。"""
    _rule(session_factory, match_field="unknown_field", match_value="x")
    matched, _ = _evaluate(session_factory)
    assert matched == []


def test_disabled_rule_is_skipped(session_factory: sessionmaker[Session]) -> None:
    """停用规则不参与匹配。"""
    _rule(session_factory, enabled=False, match_value="磁盘")
    matched, _ = _evaluate(session_factory, title="磁盘告警")
    assert matched == []


def test_multiple_rules_can_match(session_factory: sessionmaker[Session]) -> None:
    """可同时命中多条规则。"""
    _rule(session_factory, match_value="磁盘")
    _rule(session_factory, match_value="告警")
    matched, _ = _evaluate(session_factory, title="磁盘告警")
    assert len(matched) == 2


# ═══════════ 严重度合成 ═══════════


def test_severity_defaults_to_major_without_match(session_factory: sessionmaker[Session]) -> None:
    """无规则命中时建议严重度默认 major。"""
    _, severity = _evaluate(session_factory)
    assert severity == "major"


def test_matched_rule_supplies_severity(session_factory: sessionmaker[Session]) -> None:
    """命中规则的严重度即建议值。"""
    _rule(session_factory, match_value="磁盘", severity="minor")
    _, severity = _evaluate(session_factory, title="磁盘告警")
    assert severity == "minor"


def test_critical_rule_wins_over_later_matches(session_factory: sessionmaker[Session]) -> None:
    """多条命中时 critical 覆盖其他建议值（告警不得被降级）。"""
    _rule(session_factory, match_value="磁盘", severity="info")
    _rule(session_factory, match_value="告警", severity="critical")
    _, severity = _evaluate(session_factory, title="磁盘告警")
    assert severity == "critical"


# ═══════════ 规则管理端点与实时生效 ═══════════


def test_create_alert_rule_appears_in_list(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """新建规则后列表可见。"""
    name = f"规则-{uuid4().hex[:6]}"
    client.post(
        "/api/v1/admin/alert-rules",
        headers=headers["admin"],
        json={
            "name": name,
            "match_field": "title",
            "match_op": "contains",
            "match_value": "内存",
            "severity": "major",
        },
    )
    body = client.get("/api/v1/admin/alert-rules", headers=headers["viewer"]).json()
    assert any(rule["name"] == name for rule in body)


def test_alert_rule_payload_is_camel_case(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """规则出参为 camelCase（与前端 TS 类型一一对应）。"""
    body = client.get("/api/v1/admin/alert-rules", headers=headers["viewer"]).json()
    target = next(rule for rule in body if rule["id"] == resources["alert_rule_id"])
    assert set(target) >= {"matchField", "matchOp", "matchValue", "autoDiagnose"}


def test_update_alert_rule_changes_severity(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """更新规则生效（severity 变化可在列表看到）。"""
    client.put(
        f"/api/v1/admin/alert-rules/{resources['alert_rule_id']}",
        headers=headers["admin"],
        json={
            "name": "夹具规则",
            "match_field": "title",
            "match_op": "contains",
            "match_value": "夹具",
            "severity": "critical",
        },
    )
    body = client.get("/api/v1/admin/alert-rules", headers=headers["viewer"]).json()
    target = next(rule for rule in body if rule["id"] == resources["alert_rule_id"])
    assert target["severity"] == "critical"


def test_delete_alert_rule_removes_it(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """删除规则后列表不再包含它。"""
    client.delete(f"/api/v1/admin/alert-rules/{resources['alert_rule_id']}", headers=headers["admin"])
    body = client.get("/api/v1/admin/alert-rules", headers=headers["viewer"]).json()
    assert all(rule["id"] != resources["alert_rule_id"] for rule in body)


def test_delete_missing_alert_rule_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """删除不存在的规则 404。"""
    assert client.delete(
        "/api/v1/admin/alert-rules/9999", headers=headers["admin"]
    ).status_code == 404


def test_alert_rule_create_writes_audit(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """规则新建写审计。"""
    rule_id = client.post(
        "/api/v1/admin/alert-rules",
        headers=headers["admin"],
        json={
            "name": f"审计规则-{uuid4().hex[:6]}",
            "match_field": "title",
            "match_op": "contains",
            "match_value": "x",
            "severity": "major",
        },
    ).json()["id"]
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "admin.alert_rule.create",
                AuditLog.resource_id == str(rule_id),
            )
            .one()
        )
    assert row.user_id is not None


def test_disabling_rule_takes_effect_immediately(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """停用规则后立即可见地不再命中（设计中的"缓存失效"以直查库实现）。"""
    client.put(
        f"/api/v1/admin/alert-rules/{resources['alert_rule_id']}",
        headers=headers["admin"],
        json={
            "name": "夹具规则",
            "enabled": False,
            "match_field": "title",
            "match_op": "contains",
            "match_value": "夹具",
            "severity": "minor",
        },
    )
    body = client.post(
        "/api/v1/events/webhook/zabbix",
        headers=WEBHOOK_HEADERS,
        json={"title": "夹具规则命中"},
    ).json()
    assert body["matched_rules"] == 0


# ═══════════ webhook × auto_diagnose（自动研判闭环） ═══════════


def _auto_rule(session_factory: sessionmaker[Session], *, match_value: str = "自动") -> int:
    """落一条开启 auto_diagnose 的规则。"""
    return _rule(session_factory, match_value=match_value, severity="critical", auto_diagnose=True)


def test_webhook_queues_auto_diagnosis(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """命中 auto_diagnose 规则时自动排队研判（响应回报）。"""
    _auto_rule(session_factory)
    body = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "自动研判"}
    ).json()
    assert body["auto_diagnosis_queued"] is True


def test_auto_diagnosis_record_is_rule_triggered_and_completed(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """自动研判真实跑完（trigger_type=rule、最终 completed）——闭环可用性的核心证据。"""
    _auto_rule(session_factory)
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "自动闭环"}
    ).json()["event_id"]
    with session_factory() as session:
        record = session.query(DiagnosisRecord).filter_by(event_id=event_id).one()
    assert (record.trigger_type, record.status) == ("rule", "completed")


def test_auto_diagnosis_never_resolves_event(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """自动研判只给结论，绝不自动处置（resolved 永远人工触发）。

    Note:
        事件状态在自动路径后保持 open（webhook 建单态）——设计文档 §5.1 期望
        "events → diagnosing"，但 TRANSITIONS 不允许 open→diagnosing（见交付报告
        的偏差清单），故此处只锁定"绝不被自动 resolve"这一安全不变量。
    """
    _auto_rule(session_factory)
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "自动研判"}
    ).json()["event_id"]
    with session_factory() as session:
        event = session.get(Event, event_id)
    assert (event.status != "resolved", event.resolved_at) == (True, None)


def test_auto_diagnosis_writes_system_timeline(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """自动研判写系统时间线（diagnosis_started，actor=alert_rules）。"""
    _auto_rule(session_factory)
    event_id = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "自动研判"}
    ).json()["event_id"]
    with session_factory() as session:
        entry = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="diagnosis_started")
            .one()
        )
    assert (entry.actor_type, entry.actor_name) == ("system", "alert_rules")


def test_auto_diagnosis_disabled_switch_blocks_queueing(
    client: TestClient,
    platform_settings: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTO_DIAGNOSIS_ENABLED=False 时不排队（防规则风暴打爆 LLM 的总开关）。"""
    _auto_rule(session_factory)
    monkeypatch.setattr(platform_settings, "AUTO_DIAGNOSIS_ENABLED", False)
    body = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "自动研判"}
    ).json()
    assert body["auto_diagnosis_queued"] is False


def test_auto_diagnosis_disabled_switch_creates_no_record(
    client: TestClient,
    platform_settings: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总开关关闭时连研判记录都不落（不留永久 running 的僵尸记录）。"""
    _auto_rule(session_factory)
    monkeypatch.setattr(platform_settings, "AUTO_DIAGNOSIS_ENABLED", False)
    client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "自动研判"}
    )
    with session_factory() as session:
        assert session.query(DiagnosisRecord).count() == 0


def test_webhook_without_auto_rule_queues_nothing(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """无 auto_diagnose 规则时不产生研判记录。"""
    _rule(session_factory, match_value="磁盘")
    client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "磁盘告警"}
    )
    with session_factory() as session:
        assert session.query(DiagnosisRecord).count() == 0


def test_auto_diagnosis_without_app_state_degrades_gracefully(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AppState 不可用时降级为仅落 running 记录（不抛 500、不谎报已排队）。"""
    _auto_rule(session_factory)
    from src.api.platform import events as events_router

    monkeypatch.setattr(events_router, "_resolve_app_state", lambda: None)
    body = client.post(
        "/api/v1/events/webhook/zabbix", headers=WEBHOOK_HEADERS, json={"title": "无 AppState"}
    ).json()
    assert body["auto_diagnosis_queued"] is False
