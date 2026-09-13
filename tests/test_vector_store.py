"""``MemoryVectorStore`` 测试（pytest）。

覆盖用户验收要求的五个用例：

1. **test_add_and_count** —— 添加向量后验证 count 正确；
2. **test_search** —— 添加已知向量，搜索验证返回正确文档和分数；
3. **test_search_empty** —— 空索引搜索返回空列表；
4. **test_clear** —— clear 后 count 为 0，搜索返回空；
5. **test_thread_safety** —— 10 线程并发 add 100 次，验证 count==1000 且无异常。

另附加契约用例：维度校验、float64 兼容、混合读写并发、
零磁盘 AST 静态扫描（禁 faiss.write_index / pickle / shelve / open 等）。

运行方式::

    python -m pytest tests/test_vector_store.py -p no:cacheprovider -q
"""

from __future__ import annotations

import ast
import pathlib
import threading
from unittest import mock

import numpy as np
import pytest
from langchain_core.documents import Document

from src.retrieval.vector_store import MemoryVectorStore

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "retrieval" / "vector_store.py"
)

BANNED_ATTRS = {
    "write", "write_text", "write_bytes", "dump", "dump_index", "write_index",
    "write_graphml", "to_disk", "shelve", "savez", "save",
}
BANNED_NAMES = {"open", "pickle"}

DIM = 8


def make_chunks(count: int) -> list[Document]:
    """构造带 chunk_id / chunk_index 元数据的测试文档列表。"""
    return [
        Document(page_content=f"chunk-{i}", metadata={"chunk_id": f"c{i}", "chunk_index": i})
        for i in range(count)
    ]


def random_matrix(rows: int, dim: int = DIM) -> np.ndarray:
    """构造 ``(rows, dim)`` 的 float32 随机矩阵。"""
    return np.random.rand(rows, dim).astype(np.float32)


# ═══════════ 验收用例 1：add / count ═══════════


def test_add_and_count() -> None:
    """分批写入后 count 应等于累计向量条数。"""
    store = MemoryVectorStore(DIM)

    assert store.count() == 0
    store.add(random_matrix(5), make_chunks(5))
    assert store.count() == 5
    store.add(random_matrix(3), make_chunks(3))
    assert store.count() == 8


# ═══════════ 验收用例 2：search ═══════════


def test_search() -> None:
    """用已知单位向量检索，应命中对应文档且分数为 1（余弦语义）。"""
    store = MemoryVectorStore(DIM)
    vectors = np.eye(DIM, dtype=np.float32)
    store.add(vectors, make_chunks(DIM))

    results = store.search(vectors[2], top_k=3)

    assert len(results) == 3
    document, score = results[0]
    assert isinstance(document, Document)
    assert isinstance(score, float)
    assert document.metadata["chunk_index"] == 2
    assert document.page_content == "chunk-2"
    assert results[0][1] == pytest.approx(1.0, abs=1e-5)
    # 正交向量余弦为 0
    assert results[1][1] == pytest.approx(0.0, abs=1e-5)
    # 分数严格降序
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)


def test_search_top_k_limited() -> None:
    """返回条数不超过 top_k 且不超过索引条数。"""
    store = MemoryVectorStore(DIM)
    store.add(random_matrix(10), make_chunks(10))

    assert len(store.search(random_matrix(1)[0], top_k=3)) == 3
    assert len(store.search(random_matrix(1)[0], top_k=99)) == 10


def test_search_accepts_1d_and_2d_query() -> None:
    """一维与 (1, dim) 二维查询向量均应被接受。"""
    store = MemoryVectorStore(DIM)
    store.add(random_matrix(2), make_chunks(2))

    assert len(store.search(random_matrix(1)[0], top_k=2)) == 2
    assert len(store.search(random_matrix(1), top_k=2)) == 2


# ═══════════ 验收用例 3：空索引搜索 ═══════════


def test_search_empty() -> None:
    """空索引搜索应返回空列表而非抛错。"""
    store = MemoryVectorStore(DIM)
    assert store.search(np.ones(DIM, dtype=np.float32), top_k=5) == []


# ═══════════ 验收用例 4：clear ═══════════


def test_clear() -> None:
    """clear 后 count 为 0、搜索返回空，且可继续复用。"""
    store = MemoryVectorStore(DIM)
    store.add(random_matrix(3), make_chunks(3))

    store.clear()

    assert store.count() == 0
    assert store.search(random_matrix(1)[0], top_k=5) == []

    store.add(random_matrix(2), make_chunks(2))
    assert store.count() == 2
    assert store.embedding_dim == DIM


# ═══════════ 验收用例 5：线程安全 ═══════════


def test_thread_safety() -> None:
    """10 线程并发 add 100 次：count==1000 且无任何线程异常。"""
    store = MemoryVectorStore(DIM)
    errors: list[BaseException] = []
    barrier = threading.Barrier(10)

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(100):
                store.add(random_matrix(1), [Document(page_content="x")])
        except BaseException as exc:  # noqa: BLE001 - 收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.count() == 1000


def test_concurrent_add_and_clear_and_search() -> None:
    """混合并发（add + clear + search）不抛异常——P1 修复回归用例。"""
    store = MemoryVectorStore(DIM)
    store.add(random_matrix(64), make_chunks(64))
    errors: list[BaseException] = []
    stop = threading.Event()

    def adder() -> None:
        try:
            for _ in range(60):
                store.add(random_matrix(1), [Document(page_content="y")])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()

    def clearer() -> None:
        try:
            for _ in range(6):
                store.clear()
                stop.wait(0.01)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def searcher() -> None:
        try:
            while not stop.is_set():
                store.search(random_matrix(1)[0], top_k=5)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = (
        [threading.Thread(target=adder), threading.Thread(target=clearer)]
        + [threading.Thread(target=searcher) for _ in range(3)]
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert store.count() >= 0


# ═══════════ 契约与边界 ═══════════


class TestContract:
    """构造契约与维度校验。"""

    def test_init_rejects_non_positive_dimension(self) -> None:
        """维度非正数应抛 ValueError。"""
        for bad in (0, -1):
            with pytest.raises(ValueError, match="positive"):
                MemoryVectorStore(bad)

    def test_add_count_mismatch_asserts(self) -> None:
        """向量与文档条数不一致应触发断言。"""
        store = MemoryVectorStore(DIM)
        with pytest.raises(AssertionError, match="count mismatch"):
            store.add(random_matrix(3), make_chunks(2))

    def test_add_wrong_dimension_raises(self) -> None:
        """维度与初始化不符应抛 ValueError。"""
        store = MemoryVectorStore(DIM)
        with pytest.raises(ValueError, match="incompatible"):
            store.add(random_matrix(2, dim=DIM + 1), make_chunks(2))

    def test_add_accepts_float64(self) -> None:
        """float64 输入应被内部转为 float32。"""
        store = MemoryVectorStore(DIM)
        store.add(np.random.rand(2, DIM), make_chunks(2))
        assert store.count() == 2


class TestZeroDisk:
    """零磁盘静态扫描：除显式持久化方法外，源码禁出现任何落盘调用。"""

    def test_source_has_no_disk_api_calls(self) -> None:
        """AST 扫描禁用 open / write_index / pickle / shelve 等。

        豁免说明：知识库已按既定决策落到项目之外的 RAG_DATA_DIR，
        save / load 是唯一的显式落盘入口；模块其余部分（add / search / clear 等）
        必须继续满足纯内存语义 —— 否则零磁盘约束会被悄悄绕过。
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        allowed_methods = {"save", "load"}
        found: list[str] = []

        def visit(node: ast.AST, in_allowed: bool) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                in_allowed = node.name in allowed_methods
            if isinstance(node, ast.Call) and not in_allowed:
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRS:
                    found.append(f"attr:{func.attr}@{node.lineno}")
                if isinstance(func, ast.Name) and func.id in BANNED_NAMES:
                    found.append(f"name:{func.id}@{node.lineno}")
            for child in ast.iter_child_nodes(node):
                visit(child, in_allowed)

        visit(tree, False)
        assert found == []
