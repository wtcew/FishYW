"""``MemoryGraphStore`` 单元测试。

覆盖验收检查点：

1. **import 测试** —— ``networkx`` / ``threading`` 可导入，六个公共方法齐备；
2. **实体合并测试** —— 相同实体多次 add，``source_chunk_ids`` 正确去重合并；
3. **图谱检索** —— 关键词子图、按跳数取邻居、边界与异常；
4. **零磁盘验证** —— AST 扫描源码，确认无 open / write / pickle / dump 等调用。

运行方式::

    python -B -m unittest discover -s tests -t . -p "test_graph_builder.py"
"""

from __future__ import annotations

import ast
import pathlib
import threading
import unittest

from src.knowledge.graph_builder import MemoryGraphStore

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "knowledge" / "graph_builder.py"
)

BANNED_ATTRS = {
    "write",
    "write_text",
    "write_bytes",
    "dump",
    "dump_index",
    "write_index",
    "write_graphml",
    "to_disk",
    "shelve",
    "savez",
    "save",
}
BANNED_NAMES = {"open", "pickle"}


def entity(name: str, kind: str = "device", chunk: str = "c1") -> dict:
    """构造实体记录。

    Args:
        name: 实体名称。
        kind: 实体类型。
        chunk: 来源分块 ID。

    Returns:
        实体记录字典。
    """
    return {
        "kind": "entity",
        "name": name,
        "type": kind,
        "source_chunk_id": chunk,
    }


def relation(source: str, target: str, rel_type: str = "device_fault", chunk: str = "c1") -> dict:
    """构造关系记录。

    Args:
        source: 头实体。
        target: 尾实体。
        rel_type: 关系类型。
        chunk: 来源分块 ID。

    Returns:
        关系记录字典。
    """
    return {
        "kind": "relation",
        "source": source,
        "target": target,
        "type": rel_type,
        "source_chunk_id": chunk,
    }


class TestContract(unittest.TestCase):
    """import 测试与构造契约。"""

    def test_dependencies_importable(self) -> None:
        """networkx 与 threading 可用。"""
        import networkx
        import threading as threading_module

        self.assertTrue(hasattr(networkx, "DiGraph"))
        self.assertTrue(hasattr(networkx, "single_source_shortest_path_length"))
        self.assertTrue(hasattr(threading_module, "Lock"))

    def test_public_methods_present(self) -> None:
        """六个公共方法齐备。"""
        for name in (
            "add_entities",
            "search_by_keyword",
            "get_neighbors",
            "count",
            "clear",
        ):
            self.assertTrue(callable(getattr(MemoryGraphStore, name)), name)

    def test_initial_state(self) -> None:
        """初始图为空。"""
        store = MemoryGraphStore()
        self.assertEqual(store.count(), (0, 0))
        self.assertEqual(store.search_by_keyword("任意"), [])


class TestAddEntities(unittest.TestCase):
    """写入语义。"""

    def setUp(self) -> None:
        self.store = MemoryGraphStore()

    def test_add_entity_creates_node_with_type(self) -> None:
        """实体写入后成为带 type 属性的节点。"""
        self.store.add_entities([entity("设备A", "device")])
        self.assertEqual(self.store.count(), (1, 0))
        self.assertEqual(self.store.node_types(), {"设备A": "device"})

    def test_add_relation_creates_edge_with_type(self) -> None:
        """关系写入后成为带 type 属性的有向边。"""
        self.store.add_entities([relation("设备A", "频繁重启", "device_fault")])
        self.assertEqual(self.store.count(), (2, 1))
        results = self.store.search_by_keyword("设备A")
        self.assertIn("设备A -> 频繁重启 (device_fault)", results[0]["subgraph_text"])

    def test_kind_inferred_when_missing(self) -> None:
        """缺 kind 字段时按字段推断实体/关系。"""
        self.store.add_entities(
            [
                {"name": "电源模块", "type": "component", "source_chunk_id": "c9"},
                {"source": "电源模块", "target": "电压不稳", "type": "fault_solution"},
            ]
        )
        self.assertEqual(self.store.count(), (2, 1))

    def test_records_without_required_fields_are_skipped(self) -> None:
        """缺 name 或缺 source/target 的记录被跳过。"""
        self.store.add_entities(
            [
                {"kind": "entity", "type": "device"},
                {"kind": "relation", "source": "A", "type": "device_fault"},
                {"kind": "relation", "target": "B", "type": "device_fault"},
            ]
        )
        self.assertEqual(self.store.count(), (0, 0))


class TestEntityMerge(unittest.TestCase):
    """实体合并测试（验收检查点）。"""

    def setUp(self) -> None:
        self.store = MemoryGraphStore()

    def test_same_entity_merges_source_chunk_ids(self) -> None:
        """相同实体多次 add，来源分块 ID 按序去重合并。"""
        self.store.add_entities([entity("设备A", "device", "c1")])
        self.store.add_entities(
            [
                entity("设备A", "device", "c2"),
                entity("设备A", "device", "c1"),
                entity("设备A", "device", "c3"),
            ]
        )
        self.assertEqual(self.store.entity_sources("设备A"), ["c1", "c2", "c3"])
        self.assertEqual(self.store.count()[0], 1)

    def test_distinct_entities_keep_separate_sources(self) -> None:
        """不同实体的来源互不串扰。"""
        self.store.add_entities(
            [
                entity("设备A", "device", "c1"),
                entity("设备B", "device", "c2"),
            ]
        )
        self.assertEqual(self.store.entity_sources("设备A"), ["c1"])
        self.assertEqual(self.store.entity_sources("设备B"), ["c2"])

    def test_unknown_entity_has_no_sources(self) -> None:
        """未登记实体返回空列表。"""
        self.assertEqual(self.store.entity_sources("不存在"), [])

    def test_node_type_follows_latest_write(self) -> None:
        """重复写入同名实体时，节点类型以最后一次写入为准，来源分块仍去重合并。

        Note:
            这是 ``networkx.DiGraph.add_node`` 的更新语义，与规范给定的实现方式
            （直接调用 ``add_node(name, type=...)``）一致。
        """
        self.store.add_entities([entity("设备A", "device", "c1")])
        self.store.add_entities([entity("设备A", "fault_type", "c2")])
        self.assertEqual(self.store.node_types()["设备A"], "fault_type")
        self.assertEqual(self.store.entity_sources("设备A"), ["c1", "c2"])


class TestSearchByKeyword(unittest.TestCase):
    """关键词子图检索。"""

    def setUp(self) -> None:
        self.store = MemoryGraphStore()
        self.store.add_entities(
            [
                entity("某设备", "device", "c1"),
                relation("某设备", "频繁重启", "device_fault", "c1"),
                relation("某设备", "风扇异常", "device_fault", "c1"),
                entity("电源模块", "component", "c2"),
            ]
        )

    def test_returns_center_entity_and_neighbors(self) -> None:
        """返回中心实体、邻居与子图文本。"""
        results = self.store.search_by_keyword("设备")
        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item["center_entity"], "某设备")
        self.assertIn("频繁重启", item["neighbors"])
        self.assertIn("风扇异常", item["neighbors"])
        self.assertIn("某设备 -> 频繁重启 (device_fault)", item["subgraph_text"])

    def test_respects_max_results(self) -> None:
        """命中数受 max_results 限制。"""
        store = MemoryGraphStore()
        store.add_entities([entity(f"设备{i}", "device", f"c{i}") for i in range(10)])
        self.assertEqual(len(store.search_by_keyword("设备", max_results=3)), 3)
        self.assertEqual(len(store.search_by_keyword("设备", max_results=99)), 10)

    def test_case_insensitive_match(self) -> None:
        """匹配不区分大小写。"""
        store = MemoryGraphStore()
        store.add_entities([entity("Server-X", "device", "c1")])
        self.assertEqual(len(store.search_by_keyword("server")), 1)
        self.assertEqual(len(store.search_by_keyword("SERVER")), 1)

    def test_no_match_returns_empty(self) -> None:
        """无命中返回空列表。"""
        self.assertEqual(self.store.search_by_keyword("不存在的实体"), [])


class TestGetNeighbors(unittest.TestCase):
    """按跳数取邻居。"""

    def setUp(self) -> None:
        self.store = MemoryGraphStore()
        self.store.add_entities(
            [
                entity("A", "device", "c1"),
                relation("A", "B", "device_fault", "c1"),
                relation("B", "C", "fault_solution", "c1"),
            ]
        )

    def test_depth_one_returns_direct_neighbors(self) -> None:
        """depth=1 只返回直接邻居。"""
        result = self.store.get_neighbors("A", depth=1)
        self.assertEqual(result, [{"entity": "B", "distance": 1}])

    def test_depth_two_expands(self) -> None:
        """depth=2 扩展到两跳。"""
        result = self.store.get_neighbors("A", depth=2)
        mapping = {item["entity"]: item["distance"] for item in result}
        self.assertEqual(mapping, {"B": 1, "C": 2})

    def test_center_entity_excluded(self) -> None:
        """结果不含中心实体自身。"""
        names = [item["entity"] for item in self.store.get_neighbors("A", depth=2)]
        self.assertNotIn("A", names)

    def test_unknown_entity_raises_key_error(self) -> None:
        """不存在的实体应抛 KeyError。"""
        with self.assertRaises(KeyError):
            self.store.get_neighbors("不存在")


class TestCountAndClear(unittest.TestCase):
    """规模统计与清空。"""

    def test_count_returns_nodes_and_edges(self) -> None:
        """count 返回 (节点数, 边数)。"""
        store = MemoryGraphStore()
        store.add_entities(
            [
                entity("设备A", "device", "c1"),
                relation("设备A", "重启", "device_fault", "c1"),
            ]
        )
        self.assertEqual(store.count(), (2, 1))

    def test_clear_empties_graph_and_index(self) -> None:
        """clear 后图与实体索引均为空。"""
        store = MemoryGraphStore()
        store.add_entities([entity("设备A", "device", "c1")])
        store.clear()
        self.assertEqual(store.count(), (0, 0))
        self.assertEqual(store.entity_sources("设备A"), [])
        self.assertEqual(store.search_by_keyword("设备"), [])
        self.assertEqual(store.node_types(), {})

    def test_clear_then_add_again(self) -> None:
        """清空后可继续写入。"""
        store = MemoryGraphStore()
        store.add_entities([entity("设备A", "device", "c1")])
        store.clear()
        store.add_entities([entity("设备B", "device", "c2")])
        self.assertEqual(store.count(), (1, 0))
        self.assertEqual(store.entity_sources("设备B"), ["c2"])


class TestThreadSafety(unittest.TestCase):
    """并发写入与读取。"""

    def test_concurrent_add_ten_threads(self) -> None:
        """10 线程各写入 50 组实体+关系，节点数为 501（500 设备 + 1 公共尾实体）。"""
        store = MemoryGraphStore()
        errors: list[BaseException] = []

        def worker(worker_id: int) -> None:
            try:
                for index in range(50):
                    store.add_entities(
                        [
                            entity(
                                f"设备{worker_id}-{index}",
                                "device",
                                f"c{worker_id}-{index}",
                            ),
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

        self.assertEqual(errors, [])
        nodes, edges = store.count()
        self.assertEqual((nodes, edges), (501, 500))

    def test_concurrent_search_during_write(self) -> None:
        """写入过程中并发检索不报错。"""
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
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(store.count()[0], 81)


class TestZeroDisk(unittest.TestCase):
    """零磁盘静态扫描（验收检查点）。"""

    def test_source_has_no_disk_api_calls(self) -> None:
        """源码不得出现 open / write / pickle / write_graphml 等落盘调用。"""
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
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
