"""``MemoryGraphStore`` 测试（pytest）。

覆盖用户验收要求的五个用例：

1. **test_add_entities** —— 添加实体和关系后验证 count；
2. **test_entity_merge** —— 相同实体多次添加，验证 source_chunk_ids 合并；
3. **test_search_by_keyword** —— 添加已知图，关键词搜索验证返回正确子图；
4. **test_get_neighbors** —— 验证指定 depth 的邻居返回正确；
5. **test_clear** —— clear 后 count 为 (0, 0)。

另附加边界与契约用例：字段推断、缺字段跳过、大小写匹配、
KeyError、并发读写、零磁盘 AST 静态扫描（禁 nx.write_graphml / pickle / shelve 等）。

运行方式::

    python -m pytest tests/test_graph_store.py -p no:cacheprovider -q
"""

from __future__ import annotations

import ast
import pathlib
import threading

import pytest

from src.knowledge.graph_builder import MemoryGraphStore

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "knowledge" / "graph_builder.py"
)

BANNED_ATTRS = {
    "write", "write_text", "write_bytes", "dump", "dump_index", "write_index",
    "write_graphml", "to_disk", "shelve", "savez", "save",
}
BANNED_NAMES = {"open", "pickle"}


def entity(name: str, kind: str = "device", chunk: str = "c1") -> dict:
    """构造实体记录。"""
    return {"kind": "entity", "name": name, "type": kind, "source_chunk_id": chunk}


def relation(source: str, target: str, rel_type: str = "device_fault", chunk: str = "c1") -> dict:
    """构造关系记录。"""
    return {
        "kind": "relation",
        "source": source,
        "target": target,
        "type": rel_type,
        "source_chunk_id": chunk,
    }


# ═══════════ 验收用例 1：add_entities / count ═══════════


def test_add_entities() -> None:
    """实体与关系写入后 count 应为 (节点数, 边数)，属性正确。"""
    store = MemoryGraphStore()

    store.add_entities([entity("设备A", "device", "c1")])
    assert store.count() == (1, 0)
    assert store.node_types() == {"设备A": "device"}

    store.add_entities([relation("设备A", "频繁重启", "device_fault", "c1")])
    assert store.count() == (2, 1)

    results = store.search_by_keyword("设备A")
    assert "设备A -> 频繁重启 (device_fault)" in results[0]["subgraph_text"]


def test_add_entities_infers_kind_and_skips_invalid() -> None:
    """缺 kind 按字段推断；缺 name 或 source/target 的记录被跳过。"""
    store = MemoryGraphStore()
    store.add_entities(
        [
            {"name": "电源模块", "type": "component", "source_chunk_id": "c9"},
            {"source": "电源模块", "target": "电压不稳", "type": "fault_solution"},
            {"kind": "entity", "type": "device"},
            {"kind": "relation", "source": "A", "type": "device_fault"},
        ]
    )
    assert store.count() == (2, 1)


# ═══════════ 验收用例 2：实体合并 ═══════════


def test_entity_merge() -> None:
    """相同实体多次添加：source_chunk_ids 按首次出现顺序去重合并，节点不重复。"""
    store = MemoryGraphStore()
    store.add_entities([entity("设备A", "device", "c1")])
    store.add_entities(
        [
            entity("设备A", "device", "c2"),
            entity("设备A", "device", "c1"),
            entity("设备A", "device", "c3"),
        ]
    )

    assert store.entity_sources("设备A") == ["c1", "c2", "c3"]
    assert store.count()[0] == 1


def test_entity_merge_isolated_between_entities() -> None:
    """不同实体的来源互不串扰；未登记实体返回空列表。"""
    store = MemoryGraphStore()
    store.add_entities([entity("设备A", "device", "c1"), entity("设备B", "device", "c2")])

    assert store.entity_sources("设备A") == ["c1"]
    assert store.entity_sources("设备B") == ["c2"]
    assert store.entity_sources("不存在") == []


# ═══════════ 验收用例 3：关键词子图检索 ═══════════


def test_search_by_keyword() -> None:
    """已知图上关键词检索：返回中心实体、邻居与完整子图文本。"""
    store = MemoryGraphStore()
    store.add_entities(
        [
            entity("某设备", "device", "c1"),
            relation("某设备", "频繁重启", "device_fault", "c1"),
            relation("某设备", "风扇异常", "device_fault", "c1"),
            relation("电源模块", "某设备", "component_device", "c2"),
            entity("无关实体", "solution", "c3"),
        ]
    )

    results = store.search_by_keyword("设备")

    assert len(results) == 1
    item = results[0]
    assert item["center_entity"] == "某设备"
    assert "频繁重启" in item["neighbors"]
    assert "风扇异常" in item["neighbors"]
    assert "电源模块" in item["neighbors"]
    assert "某设备 -> 频繁重启 (device_fault)" in item["subgraph_text"]
    assert "电源模块 -> 某设备 (component_device)" in item["subgraph_text"]
    assert "无关实体" not in item["subgraph_text"]


def test_search_by_keyword_edge_cases() -> None:
    """大小写不敏感、max_results 截断、无命中返回空、空图返回空。"""
    store = MemoryGraphStore()
    assert store.search_by_keyword("任意") == []

    store.add_entities([entity(f"设备{i}", "device", f"c{i}") for i in range(10)])
    assert len(store.search_by_keyword("设备", max_results=3)) == 3
    assert len(store.search_by_keyword("设备", max_results=99)) == 10

    case_store = MemoryGraphStore()
    case_store.add_entities([entity("Server-X", "device", "c1")])
    assert len(case_store.search_by_keyword("server")) == 1
    assert len(case_store.search_by_keyword("SERVER")) == 1
    assert case_store.search_by_keyword("不存在的实体") == []


# ═══════════ 验收用例 4：get_neighbors ═══════════


def test_get_neighbors() -> None:
    """A→B→C 链上：depth=1 只返回直接邻居，depth=2 扩展两跳。"""
    store = MemoryGraphStore()
    store.add_entities(
        [
            entity("A", "device", "c1"),
            relation("A", "B", "device_fault", "c1"),
            relation("B", "C", "fault_solution", "c1"),
        ]
    )

    assert store.get_neighbors("A", depth=1) == [{"entity": "B", "distance": 1}]

    mapping = {
        item["entity"]: item["distance"] for item in store.get_neighbors("A", depth=2)
    }
    assert mapping == {"B": 1, "C": 2}

    names = [item["entity"] for item in store.get_neighbors("A", depth=2)]
    assert "A" not in names

    with pytest.raises(KeyError):
        store.get_neighbors("不存在")


# ═══════════ 验收用例 5：clear ═══════════


def test_clear() -> None:
    """clear 后 count 为 (0,0)，实体索引、子图检索、类型映射全部清空，且可复用。"""
    store = MemoryGraphStore()
    store.add_entities(
        [entity("设备A", "device", "c1"), relation("设备A", "重启", "device_fault", "c1")]
    )

    store.clear()

    assert store.count() == (0, 0)
    assert store.entity_sources("设备A") == []
    assert store.search_by_keyword("设备") == []
    assert store.node_types() == {}

    store.add_entities([entity("设备B", "device", "c2")])
    assert store.count() == (1, 0)
    assert store.entity_sources("设备B") == ["c2"]


# ═══════════ 线程安全 ═══════════


def test_thread_safety() -> None:
    """10 线程各写 50 组实体+关系：节点 501（500 设备 + 公共尾实体）、边 500。"""
    store = MemoryGraphStore()
    errors: list[BaseException] = []
    barrier = threading.Barrier(10)

    def worker(worker_id: int) -> None:
        try:
            barrier.wait()
            for index in range(50):
                store.add_entities(
                    [
                        entity(f"设备{worker_id}-{index}", "device", f"c{worker_id}-{index}"),
                        relation(
                            f"设备{worker_id}-{index}",
                            "频繁重启",
                            "device_fault",
                            f"c{worker_id}-{index}",
                        ),
                    ]
                )
        except BaseException as exc:  # noqa: BLE001 - 收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.count() == (501, 500)


def test_concurrent_search_during_write() -> None:
    """写入过程中并发检索/统计不报错。"""
    store = MemoryGraphStore()
    store.add_entities([entity("种子", "device", "c0")])
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for index in range(80):
                store.add_entities([entity(f"设备{index}", "device", f"c{index}")])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()

    def reader() -> None:
        try:
            while not stop.is_set():
                store.search_by_keyword("设备", max_results=3)
                store.count()
                store.get_neighbors("种子", depth=1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert store.count()[0] == 81


# ═══════════ 契约 ═══════════


class TestZeroDisk:
    """零磁盘静态扫描：源码禁出现任何落盘调用。"""

    def test_source_has_no_disk_api_calls(self) -> None:
        """AST 扫描禁用 open / write_graphml / pickle / shelve 等。"""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRS:
                found.append(f"attr:{func.attr}")
            if isinstance(func, ast.Name) and func.id in BANNED_NAMES:
                found.append(f"name:{func.id}")
        assert found == []
