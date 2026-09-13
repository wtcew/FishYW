"""HybridRetriever 测试（pytest）。

覆盖用户验收要求的四个检查点：

1. **test_build_bm25_index_populates_memory** —— 构建 BM25 索引后内存中 _bm25 对象非空；
2. **test_retrieve_survives_vector_path_failure**（及 bm25 / graph / 全路同名用例）——
   模拟单路检索抛异常，retrieve 仍返回其他路结果；
3. **test_rrf_fusion_sorted_desc_and_capped_at_20** —— RRF 结果严格降序且不超过 20；
4. **test_rerank_count_equals_min_candidates_top_k** —— 重排序返回数量等于
   min(len(candidates), top_k)。

另附加两处接口缺陷的回归用例：

* bm25_search 必须以 BM25 分数数组的整数位置映射回 chunk_id，
  而不是直接用整数索引以字符串为键的 _chunk_map（原参考实现会 KeyError）；
* graph_search 必须经 entity_sources 回填 chunk_id，
  否则图谱路在 RRF 融合中恒为零贡献。

以及降级路径、空语料边界与零磁盘 AST 静态扫描。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/test_retrieval.py
"""

from __future__ import annotations

import asyncio
import ast
import os
import pathlib
import threading
import time
from unittest import mock

import numpy as np
import pytest
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

from core.retrieval import RRF_K, RRF_TOP_K, HybridRetriever
from src.knowledge.graph_builder import MemoryGraphStore
from src.retrieval.vector_store import MemoryVectorStore
from src.settings import get_settings

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "core" / "retrieval.py"

BANNED_ATTRS = {
    "write", "write_text", "write_bytes", "dump", "dump_index", "write_index",
    "write_graphml", "to_disk", "shelve", "savez", "save",
}
BANNED_NAMES = {"open", "pickle"}

DIM = 8

#: 真实重排序模型开关：默认关闭，避免常规测试依赖约 2.27GB 权重。
REAL_RERANKER_ENABLED = os.environ.get("RAG_REAL_RERANKER") == "1"

SAMPLE_TEXTS = [
    "设备A 频繁重启 电源模块 电压不稳",
    "设备A 风扇转速异常 散热故障",
    "设备B 网络丢包 交换机端口故障",
    "电源模块 电容老化 需要更换",
    "设备A 频繁重启 解决方案 更换电源",
]


class StubEmbeddingModel:
    """确定性向量模型替身：把字符序数映射为固定维度单位向量。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim: int = dim

    def encode(self, texts: list[str], normalize_embeddings: bool = True) -> np.ndarray:
        """返回 (len(texts), dim) 的 L2 归一化矩阵。"""
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for col in range(min(len(text), self.dim)):
                vectors[row, col] = float(ord(text[col]) % 7 + 1)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return vectors / norms


class StubCrossEncoder:
    """确定性重排替身：分数为查询与文档的字符交集占比。"""

    def __init__(self) -> None:
        self.calls: int = 0

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """对每个查询-文档对返回确定性分数。"""
        self.calls += 1
        return [
            len(set(query) & set(document)) / (len(set(query)) + 1.0)
            for query, document in pairs
        ]


def make_chunks(contents: list[str], prefix: str = "c") -> list[Document]:
    """构造带 chunk_id / chunk_index 元数据的分块列表。"""
    return [
        Document(
            page_content=text,
            metadata={"chunk_id": f"{prefix}{index}", "chunk_index": index},
        )
        for index, text in enumerate(contents)
    ]


def build_retriever(
    cross_encoder: object | None = None,
) -> tuple[HybridRetriever, MemoryVectorStore, MemoryGraphStore]:
    """构造带真实内存存储与替身模型的检索器。

    Args:
        cross_encoder: 重排替身；为 None 时自动创建新的 StubCrossEncoder。

    Returns:
        (检索器, 向量存储, 图存储) 三元组，三路数据均已就绪。
    """
    vector_store = MemoryVectorStore(DIM)
    graph_store = MemoryGraphStore()
    embedder = StubEmbeddingModel(DIM)
    chunks = make_chunks(SAMPLE_TEXTS)
    vector_store.add(
        embedder.encode([chunk.page_content for chunk in chunks]), chunks
    )
    graph_store.add_entities(
        [
            {"kind": "entity", "name": "设备A", "type": "device", "source_chunk_id": "c0"},
            {"kind": "entity", "name": "设备A", "type": "device", "source_chunk_id": "c1"},
            {
                "kind": "relation",
                "source": "设备A",
                "target": "频繁重启",
                "type": "device_fault",
                "source_chunk_id": "c0",
            },
            {"kind": "entity", "name": "电源模块", "type": "component", "source_chunk_id": "c3"},
        ]
    )
    encoder = cross_encoder if cross_encoder is not None else StubCrossEncoder()
    retriever = HybridRetriever(vector_store, graph_store, embedder, cross_encoder=encoder)
    retriever.build_bm25_index(chunks)
    return retriever, vector_store, graph_store


# ═══════════ 验收检查点 1：BM25 索引构建 ═══════════


def test_build_bm25_index_populates_memory() -> None:
    """构建后 _bm25 非空，_chunk_map 与 _chunk_ids 覆盖全部分块。"""
    retriever, _, _ = build_retriever()

    assert retriever._bm25 is not None
    assert len(retriever._chunk_map) == len(SAMPLE_TEXTS)
    assert len(retriever._chunk_ids) == len(SAMPLE_TEXTS)
    assert retriever._chunk_ids == [chunk.metadata["chunk_id"] for chunk in make_chunks(SAMPLE_TEXTS)]


def test_bm25_search_maps_positions_to_chunk_ids() -> None:
    """缺陷回归：BM25 的整数位置必须映射回 chunk_id（原实现直接索引字典会 KeyError）。"""
    retriever, _, _ = build_retriever()

    results = retriever.bm25_search("设备A 频繁重启 电源模块", top_k=3)

    assert results, "BM25 应至少命中一条"
    for chunk_id, score in results:
        assert chunk_id in retriever._chunk_map
        assert isinstance(score, float)


def test_build_bm25_index_skips_chunks_without_id() -> None:
    """缺少 chunk_id 的分块被跳过，位置映射保持一一对应。"""
    retriever, _, _ = build_retriever()

    retriever.build_bm25_index(
        [
            Document(page_content="有编号的分块 设备A", metadata={"chunk_id": "x0"}),
            Document(page_content="缺编号的分块 设备B", metadata={}),
        ]
    )

    assert retriever._chunk_ids == ["x0"]
    assert len(retriever._chunk_map) == 1


def test_build_bm25_index_handles_empty_corpus() -> None:
    """空语料不抛异常：_bm25 置空且 bm25_search 返回空列表。"""
    retriever, _, _ = build_retriever()

    retriever.build_bm25_index([])

    assert retriever._bm25 is None
    assert retriever.bm25_search("任意查询") == []


def test_vector_search_returns_chunk_ids() -> None:
    """向量路返回 (chunk_id, 分数) 且按分数降序。"""
    retriever, _, _ = build_retriever()

    results = retriever.vector_search("设备A 频繁重启", top_k=3)

    assert len(results) == 3
    for chunk_id, _ in results:
        assert chunk_id in retriever._chunk_map
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_graph_search_enriches_source_chunk_id() -> None:
    """缺陷回归：图谱命中必须回填来源 chunk_id，否则 RRF 中恒为零贡献。"""
    retriever, _, _ = build_retriever()

    with mock.patch(
        "core.retrieval.jieba.analyse.extract_tags", return_value=["设备A"]
    ):
        results = retriever.graph_search("设备A")

    assert results
    first = results[0]
    assert first["center_entity"] == "设备A"
    assert first["chunk_id"] == "c0"
    assert first["chunk_ids"] == ["c0", "c1"]


def test_graph_result_contributes_to_rrf_fusion() -> None:
    """图谱路的 chunk_id 必须真实进入融合分数。"""
    retriever, _, _ = build_retriever()

    fused = retriever.rrf_fusion([], [], [{"center_entity": "设备A", "chunk_id": "c0"}])

    assert len(fused) == 1
    chunk_id, score = fused[0]
    assert chunk_id == "c0"
    assert score == pytest.approx(1.0 / (RRF_K + 1))


# ═══════════ 验收检查点 2：单路异常不影响其他路 ═══════════


def test_retrieve_survives_vector_path_failure() -> None:
    """向量路抛异常时，retrieve 仍返回其他路结果。"""
    retriever, vector_store, _ = build_retriever()

    with mock.patch.object(vector_store, "search", side_effect=RuntimeError("faiss boom")):
        results = retriever.retrieve("设备A 频繁重启 电源模块")

    assert results
    assert all(isinstance(document, Document) for document, _ in results)


def test_retrieve_survives_bm25_path_failure() -> None:
    """BM25 路抛异常时，retrieve 仍返回其他路结果。"""
    retriever, _, _ = build_retriever()

    with mock.patch.object(retriever._bm25, "get_scores", side_effect=RuntimeError("bm25 boom")):
        results = retriever.retrieve("设备A 频繁重启 电源模块")

    assert results


def test_retrieve_survives_graph_path_failure() -> None:
    """图谱路抛异常时，retrieve 仍返回其他路结果。"""
    retriever, _, graph_store = build_retriever()

    with mock.patch.object(
        graph_store, "search_by_keyword", side_effect=RuntimeError("graph boom")
    ):
        results = retriever.retrieve("设备A 频繁重启 电源模块")

    assert results


def test_retrieve_survives_all_paths_failing() -> None:
    """三路全失败时返回空列表而非抛异常。"""
    retriever, vector_store, graph_store = build_retriever()

    with mock.patch.object(vector_store, "search", side_effect=RuntimeError("v")),          mock.patch.object(retriever._bm25, "get_scores", side_effect=RuntimeError("b")),          mock.patch.object(graph_store, "search_by_keyword", side_effect=RuntimeError("g")):
        assert retriever.retrieve("设备A 频繁重启") == []


# ═══════════ 验收检查点 3：RRF 融合 ═══════════


def test_rrf_fusion_sorted_desc_and_capped_at_20() -> None:
    """融合结果严格降序排列且不超过 Top-20。"""
    retriever, _, _ = build_retriever()
    vector_results = [(f"v{index}", 1.0 - index * 0.01) for index in range(30)]
    bm25_results = [(f"b{index}", 1.0 - index * 0.01) for index in range(30)]

    fused = retriever.rrf_fusion(vector_results, bm25_results, [])

    assert len(fused) == RRF_TOP_K
    scores = [score for _, score in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_fusion_rejects_non_positive_k() -> None:
    """k 非正数时抛 ValueError。"""
    retriever, _, _ = build_retriever()

    with pytest.raises(ValueError, match="positive"):
        retriever.rrf_fusion([], [], [], k=0)


# ═══════════ 验收检查点 4：重排序数量 ═══════════


def test_rerank_count_equals_min_candidates_top_k() -> None:
    """重排序返回数量严格等于 min(len(candidates), top_k)。"""
    retriever, _, _ = build_retriever()
    candidates = [
        (chunk_id, 1.0 - index * 0.01)
        for index, chunk_id in enumerate(retriever._chunk_ids)
    ]

    for top_k in (1, 3, 5, 99):
        results = retriever.rerank("设备A 频繁重启", candidates, top_k=top_k)
        assert len(results) == min(len(candidates), top_k)


def test_rerank_falls_back_when_cross_encoder_unavailable() -> None:
    """缺陷回归：模型加载失败不中断构造，rerank 降级为 RRF 分数排序。"""
    chunks = make_chunks(SAMPLE_TEXTS)
    embedder = StubEmbeddingModel(DIM)
    vector_store = MemoryVectorStore(DIM)
    vector_store.add(embedder.encode([chunk.page_content for chunk in chunks]), chunks)

    with mock.patch("src.retrieval.reranker.CrossEncoder", side_effect=OSError("weights missing")):
        retriever = HybridRetriever(vector_store, MemoryGraphStore(), embedder)

    assert retriever._cross_encoder is None

    retriever.build_bm25_index(chunks)
    candidates = [
        (chunk_id, 1.0 - index * 0.01)
        for index, chunk_id in enumerate(retriever._chunk_ids)
    ]
    results = retriever.rerank("设备A", candidates, top_k=3)

    assert len(results) == 3
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_rerank_falls_back_when_predict_raises() -> None:
    """推理解析异常时降级为 RRF 分数排序，数量语义不变。"""
    encoder = StubCrossEncoder()
    retriever, _, _ = build_retriever(cross_encoder=encoder)
    candidates = [
        (chunk_id, 1.0 - index * 0.01)
        for index, chunk_id in enumerate(retriever._chunk_ids)
    ]

    with mock.patch.object(encoder, "predict", side_effect=RuntimeError("inference boom")):
        results = retriever.rerank("设备A", candidates, top_k=2)

    assert len(results) == 2


def test_retrieve_end_to_end_returns_documents() -> None:
    """端到端：三路齐备时返回不超过 TOP_K_RERANK 条可溯源文档。"""
    retriever, _, _ = build_retriever()

    results = retriever.retrieve("设备A 频繁重启 电源模块")

    assert results
    assert len(results) <= 5
    for document, score in results:
        assert isinstance(document, Document)
        assert isinstance(score, float)


# ═══════════ 三路并发召回 ═══════════


def test_concurrent_sources_match_sequential() -> None:
    """并发召回 + 融合 + 重排的结果应与手工串行逐位一致。"""
    retriever, _, _ = build_retriever()
    query = "设备A 频繁重启 电源模块"
    settings = get_settings()

    concurrent = retriever.retrieve(query)

    vector_results = retriever.vector_search(query, top_k=settings.TOP_K_RETRIEVAL)
    bm25_results = retriever.bm25_search(query, top_k=settings.TOP_K_RETRIEVAL)
    graph_results = retriever.graph_search(query)
    fused = retriever.rrf_fusion(vector_results, bm25_results, graph_results)
    sequential = retriever.rerank(query, fused, top_k=settings.TOP_K_RERANK)

    assert [doc.metadata["chunk_id"] for doc, _ in concurrent] == [
        doc.metadata["chunk_id"] for doc, _ in sequential
    ]
    assert [round(score, 6) for _, score in concurrent] == [
        round(score, 6) for _, score in sequential
    ]


def test_aretrieve_matches_retrieve() -> None:
    """异步入口 aretrieve 与同步入口 retrieve 返回一致。"""
    retriever, _, _ = build_retriever()
    query = "设备A 频繁重启 电源模块"

    sync_result = retriever.retrieve(query)
    async_result = asyncio.run(retriever.aretrieve(query))

    assert [doc.metadata["chunk_id"] for doc, _ in sync_result] == [
        doc.metadata["chunk_id"] for doc, _ in async_result
    ]
    assert [round(score, 6) for _, score in sync_result] == [
        round(score, 6) for _, score in async_result
    ]


# ═══════════ 并发一致性 ═══════════


def _slow_bm25(corpus: list[list[str]]) -> BM25Okapi:
    """构造 BM25 前短暂停顿，用于放大索引重建的时间窗口。"""
    time.sleep(0.001)
    return BM25Okapi(corpus)


def test_bm25_search_consistent_under_concurrent_rebuild() -> None:
    """缺陷回归：并发重建索引时不得返回错位的 chunk_id 与分数组合。

    A 组语料含 ALPHA、B 组不含。若 bm25_search 取到「B 的标识 + A 的分数」
    这一错位组合，就会以正分返回以 B 开头的 chunk_id。
    """
    retriever, _, _ = build_retriever()
    docs_a = make_chunks(["ALPHA 甲乙丙"] * 20, prefix="A")
    docs_b = make_chunks(["BETA 丁戊己"] * 50, prefix="B")
    retriever.build_bm25_index(docs_a)

    misaligned: list[tuple[str, float]] = []
    stop = threading.Event()

    def rebuilder() -> None:
        for _ in range(30):
            retriever.build_bm25_index(docs_b)
            retriever.build_bm25_index(docs_a)
        stop.set()

    def searcher() -> None:
        while not stop.is_set():
            for chunk_id, score in retriever.bm25_search("ALPHA", top_k=10):
                if chunk_id.startswith("B") and score > 0.0:
                    misaligned.append((chunk_id, score))

    threads = [threading.Thread(target=rebuilder)] + [
        threading.Thread(target=searcher) for _ in range(3)
    ]
    with mock.patch("core.retrieval.BM25Okapi", side_effect=_slow_bm25):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

    assert misaligned == []


# ═══════════ 真实模型端到端（默认跳过）═══════════


@pytest.mark.skipif(
    not REAL_RERANKER_ENABLED,
    reason="需设置 RAG_REAL_RERANKER=1 且已缓存 BAAI/bge-reranker-v2-m3 权重",
)
def test_real_cross_encoder_reranks_candidates() -> None:
    """真实 bge-reranker-v2-m3 加载进内存并对候选完成实际打分排序。"""
    chunks = make_chunks(SAMPLE_TEXTS)
    embedder = StubEmbeddingModel(DIM)
    vector_store = MemoryVectorStore(DIM)
    vector_store.add(embedder.encode([chunk.page_content for chunk in chunks]), chunks)

    retriever = HybridRetriever(vector_store, MemoryGraphStore(), embedder)
    assert retriever._cross_encoder is not None, "真实重排序模型应加载成功"

    retriever.build_bm25_index(chunks)
    candidates = [
        (chunk_id, 1.0 - index * 0.01)
        for index, chunk_id in enumerate(retriever._chunk_ids)
    ]

    results = retriever.rerank("设备A频繁重启的根因是什么", candidates, top_k=5)

    assert len(results) == min(len(candidates), 5)
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)
    assert all(isinstance(score, float) for score in scores)


# ═══════════ 零磁盘 ═══════════


class TestZeroDisk:
    """零磁盘静态扫描：源码禁出现任何落盘调用。"""

    def test_source_has_no_disk_api_calls(self) -> None:
        """AST 扫描禁用 open / pickle / write 等落盘 API。"""
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
