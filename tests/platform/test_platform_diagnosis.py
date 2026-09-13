"""研判闭环测试：证据钳制、置信度上限、超时/失败回退与状态联动。

覆盖后端设计文档 §5 与 §7 的 evaluation 要点：全部走真实 SQLAlchemy + tmp SQLite，
agent 侧用假替身（绝不加载真实模型/凭据）。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_diagnosis.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import AuditLog, DiagnosisRecord, Event, EventTimelineEntry
from src.platform.services import events as event_service


def _event_row(
    session_factory: sessionmaker[Session], *, asset_id: int | None, status: str = "diagnosing"
) -> int:
    """落一条指定状态的事件。"""
    with session_factory() as session:
        event = Event(
            event_no=f"EV-20260101-{uuid4().hex[:4].upper()}",
            title="磁盘告警",
            source="manual",
            severity="major",
            status=status,
            asset_id=asset_id,
        )
        session.add(event)
        session.commit()
        return event.id


def _record_row(session_factory: sessionmaker[Session], event_id: int) -> int:
    """落一条 running 态研判记录。"""
    with session_factory() as session:
        record = DiagnosisRecord(
            event_id=event_id, status="running", trigger_type="manual", query=""
        )
        session.add(record)
        session.commit()
        return record.id


@pytest.fixture()
def diag_case(session_factory: sessionmaker[Session], resources: dict[str, Any]):
    """造 (事件, 研判记录) 对，返回 (event_id, diagnosis_id)。"""

    def _make(*, with_asset: bool = True, status: str = "diagnosing") -> tuple[int, int]:
        event_id = _event_row(
            session_factory,
            asset_id=resources["asset_id"] if with_asset else None,
            status=status,
        )
        return event_id, _record_row(session_factory, event_id)

    return _make


@pytest.fixture()
def run_now(session_factory: sessionmaker[Session]):
    """在独立事件循环里跑研判（服务层直调，绕过 HTTP）。"""

    def _run(event_id: int, diagnosis_id: int, holder: Any) -> None:
        with session_factory() as session:
            event = session.get(Event, event_id)
            record = session.get(DiagnosisRecord, diagnosis_id)
            asyncio.run(event_service.run_diagnosis(session_factory, holder, record, event))

    return _run


@pytest.fixture()
def holder(fake_agent: Any) -> Any:
    """假 AppState（agent 图替身）。"""
    return fake_agent


def _read(session_factory: sessionmaker[Session], diagnosis_id: int) -> DiagnosisRecord:
    with session_factory() as session:
        record = session.get(DiagnosisRecord, diagnosis_id)
        assert record is not None
        session.expunge(record)
        return record


def _read_event(session_factory: sessionmaker[Session], event_id: int) -> Event:
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        session.expunge(event)
        return event


def _fake_agent_returning(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    """把 run_agent 换成返回指定状态的替身（签名跟随真实 run_agent）。"""

    async def _fake(
        _query: str, _graph: Any, history: Any = None, on_node: Any = None
    ) -> dict[str, Any]:
        return state

    monkeypatch.setattr("src.agent.state_machine.run_agent", _fake)


# ═══════════ 成功路径 ═══════════


def test_success_marks_record_completed(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """研判成功 → status=completed。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).status == "completed"


def test_success_stores_answer_and_iterations(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """落库模型答案与迭代次数（前端展示依据）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert record.answer_summary.startswith("根因") and record.agent_iterations == 2


def test_success_records_trace_id(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """记录 trace_id（可跳转现有链路明细）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).trace_id == "trace-fake-1"


def test_success_records_latency(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """记录耗时（latency_ms 非空，SLA 与性能回归依据）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).latency_ms is not None


def test_success_is_not_degraded(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """有知识证据的成功路径不标降级。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (record.degraded, record.degraded_reason) == (False, None)


def test_success_sets_finished_at(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """落 finished_at（研判耗时与趋势统计）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).finished_at is not None


# ═══════════ 证据装配 ═══════════


def test_evidence_starts_with_asset_when_linked(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """有资产时第一条证据是 asset（来自平台 DB，不经模型）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).evidence[0]["kind"] == "asset"


def test_evidence_seq_is_continuous(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """证据编号 seq 从 1 连续递增（答案里的 [E n] 引用依赖它）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    evidence = _read(session_factory, diagnosis_id).evidence
    assert [item["seq"] for item in evidence] == list(range(1, len(evidence) + 1))


def test_evidence_maps_knowledge_from_sources(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """检索来源映射为 knowledge 证据（ref=chunk_id、title=filename）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    knowledge = [
        item for item in _read(session_factory, diagnosis_id).evidence if item["kind"] == "knowledge"
    ]
    assert (knowledge[0]["ref"], knowledge[0]["title"]) == ("chunk-1", "ops.md")


def test_evidence_snippet_is_truncated(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """证据片段截断到 200 字符（防单条证据撑爆响应）。"""
    _fake_agent_returning(
        monkeypatch,
        {
            "answer": "根因：x",
            "sources": [{"chunk_id": "c", "filename": "f.md", "content": "长" * 500}],
            "reflection": {"sufficient": True},
            "iterations": 1,
        },
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    knowledge = [
        item for item in _read(session_factory, diagnosis_id).evidence if item["kind"] == "knowledge"
    ]
    assert len(knowledge[0]["snippet"]) == 200


def test_no_asset_means_no_asset_evidence(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """无资产时不出 asset 证据（不臆造上下文）。"""
    event_id, diagnosis_id = diag_case(with_asset=False)
    run_now(event_id, diagnosis_id, holder)
    kinds = {item["kind"] for item in _read(session_factory, diagnosis_id).evidence}
    assert kinds == {"knowledge"}


def test_evidence_sufficient_follows_reflection(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """reflection.sufficient 透传到 evidence_sufficient。"""
    _fake_agent_returning(
        monkeypatch,
        {"answer": "根因：x", "sources": [], "reflection": {"sufficient": False}, "iterations": 1},
    )
    event_id, diagnosis_id = diag_case(with_asset=False)
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).evidence_sufficient is False


# ═══════════ 置信度钳制 ═══════════


def test_confidence_is_clamped_to_point_nine_with_knowledge(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 knowledge 证据时 confidence 上限 0.9（模型给 0.95 也要压下来）。"""
    _fake_agent_returning(
        monkeypatch,
        {
            "answer": "根因：磁盘满\n建议：扩容\n置信度 0.95",
            "sources": [{"chunk_id": "c1", "filename": "ops.md", "content": "手册"}],
            "reflection": {"sufficient": True},
            "iterations": 1,
        },
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).confidence == pytest.approx(0.9)


def test_confidence_below_cap_is_kept(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型置信度低于上限时原样保留。"""
    _fake_agent_returning(
        monkeypatch,
        {
            "answer": "根因：x\n置信度 0.62",
            "sources": [{"chunk_id": "c1", "filename": "f.md", "content": "x"}],
            "reflection": {"sufficient": True},
            "iterations": 1,
        },
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).confidence == pytest.approx(0.62)


def test_only_asset_evidence_caps_confidence_at_half(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """仅资产证据（无知识命中）时 confidence 强制 ≤0.5。"""
    _fake_agent_returning(
        monkeypatch,
        {"answer": "根因：x\n置信度 0.9", "sources": [], "reflection": {"sufficient": True}, "iterations": 1},
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).confidence == pytest.approx(0.5)


def test_only_asset_evidence_marks_insufficient(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """仅资产证据 → degraded_reason=insufficient_evidence 且 degraded=True。"""
    _fake_agent_returning(
        monkeypatch,
        {"answer": "根因：x\n置信度 0.9", "sources": [], "reflection": {"sufficient": True}, "iterations": 1},
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (record.degraded, record.degraded_reason) == (True, "insufficient_evidence")


def test_missing_confidence_stays_null(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型未给置信度 → NULL（前端显示"未知"，不得臆造）。"""
    _fake_agent_returning(
        monkeypatch,
        {
            "answer": "根因：x",
            "sources": [{"chunk_id": "c1", "filename": "f.md", "content": "x"}],
            "reflection": {"sufficient": True},
            "iterations": 1,
        },
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read(session_factory, diagnosis_id).confidence is None


def test_missing_confidence_without_knowledge_marks_insufficient(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """无知识证据且未给置信度 → NULL + insufficient_evidence。"""
    _fake_agent_returning(
        monkeypatch,
        {"answer": "根因：x", "sources": [], "reflection": {"sufficient": False}, "iterations": 1},
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (record.confidence, record.degraded_reason) == (None, "insufficient_evidence")


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("置信度 0.8", 0.8),
        ("置信度：0.75", 0.75),
        ("置信度=1.0", 1.0),
        ("confidence 0.5", None),
        ("置信度 2.5", None),
    ],
)
def test_extract_confidence_patterns(answer: str, expected: float | None) -> None:
    """置信度抽取模式（'置信度' 前缀 + 0/1 开头数值）。"""
    assert event_service._extract_confidence(answer) == expected


@pytest.mark.parametrize(
    "evidence,confidence,expected",
    [
        ([{"kind": "knowledge"}], 0.95, (0.9, None)),
        ([{"kind": "knowledge"}], None, (None, None)),
        ([{"kind": "asset"}], 0.8, (0.5, "insufficient_evidence")),
        ([{"kind": "asset"}], None, (None, "insufficient_evidence")),
        ([], 0.7, (0.5, "insufficient_evidence")),
        ([], None, (None, "insufficient_evidence")),
    ],
)
def test_clamp_confidence_matrix(
    evidence: list[dict[str, Any]], confidence: float | None, expected: tuple[Any, Any]
) -> None:
    """证据充分性 → 置信度上限矩阵（"证据编号 + 置信度上限"约束）。"""
    assert event_service._clamp_confidence(confidence, evidence) == expected


# ═══════════ 根因/建议抽取 ═══════════


def test_extract_sections_pulls_root_cause_and_suggestion() -> None:
    """三段式答案抽取根因与建议（同行冒号写法）。"""
    root, suggestion = event_service._extract_sections("分析如下\n根因：磁盘写满\n建议：清理日志并扩容")
    assert (root, suggestion) == ("磁盘写满", "清理日志并扩容")


def test_extract_sections_supports_newline_form() -> None:
    """三段式答案抽取兼容「标记换行后跟内容」的写法。"""
    root, suggestion = event_service._extract_sections("根因\n磁盘写满\n建议\n扩容")
    assert (root, suggestion) == ("磁盘写满", "扩容")


def test_extract_sections_truncates_long_content() -> None:
    """抽取结果截断到 1000 字符（防超长答案撑爆字段）。"""
    root, _ = event_service._extract_sections("根因：" + "长" * 1200)
    assert len(root or "") == 1000


def test_extract_sections_returns_none_without_markers() -> None:
    """无标记时返回 (None, None)（由 answer_summary 兜底）。"""
    assert event_service._extract_sections("这是一段自由叙述") == (None, None)


# ═══════════ query 装配（确定性事实全来自 DB） ═══════════


def test_build_query_includes_asset_facts(
    client: TestClient, session_factory: sessionmaker[Session], resources: dict[str, Any]
) -> None:
    """query 含资产名/类型/标识（事实部分确定性来自平台库）。"""
    event_id = _event_row(session_factory, asset_id=resources["asset_id"])
    with session_factory() as session:
        event = session.get(Event, event_id)
        asset = session.get(type(session.get(Event, event_id)).asset.property.mapper.class_, resources["asset_id"])
        query = event_service.build_query(event, asset, {"k": "v"})
    assert "web-01" in query and "10.1.1.1" in query


def test_build_query_asks_for_evidence_markers(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """query 明确要求标注 [E n]（答案引用编号契约）。"""
    event_id = _event_row(session_factory, asset_id=None)
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert "[E n]" in event_service.build_query(event, None, None)


def test_build_query_demands_three_line_output_format(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """query 必须显式约束三行输出格式（根因/建议/置信度标记）。

    Note:
        解析层（``_extract_sections``/``_extract_confidence``）以这三个中文标记为
        契约；prompt 不写死格式时实测模型返回自由文本，根因与置信度双双落成 None
        而 degraded 仍为 False（2026-09-12 真实踩坑，故在此固化）。
    """
    event_id = _event_row(session_factory, asset_id=None)
    with session_factory() as session:
        event = session.get(Event, event_id)
        query = event_service.build_query(event, None, None)
    assert ("根因：" in query, "建议：" in query, "置信度：" in query) == (True, True, True)


def test_free_text_answer_lands_as_none_fields_without_crash(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型返回自由文本（无标记）→ 根因/建议/置信度落 None，且不抛错。

    这是"解析失败必须显式暴露"的回归：字段为空是事实，但流程不得崩。
    """
    _fake_agent_returning(
        monkeypatch,
        {
            "answer": "看起来像是磁盘写满了，建议清理一下日志文件。",
            "sources": [{"chunk_id": "c1", "filename": "ops.md", "content": "手册"}],
            "reflection": {"sufficient": True},
            "iterations": 1,
        },
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (record.status, record.root_cause, record.confidence) == ("completed", None, None)


def test_free_text_answer_is_flagged_unparsed(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """自由文本答案必须标 degraded_reason=unparsed（不得以 degraded=False 掩盖解析失败）。"""
    _fake_agent_returning(
        monkeypatch,
        {
            "answer": "看起来像是磁盘写满了。",
            "sources": [{"chunk_id": "c1", "filename": "ops.md", "content": "手册"}],
            "reflection": {"sufficient": True},
            "iterations": 1,
        },
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (record.degraded, record.degraded_reason) == (True, "unparsed")


def test_marked_answer_lands_all_three_fields(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型按三行格式回答 → 根因/建议/置信度全部正确落库（解析契约正面用例）。"""
    _fake_agent_returning(
        monkeypatch,
        {
            "answer": "根因：日志分区写满\n建议：清理 /var/log 并扩容\n置信度：0.83",
            "sources": [{"chunk_id": "c1", "filename": "ops.md", "content": "手册"}],
            "reflection": {"sufficient": True},
            "iterations": 1,
        },
    )
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (
        record.root_cause, record.suggestion, record.confidence,
    ) == ("日志分区写满", "清理 /var/log 并扩容", pytest.approx(0.83))


def test_record_event_asset_returns_none_without_asset(
    session_factory: sessionmaker[Session]
) -> None:
    """无资产事件返回 None（不误挂资产）。"""
    event_id = _event_row(session_factory, asset_id=None)
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event_service.record_event_asset(session_factory, event) is None


def test_record_event_asset_loads_linked_asset(
    session_factory: sessionmaker[Session], resources: dict[str, Any], client: TestClient
) -> None:
    """有资产事件返回资产实体（供 query 装配）。"""
    event_id = _event_row(session_factory, asset_id=resources["asset_id"])
    with session_factory() as session:
        event = session.get(Event, event_id)
        asset = event_service.record_event_asset(session_factory, event)
    assert asset is not None and asset.id == resources["asset_id"]


# ═══════════ 超时 / 失败回退 ═══════════


def test_timeout_marks_status_timeout(
    diag_case,
    run_now,
    holder,
    session_factory: sessionmaker[Session],
    platform_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时 → status=timeout、degraded_reason=timeout。"""
    monkeypatch.setattr(platform_settings, "DIAGNOSIS_TIMEOUT_SECONDS", 0.01)
    _fake_agent_returning(monkeypatch, {})  # 覆盖为慢速版本见下
    import asyncio as _asyncio

    async def _slow(
        _query: str, _graph: Any, history: Any = None, on_node: Any = None
    ) -> dict[str, Any]:
        await _asyncio.sleep(0.5)
        return {}

    monkeypatch.setattr("src.agent.state_machine.run_agent", _slow)
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (record.status, record.degraded_reason) == ("timeout", "timeout")


def test_timeout_falls_event_back_to_acknowledged(
    diag_case,
    run_now,
    holder,
    session_factory: sessionmaker[Session],
    platform_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时把事件从 diagnosing 回退到 acknowledged（避免卡在中间态）。"""
    monkeypatch.setattr(platform_settings, "DIAGNOSIS_TIMEOUT_SECONDS", 0.01)

    async def _slow(
        _query: str, _graph: Any, history: Any = None, on_node: Any = None
    ) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {}

    monkeypatch.setattr("src.agent.state_machine.run_agent", _slow)
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    assert _read_event(session_factory, event_id).status == "acknowledged"


def test_timeout_writes_failure_timeline(
    diag_case,
    run_now,
    holder,
    session_factory: sessionmaker[Session],
    platform_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时写 diagnosis_finished 时间线并说明原因。"""
    monkeypatch.setattr(platform_settings, "DIAGNOSIS_TIMEOUT_SECONDS", 0.01)

    async def _slow(
        _query: str, _graph: Any, history: Any = None, on_node: Any = None
    ) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {}

    monkeypatch.setattr("src.agent.state_machine.run_agent", _slow)
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    with session_factory() as session:
        entry = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="diagnosis_finished")
            .one()
        )
    assert "未完成" in entry.content


def test_missing_graph_marks_failed_llm_unavailable(
    diag_case, run_now, session_factory: sessionmaker[Session]
) -> None:
    """无 agent 图（未就绪）→ failed + llm_unavailable，而不是 500。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, SimpleNamespace(agent_graph=None))
    record = _read(session_factory, diagnosis_id)
    assert (record.status, record.degraded_reason) == ("failed", "llm_unavailable")


def test_missing_graph_falls_event_back(
    diag_case, run_now, session_factory: sessionmaker[Session]
) -> None:
    """图不可用时事件同样回退到 acknowledged（不卡中间态）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, SimpleNamespace(agent_graph=None))
    assert _read_event(session_factory, event_id).status == "acknowledged"


def test_agent_exception_lands_in_record(
    diag_case, run_now, holder, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent 抛异常 → failed 落库并保留错误文本（不抛给前端按钮）。"""
    _fake_agent_returning(monkeypatch, {})
    raise_agent = monkeypatch.setattr

    async def _boom(
        _query: str, _graph: Any, history: Any = None, on_node: Any = None
    ) -> dict[str, Any]:
        raise RuntimeError("llm down")

    raise_agent("src.agent.state_machine.run_agent", _boom)
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, holder)
    record = _read(session_factory, diagnosis_id)
    assert (record.status, "llm down" in (record.error or "")) == ("failed", True)


def test_failed_diagnosis_never_resolves_event(
    diag_case, run_now, session_factory: sessionmaker[Session]
) -> None:
    """失败路径同样不自动 resolve（人工确认才是闭环终点）。"""
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, SimpleNamespace(agent_graph=None))
    event = _read_event(session_factory, event_id)
    assert (event.status, event.resolved_at) == ("acknowledged", None)


def test_acknowledged_event_stays_acknowledged_after_success(
    diag_case, run_now, holder, session_factory: sessionmaker[Session]
) -> None:
    """已认领事件研判成功后仍是 acknowledged（状态机联动不越权）。"""
    event_id, diagnosis_id = diag_case(status="acknowledged")
    run_now(event_id, diagnosis_id, holder)
    assert _read_event(session_factory, event_id).status == "acknowledged"


# ═══════════ HTTP 路径（202 受理 + 轮询完成） ═══════════


def test_trigger_returns_202_running(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """人工触发返回 202 + diagnosis_id + running（前端据此开始轮询）。"""
    event_id = make_event()
    response = client.post(f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"])
    body = response.json()
    assert (response.status_code, body["status"]) == (202, "running")


def test_triggered_diagnosis_completes(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """触发后研判真实跑完（假 agent）并可通过轮询端点取到 completed。"""
    event_id = make_event()
    diagnosis_id = client.post(
        f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"]
    ).json()["diagnosis_id"]
    body = client.get(
        f"/api/v1/events/diagnoses/{diagnosis_id}", headers=headers["viewer"]
    ).json()
    assert body["status"] == "completed"


def test_polled_diagnosis_carries_evidence_and_confidence(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """轮询结果含证据数组与置信度（前端 DiagnosisPanel 的渲染输入）。"""
    event_id = make_event()
    diagnosis_id = client.post(
        f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"]
    ).json()["diagnosis_id"]
    body = client.get(
        f"/api/v1/events/diagnoses/{diagnosis_id}", headers=headers["viewer"]
    ).json()
    assert (len(body["evidence"]) > 0, body["confidence"]) == (True, pytest.approx(0.8))


def test_trigger_writes_audit(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    make_event,
    session_factory: sessionmaker[Session],
) -> None:
    """触发研判写 diagnosis.trigger 审计。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"])
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.action == "diagnosis.trigger", AuditLog.resource_id == str(event_id))
            .one()
        )
    assert row.user_id is not None


def test_trigger_writes_diagnosis_started_timeline(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """触发研判写 diagnosis_started 时间线。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"])
    with session_factory() as session:
        assert session.query(EventTimelineEntry).filter_by(
            event_id=event_id, entry_type="diagnosis_started"
        ).count() == 1


def test_trigger_records_manual_trigger_type(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """人工触发 trigger_type=manual 且记录触发人。"""
    event_id = make_event()
    diagnosis_id = client.post(
        f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"]
    ).json()["diagnosis_id"]
    with session_factory() as session:
        record = session.get(DiagnosisRecord, diagnosis_id)
    assert (record.trigger_type, record.triggered_by_id is not None) == ("manual", True)


def test_trigger_on_missing_event_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """对不存在事件触发研判 404。"""
    assert client.post(
        "/api/v1/events/9999/diagnose", headers=headers["operator"]
    ).status_code == 404


def test_diagnosis_detail_missing_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """研判记录不存在 404。"""
    assert client.get(
        "/api/v1/events/diagnoses/9999", headers=headers["viewer"]
    ).status_code == 404


def test_diagnosis_detail_payload_is_camel_case(
    client: TestClient, headers: dict[str, dict[str, str]], resources: dict[str, Any]
) -> None:
    """研判出参 camelCase（前端 TS 类型一一对应）。"""
    body = client.get(
        f"/api/v1/events/diagnoses/{resources['diagnosis_id']}", headers=headers["viewer"]
    ).json()
    assert set(body) >= {"eventId", "triggerType", "rootCause", "degradedReason", "latencyMs"}


def test_event_detail_exposes_latest_diagnosis_after_trigger(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """事件详情带出最近研判（详情页与列表页数据源一致）。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"])
    body = client.get(f"/api/v1/events/{event_id}", headers=headers["viewer"]).json()
    assert body["latestDiagnosis"]["status"] == "completed"


def test_diagnosis_timeline_records_finish(
    client: TestClient, headers: dict[str, dict[str, str]], make_event, session_factory: sessionmaker[Session]
) -> None:
    """完成后写 diagnosis_finished 时间线（复盘链条闭合）。"""
    event_id = make_event()
    client.post(f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"])
    with session_factory() as session:
        entry = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="diagnosis_finished")
            .one()
        )
    assert entry.actor_type == "agent"


def test_wait_for_timeout_value_is_read_from_settings(
    platform_settings: Any, monkeypatch: pytest.MonkeyPatch, diag_case, run_now, session_factory: sessionmaker[Session]
) -> None:
    """超时阈值可配置（DIAGNOSIS_TIMEOUT_SECONDS 生效，便于现场调参）。"""
    monkeypatch.setattr(platform_settings, "DIAGNOSIS_TIMEOUT_SECONDS", 0.01)

    async def _slow(
        _query: str, _graph: Any, history: Any = None, on_node: Any = None
    ) -> dict[str, Any]:
        await asyncio.sleep(0.4)
        return {}

    monkeypatch.setattr("src.agent.state_machine.run_agent", _slow)
    event_id, diagnosis_id = diag_case()
    run_now(event_id, diagnosis_id, SimpleNamespace(agent_graph=object()))
    record = _read(session_factory, diagnosis_id)
    assert (record.status, "0.01" in (record.error or "")) == ("timeout", True)


# ═══════════ Q11 状态流（研判前的状态准备，2026-09-13 裁决） ═══════════


def _status_change_details(
    session_factory: sessionmaker[Session], event_id: int
) -> list[dict[str, Any]]:
    """按时间顺序取事件的 status_change 时间线 detail（from/to 对）。"""
    with session_factory() as session:
        entries = (
            session.query(EventTimelineEntry)
            .filter_by(event_id=event_id, entry_type="status_change")
            .order_by(EventTimelineEntry.id.asc())
            .all()
        )
        return [dict(entry.detail or {}) for entry in entries]


def _prepare(
    event_id: int,
    session_factory: sessionmaker[Session],
    *,
    user_id: int | None = 1,
    username: str = "operator",
    actor_type: str = "user",
) -> None:
    """直调路由层状态准备助手（TestClient 下 BackgroundTasks 已跑完，
    研判"中间态"只能这样直测——2026-09-13 实测结论）。"""
    from src.api.platform.events import _prepare_event_for_diagnosis

    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        _prepare_event_for_diagnosis(
            session, event, user_id=user_id, username=username, actor_type=actor_type
        )
        session.commit()


def test_prepare_rejects_closed_event(
    diag_case, session_factory: sessionmaker[Session]
) -> None:
    """closed 是终态：研判请求直接 409。"""
    event_id, _ = diag_case(status="closed")
    with pytest.raises(Exception) as excinfo:
        _prepare(event_id, session_factory)
    assert getattr(excinfo.value, "status_code", None) == 409


def test_prepare_rejects_resolved_event(
    diag_case, session_factory: sessionmaker[Session]
) -> None:
    """resolved 无需研判：409（提示已恢复）。"""
    event_id, _ = diag_case(status="resolved")
    with pytest.raises(Exception) as excinfo:
        _prepare(event_id, session_factory)
    assert (getattr(excinfo.value, "status_code", None), "已恢复" in str(excinfo.value)) == (
        409, True,
    )


def test_prepare_open_event_auto_claims_then_enters_diagnosing(
    diag_case, session_factory: sessionmaker[Session]
) -> None:
    """open → 自动认领（acknowledged）→ diagnosing，两条合法流转都留痕。"""
    event_id, _ = diag_case(status="open")
    _prepare(event_id, session_factory, user_id=None, username="alert_rules", actor_type="system")
    with session_factory() as session:
        event = session.get(Event, event_id)
        status = event.status
        acknowledged_by = event.acknowledged_by_id
    assert status == "diagnosing"
    assert _status_change_details(session_factory, event_id) == [
        {"from": "open", "to": "acknowledged"},
        {"from": "acknowledged", "to": "diagnosing"},
    ]
    # 系统自动路径：认领人不落用户 ID（user_id=None），时间线操作者是 system。
    assert acknowledged_by is None


def test_prepare_acknowledged_event_moves_to_diagnosing(
    diag_case, session_factory: sessionmaker[Session]
) -> None:
    """acknowledged → diagnosing（合法路径，单次流转）。"""
    event_id, _ = diag_case(status="acknowledged")
    _prepare(event_id, session_factory)
    with session_factory() as session:
        assert session.get(Event, event_id).status == "diagnosing"
    assert _status_change_details(session_factory, event_id) == [
        {"from": "acknowledged", "to": "diagnosing"}
    ]


def test_prepare_diagnosing_event_stays_diagnosing(
    diag_case, session_factory: sessionmaker[Session]
) -> None:
    """diagnosing → 保持（允许并发追加研判，不重复流转、不加时间线）。"""
    event_id, _ = diag_case(status="diagnosing")
    _prepare(event_id, session_factory)
    with session_factory() as session:
        assert session.get(Event, event_id).status == "diagnosing"
    assert _status_change_details(session_factory, event_id) == []


def test_http_diagnose_rejected_for_closed_event(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """HTTP 层：closed 事件触发研判 409（终态护栏）。"""
    with session_factory() as session:
        event = Event(
            event_no="EV-20260101-CLOSED", title="已关闭", source="manual",
            severity="major", status="closed",
        )
        session.add(event)
        session.commit()
        event_id = event.id
    response = client.post(
        f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"]
    )
    assert response.status_code == 409


def test_http_diagnose_rejected_for_resolved_event(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """HTTP 层：resolved 事件触发研判 409（open 须先认领才能恢复）。"""
    event_id = make_event()
    assert client.post(
        f"/api/v1/events/{event_id}/acknowledge", headers=headers["operator"]
    ).status_code == 200
    assert client.post(
        f"/api/v1/events/{event_id}/resolve", headers=headers["operator"]
    ).status_code == 200
    assert client.post(
        f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"]
    ).status_code == 409


def test_http_diagnose_on_open_event_ends_acknowledged(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    make_event,
    session_factory: sessionmaker[Session],
) -> None:
    """HTTP 层终态：open 事件触发研判后回到 acknowledged（diagnosing 是中间态，
    TestClient 下 BackgroundTasks 在响应前执行完，只能断言最终态）。"""
    event_id = make_event()
    assert client.post(
        f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"]
    ).status_code == 202
    with session_factory() as session:
        assert session.get(Event, event_id).status == "acknowledged"
    assert _status_change_details(session_factory, event_id) == [
        {"from": "open", "to": "acknowledged"},
        {"from": "acknowledged", "to": "diagnosing"},
    ]


# ═══════════ 事件研判列表端点（Q10 裁决，PRD §3.3） ═══════════


def _insert_record_at(
    session_factory: sessionmaker[Session],
    event_id: int,
    *,
    started_at: datetime,
    status: str = "completed",
) -> int:
    """落一条指定开始时间的研判记录（验证排序，规避同刻并列）。"""
    with session_factory() as session:
        record = DiagnosisRecord(
            event_id=event_id, status=status, trigger_type="manual", query="",
            started_at=started_at,
        )
        session.add(record)
        session.commit()
        return record.id


def test_event_diagnoses_returns_items_and_total(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    session_factory: sessionmaker[Session],
) -> None:
    """返回 {items,total} 信封（非分页页码，历史研判面板的数据源）。"""
    event_id = _event_row(session_factory, asset_id=None)
    _insert_record_at(
        session_factory, event_id, started_at=datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
    )
    response = client.get(f"/api/v1/events/{event_id}/diagnoses", headers=headers["viewer"])
    body = response.json()
    assert (response.status_code, set(body)) == (200, {"items", "total"})
    assert (len(body["items"]), body["total"]) == (1, 1)


def test_event_diagnoses_orders_by_started_at_desc(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    session_factory: sessionmaker[Session],
) -> None:
    """started_at 倒序（最新研判排最前，前端默认展示）。"""
    event_id = _event_row(session_factory, asset_id=None)
    oldest = _insert_record_at(
        session_factory, event_id, started_at=datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)
    )
    newest = _insert_record_at(
        session_factory, event_id, started_at=datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    )
    body = client.get(f"/api/v1/events/{event_id}/diagnoses", headers=headers["viewer"]).json()
    assert [item["id"] for item in body["items"]] == [newest, oldest]


def test_event_diagnoses_empty_event_returns_zero_total(
    client: TestClient,
    headers: dict[str, dict[str, str]],
    session_factory: sessionmaker[Session],
) -> None:
    """无研判记录 → items=[]、total=0（不是 404）。"""
    event_id = _event_row(session_factory, asset_id=None)
    body = client.get(f"/api/v1/events/{event_id}/diagnoses", headers=headers["viewer"]).json()
    assert (body["items"], body["total"]) == ([], 0)


def test_event_diagnoses_missing_event_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """事件不存在 404。"""
    assert client.get(
        "/api/v1/events/9999/diagnoses", headers=headers["viewer"]
    ).status_code == 404


def test_event_diagnoses_requires_authentication(client: TestClient) -> None:
    """匿名请求 401（研判记录含运维上下文）。"""
    assert client.get("/api/v1/events/1/diagnoses").status_code == 401


# ═══════════ SSE 观察端点（Q10 裁决，PRD §5.4） ═══════════


def test_sse_stream_without_running_diagnosis_emits_idle_and_done(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """无进行中研判：先推 status=idle 再推 done 后结束（前端据此回落轮询）。"""
    event_id = _event_row(session_factory, asset_id=None)
    response = client.get(
        f"/api/v1/events/{event_id}/diagnose/stream", headers=headers["operator"]
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: status\ndata: {"status": "idle"' in response.text
    assert 'event: done\ndata: {"status": "idle"}' in response.text


def test_sse_stream_after_completed_diagnosis_emits_idle(
    client: TestClient, headers: dict[str, dict[str, str]], make_event
) -> None:
    """研判完成后（无 running 记录）观察窗口报告 idle（注册前已完成 → 复查兜底路径）。"""
    event_id = make_event()
    assert client.post(
        f"/api/v1/events/{event_id}/diagnose", headers=headers["operator"]
    ).status_code == 202
    response = client.get(
        f"/api/v1/events/{event_id}/diagnose/stream", headers=headers["operator"]
    )
    assert 'event: status\ndata: {"status": "idle"' in response.text


def test_sse_stream_missing_event_is_404(
    client: TestClient, headers: dict[str, dict[str, str]]
) -> None:
    """事件不存在 404。"""
    assert client.get(
        "/api/v1/events/9999/diagnose/stream", headers=headers["operator"]
    ).status_code == 404


def test_sse_stream_requires_operator_role(
    client: TestClient, headers: dict[str, dict[str, str]], session_factory: sessionmaker[Session]
) -> None:
    """观察窗口仅 operator 及以上可用（viewer 403）。"""
    event_id = _event_row(session_factory, asset_id=None)
    assert client.get(
        f"/api/v1/events/{event_id}/diagnose/stream", headers=headers["viewer"]
    ).status_code == 403
