"""RRF 融合算法验证 + 降级逻辑测试（pytest）。

覆盖 Goal 验收的三项任务：

1. **任务 1** —— 手动构造三路已知排名，逐项对比手算 RRF 分数
   （``score = Σ 1/(k + rank)``，rank 自 1 起）：同时验证通用原语
   ``reciprocal_rank_fusion`` 与编排层 ``HybridRetriever.rrf_fusion``；
2. **任务 2** —— 某一路检索返回**空列表**时，融合过程不报错且正常返回
   （区别于既有测试覆盖的「单路抛异常」降级）；
3. **任务 3** —— ``CrossEncoderReranker`` 重排序返回数量**严格不超过** top_k，
   并验证截断保留的是真实最高分文档。

全部使用确定性 stub 模型，不触发真实权重下载（零磁盘 + C 盘约束）。

运行方式::

    python -m pytest tests/test_rrf_rerank_verification.py -p no:cacheprovider -q
"""

from __future__ import annotations
from unittest import mock

import pytest
from langchain_core.documents import Document

from core.retrieval import RRF_K, RRF_TOP_K, HybridRetriever
from src.knowledge.graph_builder import MemoryGraphStore
from src.retrieval.reranker import CrossEncoderReranker
from src.retrieval.vector_store import MemoryVectorStore, reciprocal_rank_fusion


# ═══════════ 确定性替身 ═══════════


class StubEmbedder:
    """占位向量模型：仅满足构造签名，不参与 RRF 算术验证。"""

    def encode(self, texts: list[str], normalize_embeddings: bool = True) -> object:  # noqa: ARG002
        raise AssertionError("RRF 验证不应触发向量编码")


class StubCrossEncoder:
    """确定性重排替身：分数 = 查询与文档的字符交集占比。"""

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [
            len(set(query) & set(document)) / (len(set(query)) + 1.0)
            for query, document in pairs
        ]


def make_degraded_retriever() -> HybridRetriever:
    """构造 Cross-Encoder 加载失败（降级态）的检索器，避免真实权重加载。"""
    with mock.patch(
        "src.retrieval.reranker.CrossEncoder", side_effect=OSError("weights missing")
    ):
        return HybridRetriever(
            MemoryVectorStore(8), MemoryGraphStore(), StubEmbedder()
        )


# ═══════════ 任务 1：三路已知结果 vs 手算分数 ═══════════
#
# 三路排名设计（k=60，每个文档的名次多重集互不相同，总分严格可分辨）：
#   文档  L1 名次  L2 名次  L3 名次   手算总分
#   A     1        2        3        1/61 + 1/62 + 1/63
#   B     2        1        4        1/62 + 1/61 + 1/64
#   C     3        3        1        1/63 + 1/63 + 1/61
#   D     4        4        2        1/64 + 1/64 + 1/62
#   E     5        5        5        3/65
#   F     6        -        -        1/66
# 期望排序：A > B > C > D > E > F（无并列）。

HAND_LIST_1: list[str] = ["A", "B", "C", "D", "E", "F"]
HAND_LIST_2: list[str] = ["B", "A", "C", "D", "E"]
HAND_LIST_3: list[str] = ["C", "D", "A", "B", "E"]

HAND_EXPECTED: dict[str, float] = {
    "A": 1 / 61 + 1 / 62 + 1 / 63,
    "B": 1 / 62 + 1 / 61 + 1 / 64,
    "C": 1 / 63 + 1 / 63 + 1 / 61,
    "D": 1 / 64 + 1 / 64 + 1 / 62,
    "E": 3 / 65,
    "F": 1 / 66,
}
HAND_ORDER: list[str] = ["A", "B", "C", "D", "E", "F"]


def test_rrf_hand_computed_scores_primitive() -> None:
    """任务 1（原语层）：三路已知排名的融合分数与手算结果逐项一致。"""
    fused = reciprocal_rank_fusion([HAND_LIST_1, HAND_LIST_2, HAND_LIST_3], k=RRF_K)

    assert [chunk_id for chunk_id, _ in fused] == HAND_ORDER
    for chunk_id, score in fused:
        assert score == pytest.approx(HAND_EXPECTED[chunk_id], rel=1e-12), chunk_id


def test_rrf_hand_computed_scores_via_retriever() -> None:
    """任务 1（编排层）：HybridRetriever.rrf_fusion 与手算结果一致。"""
    retriever = make_degraded_retriever()
    # 有序路以递减分数携带排名次序；图谱路保持原始返回顺序。
    vector_results = [(doc_id, 1.0 - index * 0.01) for index, doc_id in enumerate(HAND_LIST_1)]
    bm25_results = [(doc_id, 1.0 - index * 0.01) for index, doc_id in enumerate(HAND_LIST_2)]
    graph_results = [{"chunk_id": doc_id} for doc_id in HAND_LIST_3]

    fused = retriever.rrf_fusion(vector_results, bm25_results, graph_results)

    assert [chunk_id for chunk_id, _ in fused] == HAND_ORDER
    for chunk_id, score in fused:
        assert score == pytest.approx(HAND_EXPECTED[chunk_id], rel=1e-12), chunk_id


def test_rrf_dedup_keeps_best_rank_per_path() -> None:
    """任务 1（补充）：同一路内重复标识只按首次名次计分一次，且不推进名次。"""
    fused = reciprocal_rank_fusion([["A", "B", "A"], ["A", "B", "C"]], k=RRF_K)

    # L1: A(rank1) B(rank2)，第三个 A 为路内重复 → 跳过且名次不推进；
    # L2: A(rank1) B(rank2) C(rank3)。
    expected = {
        "A": 2 / (RRF_K + 1),
        "B": 2 / (RRF_K + 2),
        "C": 1 / (RRF_K + 3),
    }
    scores = dict(fused)
    assert scores["A"] == pytest.approx(expected["A"], rel=1e-12)
    assert scores["B"] == pytest.approx(expected["B"], rel=1e-12)
    assert scores["C"] == pytest.approx(expected["C"], rel=1e-12)
    assert [chunk_id for chunk_id, _ in fused] == ["A", "B", "C"]


# ═══════════ 任务 2：单路空列表降级 ═══════════


def test_rrf_single_path_empty_primitive() -> None:
    """任务 2（原语层）：某一路为空列表时融合不报错，其余路正常计分。"""
    fused = reciprocal_rank_fusion([["A", "B", "C"], [], ["B", "D"]], k=RRF_K)

    expected = {
        "A": 1 / (RRF_K + 1),
        "B": 1 / (RRF_K + 2) + 1 / (RRF_K + 1),
        "C": 1 / (RRF_K + 3),
        "D": 1 / (RRF_K + 2),
    }
    assert [chunk_id for chunk_id, _ in fused] == ["B", "A", "D", "C"]
    for chunk_id, score in fused:
        assert score == pytest.approx(expected[chunk_id], rel=1e-12), chunk_id


def test_rrf_each_single_path_empty_via_retriever() -> None:
    """任务 2（编排层）：三路中任意一路为空，融合均正常返回非空结果。

    Note:
        各路名次独立从 1 起算。用例通过让不同路共享部分文档标识制造
        可分辨的累加分数（如 ``x`` 同时出现在向量路与图谱路），
        避免不同路同名次产生的并列干扰排序断言。
    """
    retriever = make_degraded_retriever()

    # 向量路为空：bm25 与图谱路正常融合。
    fused = retriever.rrf_fusion([], [("x", 9.0), ("y", 8.0)], [{"chunk_id": "x"}])
    scores = dict(fused)
    assert scores["x"] == pytest.approx(2 / (RRF_K + 1), rel=1e-12)
    assert scores["y"] == pytest.approx(1 / (RRF_K + 2), rel=1e-12)
    assert [chunk_id for chunk_id, _ in fused] == ["x", "y"]

    # BM25 路为空：y 跨两路累加后反超向量路第一名 x。
    fused = retriever.rrf_fusion([("x", 0.9), ("y", 0.8)], [], [{"chunk_id": "y"}])
    scores = dict(fused)
    assert scores["x"] == pytest.approx(1 / (RRF_K + 1), rel=1e-12)
    assert scores["y"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1), rel=1e-12)
    assert [chunk_id for chunk_id, _ in fused] == ["y", "x"]

    # 图谱路为空：z 跨向量路与 BM25 路累加后居首。
    fused = retriever.rrf_fusion(
        [("x", 0.9), ("y", 0.8), ("z", 0.7)], [("z", 5.0)], []
    )
    scores = dict(fused)
    assert scores["z"] == pytest.approx(1 / (RRF_K + 3) + 1 / (RRF_K + 1), rel=1e-12)
    assert scores["x"] == pytest.approx(1 / (RRF_K + 1), rel=1e-12)
    assert scores["y"] == pytest.approx(1 / (RRF_K + 2), rel=1e-12)
    assert [chunk_id for chunk_id, _ in fused] == ["z", "x", "y"]


def test_rrf_all_paths_empty_returns_empty() -> None:
    """任务 2（边界）：三路全为空列表时返回空列表而非抛异常。"""
    assert reciprocal_rank_fusion([[], [], []], k=RRF_K) == []

    retriever = make_degraded_retriever()
    assert retriever.rrf_fusion([], [], []) == []


# ═══════════ 任务 3：重排序严格不超过 top_k ═══════════


def test_rerank_output_len_strictly_capped() -> None:
    """任务 3（核心）：CrossEncoderReranker 返回数量严格 ≤ top_k。"""
    reranker = CrossEncoderReranker(top_k=3, model=StubCrossEncoder())
    documents = [f"文档{index} 设备故障" for index in range(10)]

    results = reranker.rerank("设备 故障", documents)

    assert reranker.available is True
    assert len(results) == 3  # ≤ top_k=3，且 min(10, 3)=3
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("top_k", [1, 2, 3, 5, 7, 10, 50])
def test_rerank_len_never_exceeds_top_k(top_k: int) -> None:
    """任务 3（参数化）：任意 top_k 下输出长度 == min(候选数, top_k) ≤ top_k。"""
    reranker = CrossEncoderReranker(top_k=top_k, model=StubCrossEncoder())
    documents = [f"候选{index}" for index in range(10)]

    results = reranker.rerank("查询", documents)

    assert len(results) <= top_k
    assert len(results) == min(len(documents), top_k)


def test_rerank_truncation_keeps_true_top_scores() -> None:
    """任务 3（截断正确性）：被截断保留的应是替身分数下的真实 Top-k。"""
    reranker = CrossEncoderReranker(top_k=2, model=StubCrossEncoder())
    documents = ["电源模块 老化", "完全无关内容", "风扇 转速 异常"]
    query = "电源 风扇"

    results = reranker.rerank(query, documents)

    full_scores = StubCrossEncoder().predict([(query, doc) for doc in documents])
    expected_docs = [
        doc
        for doc, _ in sorted(zip(documents, full_scores), key=lambda p: p[1], reverse=True)
    ][:2]
    assert [doc for doc, _ in results] == expected_docs
    assert len(results) == 2


def test_rerank_empty_candidates_and_unloaded_model() -> None:
    """任务 3（边界）：空候选返回空列表；未加载模型时 predict/rerank 抛 RuntimeError。"""
    reranker = CrossEncoderReranker(top_k=5, model=StubCrossEncoder())
    assert reranker.rerank("查询", []) == []

    unloaded = CrossEncoderReranker(top_k=5)
    assert unloaded.available is False
    with pytest.raises(RuntimeError, match="not loaded"):
        unloaded.predict([("q", "d")])
    with pytest.raises(RuntimeError, match="not loaded"):
        unloaded.rerank("查询", ["文档"])


def test_retriever_rerank_wrapper_never_exceeds_top_k() -> None:
    """任务 3（编排层）：HybridRetriever.rerank（含降级路径）同样不超 top_k。"""
    retriever = make_degraded_retriever()
    chunks = [
        Document(page_content=f"设备{i} 故障", metadata={"chunk_id": f"c{i}"})
        for i in range(8)
    ]
    retriever.build_bm25_index(chunks)
    candidates = [(f"c{i}", 1.0 - i * 0.05) for i in range(8)]

    for top_k in (1, 3, 5, 8, 99):
        results = retriever.rerank("设备 故障", candidates, top_k=top_k)
        assert len(results) <= top_k
        assert len(results) == min(len(candidates), top_k)
