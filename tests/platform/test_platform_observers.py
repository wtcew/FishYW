"""研判观察者（SSE 广播内核）测试：注册/注销/通知与"落库与观察解耦"。

背景：SSE 观察端点 ``GET /events/{id}/diagnose/stream`` 已实现（Q10 裁决
按 PRD §5.4 落地，HTTP 层契约见 test_platform_diagnosis.py 末节）。本文件
锁定服务层三个原语（``register_observer`` / ``_notify_observers`` /
``unregister_observer``）的语义，并验证"无观察者时研判照样落库"（解耦保证）。

核心语义（Q10）：节点事件只投递、**不移除订阅**（观察端点要流式接收多个
节点事件）；仅 ``diagnosis_finished`` 才清空该研判的全部订阅。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/platform/test_platform_observers.py
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from src.platform.models import DiagnosisRecord, Event
from src.platform.services import events as event_service


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """每个用例前后清空观察者注册表（模块级全局状态，避免串味）。"""
    event_service._observers.clear()
    yield
    event_service._observers.clear()


def test_register_observer_adds_queue() -> None:
    """注册后该研判 ID 下出现一个订阅队列。"""

    async def _run() -> int:
        await event_service.register_observer(7)
        return len(event_service._observers[7])

    assert asyncio.run(_run()) == 1


def test_register_observer_supports_multiple_subscribers() -> None:
    """同一研判允许多个观察者（多标签页/多客户端）。"""

    async def _run() -> int:
        await event_service.register_observer(7)
        await event_service.register_observer(7)
        return len(event_service._observers[7])

    assert asyncio.run(_run()) == 2


def test_unregister_observer_removes_queue() -> None:
    """注销后注册表不再持有该队列。"""

    async def _run() -> int:
        queue = await event_service.register_observer(7)
        event_service.unregister_observer(7, queue)
        return len(event_service._observers.get(7, []))

    assert asyncio.run(_run()) == 0


def test_unregister_unknown_observer_is_noop() -> None:
    """注销不存在的观察者不抛错（断开连接可能重复触发）。"""

    async def _run() -> None:
        await event_service.register_observer(7)
        event_service.unregister_observer(7, asyncio.Queue())
        event_service.unregister_observer(999, asyncio.Queue())

    asyncio.run(_run())
    assert len(event_service._observers[7]) == 1


def test_notify_delivers_payload_to_observer() -> None:
    """通知把完成载荷投递到订阅队列（SSE 端点的数据源）。"""

    async def _run() -> dict[str, Any]:
        queue = await event_service.register_observer(7)
        await event_service._notify_observers(7, {"type": "diagnosis_finished", "status": "completed"})
        return queue.get_nowait()

    assert asyncio.run(_run())["type"] == "diagnosis_finished"


def test_notify_pops_registry_entry() -> None:
    """通知后注册表条目被清空（避免长期泄漏与重复投递）。"""

    async def _run() -> bool:
        await event_service.register_observer(7)
        await event_service._notify_observers(7, {"type": "diagnosis_finished"})
        return 7 in event_service._observers

    assert asyncio.run(_run()) is False


def test_notify_node_event_keeps_subscription() -> None:
    """节点事件只投递、不移除订阅（Q10：观察端点要连续接收多个节点事件）。"""

    async def _run() -> tuple[Any, bool, Any]:
        queue = await event_service.register_observer(7)
        await event_service._notify_observers(7, {"type": "node", "node": "decompose"})
        still_registered = 7 in event_service._observers
        first = queue.get_nowait()
        await event_service._notify_observers(7, {"type": "node", "node": "retrieve"})
        second = queue.get_nowait()
        return first, still_registered, second

    first, still_registered, second = asyncio.run(_run())
    assert (first["type"], first["node"], still_registered, second["node"]) == (
        "node", "decompose", True, "retrieve",
    )


def test_notify_node_events_are_all_delivered_before_finish() -> None:
    """研判全程的节点事件按序全部可达（不清订阅的直接推论）。"""

    async def _run() -> list[str]:
        queue = await event_service.register_observer(7)
        for node in ("decompose", "retrieve", "reflect", "generate", "hallucination_check"):
            await event_service._notify_observers(7, {"type": "node", "node": node})
        await event_service._notify_observers(7, {"type": "diagnosis_finished", "status": "completed"})
        payloads = [queue.get_nowait() for _ in range(6)]
        return [str(p.get("node") or p.get("type")) for p in payloads]

    assert asyncio.run(_run()) == [
        "decompose", "retrieve", "reflect", "generate", "hallucination_check",
        "diagnosis_finished",
    ]


def test_notify_without_observers_is_noop() -> None:
    """无观察者时通知安全返回（研判不因没人看而失败）。"""
    asyncio.run(event_service._notify_observers(1234, {"type": "diagnosis_finished"}))
    assert 1234 not in event_service._observers


def test_notify_survives_broken_observer() -> None:
    """观察者队列写入失败不影响研判（断开只影响观察）。"""

    class _BrokenQueue(asyncio.Queue):
        async def put(self, item: Any) -> None:  # type: ignore[override]
            raise RuntimeError("client gone")

    async def _run() -> None:
        queue = await event_service.register_observer(7)
        event_service._observers[7] = [_BrokenQueue(), queue]
        await event_service._notify_observers(7, {"type": "diagnosis_finished"})

    asyncio.run(_run())
    assert True


def test_registry_is_scoped_by_diagnosis_id() -> None:
    """不同研判的通知互不干扰。"""

    async def _run() -> tuple[Any, Any]:
        queue_a = await event_service.register_observer(1)
        queue_b = await event_service.register_observer(2)
        await event_service._notify_observers(1, {"type": "diagnosis_finished", "id": 1})
        return queue_a.get_nowait(), queue_b.empty()

    payload, other_empty = asyncio.run(_run())
    assert (payload["id"], other_empty) == (1, True)


# ═══════════ 落库与观察解耦 ═══════════


def test_diagnosis_persists_without_observers(
    session_factory: sessionmaker[Session], resources: dict[str, Any], fake_agent: Any
) -> None:
    """无任何观察者时研判照常跑完并落库（结果与是否有观察者无关）。"""
    with session_factory() as session:
        event = Event(
            event_no="EV-20260101-OBS1", title="无观察者", source="manual",
            severity="major", status="diagnosing", asset_id=resources["asset_id"],
        )
        session.add(event)
        session.flush()
        record = DiagnosisRecord(event_id=event.id, status="running", trigger_type="manual", query="")
        session.add(record)
        session.commit()
        event_id, diagnosis_id = event.id, record.id
        event = session.get(Event, event_id)

    assert event_service._observers == {}
    asyncio.run(event_service.run_diagnosis(session_factory, fake_agent, record, event))

    with session_factory() as session:
        assert session.get(DiagnosisRecord, diagnosis_id).status == "completed"
