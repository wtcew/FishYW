"""混合检索模块。

构建「向量召回 + BM25 关键词召回 + 知识图谱召回」三路并行的混合检索链路，
经 RRF（Reciprocal Rank Fusion）融合后交由 Cross-Encoder 精排，
为 Agent 状态机的检索节点提供可溯源的候选文档。

链路结构::

    query ─┬─> vector_search ─┐
           ├─> bm25_search    ├─> rrf_fusion ─> rerank ─> [(Document, score)]
           └─> graph_search  ─┘

零磁盘约束：

* BM25 语料由 :class:`rank_bm25.BM25Okapi` 在内存中构建，不落盘；
* Cross-Encoder 权重经 :class:`sentence_transformers.CrossEncoder` 直接加载到内存；
* 所有中间结果仅作为局部变量存在，不产生任何持久化文件。

Note:
    三路召回彼此独立：任一路抛出异常时仅记录日志并返回空列表，
    其余路结果照常参与融合，保证检索链路整体可用。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import jieba
import jieba.analyse
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

from src.knowledge.graph_builder import MemoryGraphStore
from src.retrieval.reranker import CrossEncoderReranker
from src.retrieval.vector_store import MemoryVectorStore, reciprocal_rank_fusion
from src.settings import get_settings

logger = logging.getLogger(__name__)

#: RRF 平滑常数，缺省 60（原论文推荐值）。
RRF_K: int = 60

#: RRF 融合后保留的候选数量上限。
RRF_TOP_K: int = 20

#: 图谱召回阶段用于提取关键词的 topK。
GRAPH_KEYWORD_TOP_K: int = 5

#: 三路召回并发执行时的线程池大小（向量 / BM25 / 图谱各占一个工作线程）。
RETRIEVAL_WORKERS: int = 3


class HybridRetriever:
    """向量 / BM25 / 知识图谱三路混合检索器。

    三路召回相互独立且各自具备异常隔离：任一路失败只返回空列表，
    不影响其余路参与 RRF 融合；图谱路结果经来源回填获得 chunk_id，
    从而与向量、BM25 两路在融合阶段处于同等地位。

    Attributes:
        embedding_model: 查询向量化模型，须与入库时的模型保持一致。
    """

    def __init__(
        self,
        vector_store: MemoryVectorStore,
        graph_store: MemoryGraphStore,
        embedding_model: SentenceTransformer,
        cross_encoder: CrossEncoder | None = None,
        model_name: str | None = None,
    ) -> None:
        """初始化混合检索器。

        Args:
            vector_store: 内存向量存储实例。
            graph_store: 内存知识图谱实例。
            embedding_model: 查询向量化模型，须与入库向量同源。
            cross_encoder: 可选的 Cross-Encoder 实例（依赖注入，便于测试与复用）；
                为 None 时按 model_name 现场加载到内存。
            model_name: 重排序模型名称，缺省读取全局配置 RERANKER_MODEL_NAME。

        Note:
            Cross-Encoder 加载失败不会中断构造：此时 rerank 自动降级为
            按 RRF 融合分数排序，检索链路整体仍然可用。
        """
        settings = get_settings()
        self._vector_store: MemoryVectorStore = vector_store
        self._graph_store: MemoryGraphStore = graph_store
        self._embedding_model: SentenceTransformer = embedding_model
        self._bm25: BM25Okapi | None = None
        self._chunk_map: dict[str, Document] = {}
        # 与 BM25 语料位置一一对应的 chunk_id 列表：
        # BM25Okapi.get_scores 返回的是整数位置，必须借此映射回 chunk_id，
        # 否则会以整数去索引以字符串为键的 _chunk_map 而触发 KeyError。
        self._chunk_ids: list[str] = []
        # 保护 (_bm25, _chunk_ids) 的一致性：重建与读取互斥，
        # 避免读者取到「新 chunk_ids + 旧 bm25」的错位组合。
        self._index_lock: threading.Lock = threading.Lock()
        self._model_name: str = (
            model_name if model_name is not None else settings.RERANKER_MODEL_NAME
        )
        # 重排序能力复用 src 层的 CrossEncoderReranker（消除双轨、单一实现来源）；
        # 加载失败不中断构造，rerank 自动降级为按 RRF 融合分数排序。
        self._reranker: CrossEncoderReranker = CrossEncoderReranker(
            model_name=self._model_name,
            top_k=settings.TOP_K_RERANK,
            model=cross_encoder,
        )
        try:
            self._reranker.load_model()
        except RuntimeError as exc:
            logger.error("重排序模型加载失败，rerank 将降级为 RRF 分数排序: %s", exc)
        self._cross_encoder: CrossEncoder | None = self._reranker.model
        # 三路召回共用同一个线程池：每次 retrieve 新建池的固定开销（实测约 0.39ms）
        # 在三路总耗时仅约 0.7ms 的量级下会吃掉全部并行收益，故实例级复用。
        self._pool_lock: threading.Lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None

    def build_bm25_index(self, chunks: list[Document]) -> None:
        """构建内存级 BM25 索引。

        Args:
            chunks: 分块文档列表，每块 metadata 须含 chunk_id；
                缺少 chunk_id 的分块会被跳过并记录告警。

        Note:
            chunk_id 列表与 BM25 语料按同一顺序构建，保证 get_scores 返回的
            整数位置可直接映射回 chunk_id。语料为空或全为空白文本时
            _bm25 置为 None，此时 bm25_search 返回空列表，其余两路不受影响。
        """
        valid_chunks = [chunk for chunk in chunks if chunk.metadata.get("chunk_id")]
        skipped = len(chunks) - len(valid_chunks)
        if skipped:
            logger.warning("BM25 索引跳过缺少 chunk_id 的分块: skipped=%d", skipped)

        if not valid_chunks:
            with self._index_lock:
                self._bm25 = None
                self._chunk_map = {}
                self._chunk_ids = []
            logger.warning("BM25 索引语料为空，关键词召回不可用")
            return

        # 全部分词与索引构建在锁外完成（只碰局部变量），临界区内仅做状态替换，
        # 使并发读者看到的状态要么整体是旧索引、要么整体是新索引。
        corpus_tokenized = [jieba.lcut(chunk.page_content) for chunk in valid_chunks]
        chunk_ids = [str(chunk.metadata["chunk_id"]) for chunk in valid_chunks]
        chunk_map = {
            str(chunk.metadata["chunk_id"]): chunk for chunk in valid_chunks
        }
        bm25: BM25Okapi | None = (
            BM25Okapi(corpus_tokenized) if any(corpus_tokenized) else None
        )

        with self._index_lock:
            self._bm25 = bm25
            self._chunk_ids = chunk_ids
            self._chunk_map = chunk_map

        if bm25 is None:
            logger.warning("BM25 语料全部为空白文本，关键词召回不可用")
            return

        logger.info("BM25 索引已构建: chunks=%d", len(chunk_ids))

    def vector_search(self, query: str, top_k: int = 15) -> list[tuple[str, float]]:
        """向量检索。

        Args:
            query: 查询文本。
            top_k: 返回的最大结果数，缺省 15。

        Returns:
            (chunk_id, 余弦相似度) 列表，按分数降序；失败或无命中时返回空列表。
        """
        try:
            query_embedding = self._embedding_model.encode(
                [query], normalize_embeddings=True
            )
            results = self._vector_store.search(query_embedding[0], top_k)
            return [
                (str(document.metadata["chunk_id"]), float(score))
                for document, score in results
                if document.metadata.get("chunk_id")
            ]
        except Exception as exc:  # noqa: BLE001 - 单路失败不得影响其他路
            logger.error("向量检索失败: %s", exc)
            return []

    def bm25_search(self, query: str, top_k: int = 15) -> list[tuple[str, float]]:
        """BM25 关键词检索。

        Args:
            query: 查询文本，经 jieba.lcut 分词后与内存语料做词频匹配。
            top_k: 返回的最大结果数，缺省 15。

        Returns:
            (chunk_id, BM25 分数) 列表，按分数降序；
            索引未构建或检索失败时返回空列表。
        """
        # 在同一把锁内一次性取走索引与标识列表，保证二者版本一致。
        with self._index_lock:
            bm25 = self._bm25
            chunk_ids = list(self._chunk_ids)
        if bm25 is None or not chunk_ids:
            return []
        try:
            query_tokens = jieba.lcut(query)
            scores = bm25.get_scores(query_tokens)
            top_indices = sorted(
                range(len(scores)), key=lambda index: float(scores[index]), reverse=True
            )[:top_k]
            return [
                (chunk_ids[index], float(scores[index]))
                for index in top_indices
                if 0 <= index < len(chunk_ids)
            ]
        except Exception as exc:  # noqa: BLE001 - 单路失败不得影响其他路
            logger.error("BM25 检索失败: %s", exc)
            return []

    def graph_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """知识图谱检索。

        Args:
            query: 查询文本，经 TF-IDF 关键词提取后匹配中心实体。
            top_k: 返回的最大结果数，缺省 5。

        Returns:
            图谱命中列表，每项在 MemoryGraphStore.search_by_keyword 返回的
            center_entity / neighbors / subgraph_text 之外，补充 chunk_id
            （首个来源分块，供 RRF 融合定位文档）与 chunk_ids（全部来源分块）；
            检索失败时返回空列表。

        Note:
            search_by_keyword 本身不返回 chunk_id，此处经 entity_sources 回填；
            若缺少该回填，图谱路在 RRF 融合中将恒为零贡献。
        """
        try:
            keywords = jieba.analyse.extract_tags(query, topK=GRAPH_KEYWORD_TOP_K)
            results: list[dict[str, Any]] = []
            seen_entities: set[str] = set()
            for keyword in keywords:
                for item in self._graph_store.search_by_keyword(keyword):
                    entity = item.get("center_entity")
                    if entity in seen_entities:
                        continue
                    seen_entities.add(entity)
                    source_chunk_ids = list(self._graph_store.entity_sources(entity))
                    enriched: dict[str, Any] = dict(item)
                    enriched["chunk_id"] = (
                        source_chunk_ids[0] if source_chunk_ids else None
                    )
                    enriched["chunk_ids"] = source_chunk_ids
                    results.append(enriched)
            return results[:top_k]
        except Exception as exc:  # noqa: BLE001 - 单路失败不得影响其他路
            logger.error("图谱检索失败: %s", exc)
            return []

    def rrf_fusion(
        self,
        vector_results: list[tuple[str, float]],
        bm25_results: list[tuple[str, float]],
        graph_results: list[dict[str, Any]],
        k: int = 60,
    ) -> list[tuple[str, float]]:
        """RRF 分数融合。

        融合公式为 score(chunk) = 各路 1 / (k + rank) 之和，rank 自 1 起。
        某一路结果为空时该路自然无贡献，其余路照常融合。

        Args:
            vector_results: 向量路 (chunk_id, 分数) 列表。
            bm25_results: BM25 路 (chunk_id, 分数) 列表。
            graph_results: 图谱路结果列表，取其中的 chunk_id 字段计入名次。
            k: RRF 平滑常数，缺省 60。

        Returns:
            按融合分数降序排列的 (chunk_id, 分数) 列表，
            长度不超过 RRF_TOP_K（20）。

        Raises:
            ValueError: k 非正数。

        Note:
            有序路（向量 / BM25）先按分数降序、按 chunk_id 去重（保留最高分）
            再分配名次，避免同一标识重复计分；图谱路按原始返回顺序分配名次。
        """
        # 融合算术复用 src 层的通用原语；本方法只负责把三路结果整形成
        # 「有序标识列表」：有序路按分数降序去重，图谱路保持原始返回顺序。
        ranked_lists: list[list[str]] = [
            [chunk_id for chunk_id, _ in self._dedup_ranked(vector_results)],
            [chunk_id for chunk_id, _ in self._dedup_ranked(bm25_results)],
            [
                str(item["chunk_id"])
                for item in graph_results
                if item.get("chunk_id")
            ],
        ]
        return reciprocal_rank_fusion(ranked_lists, k=k, top_k=RRF_TOP_K)

    def rerank(
        self,
        query: str,
        candidates: list[tuple[str, float]],
        top_k: int = 5,
    ) -> list[tuple[Document, float]]:
        """Cross-Encoder 重排序。

        Args:
            query: 查询文本。
            candidates: RRF 融合后的 (chunk_id, 分数) 候选列表。
            top_k: 精排后保留的文档数量，缺省 5。

        Returns:
            按相关性降序排列的 (Document, 分数) 列表，
            长度等于 min(有效候选数, top_k)；候选为空或全部无法定位到
            分块时返回空列表。

        Note:
            Cross-Encoder 不可用（加载失败或推理异常）时自动降级为按候选
            自带的 RRF 融合分数排序，返回条数语义保持不变。
        """
        chunk_map = self._chunk_map
        documents = [
            chunk_map[chunk_id]
            for chunk_id, _ in candidates
            if chunk_id in chunk_map
        ]
        if not documents:
            return []

        if self._cross_encoder is None:
            return self._rerank_fallback(candidates, top_k)

        try:
            pairs = [(query, document.page_content) for document in documents]
            scores = self._reranker.predict(pairs)
            scored_documents = sorted(
                zip(documents, scores),
                key=lambda item: item[1],
                reverse=True,
            )
            return scored_documents[:top_k]
        except Exception as exc:  # noqa: BLE001 - 推理失败降级而非中断
            logger.error("Cross-Encoder 重排序失败，降级为 RRF 分数排序: %s", exc)
            return self._rerank_fallback(candidates, top_k)

    def _rerank_fallback(
        self, candidates: list[tuple[str, float]], top_k: int
    ) -> list[tuple[Document, float]]:
        """重排序降级路径：按候选自带的融合分数排序。

        Args:
            candidates: RRF 融合后的 (chunk_id, 分数) 候选列表。
            top_k: 返回的文档数量上限。

        Returns:
            按融合分数降序排列的 (Document, 融合分数) 列表；
            无法定位到分块的候选被跳过。
        """
        chunk_map = self._chunk_map
        ordered = sorted(candidates, key=lambda item: float(item[1]), reverse=True)
        results: list[tuple[Document, float]] = []
        for chunk_id, score in ordered:
            document = chunk_map.get(chunk_id)
            if document is not None:
                results.append((document, float(score)))
        return results[:top_k]

    def _gather_sources(
        self, query: str, top_k: int
    ) -> tuple[list[tuple[str, float]], list[tuple[str, float]], list[dict[str, Any]]]:
        """并发执行三路召回。

        Args:
            query: 查询文本。
            top_k: 向量路与 BM25 路各自返回的最大条数。

        Returns:
            ``(向量结果, BM25 结果, 图谱结果)`` 三元组，顺序与串行调用一致。

        Note:
            三路各自内部已完成异常隔离，因此本方法不会因单路失败而抛出；
            ``future.result()`` 仅用于收集已就绪的结果。
        """
        pool = self._executor()
        vector_future = pool.submit(self.vector_search, query, top_k)
        bm25_future = pool.submit(self.bm25_search, query, top_k)
        graph_future = pool.submit(self.graph_search, query)
        return (
            vector_future.result(),
            bm25_future.result(),
            graph_future.result(),
        )

    def _executor(self) -> ThreadPoolExecutor:
        """返回实例级复用的召回线程池（惰性创建、加锁保证唯一）。

        Returns:
            供三路召回共用的 :class:`ThreadPoolExecutor`。

        Note:
            复用线程池可省去每次调用约 0.39ms 的创建开销——本地三路召回
            合计仅约 0.7ms，该固定开销足以让并发慢于串行，故必须复用。
        """
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=RETRIEVAL_WORKERS,
                    thread_name_prefix="retrieval",
                )
            return self._pool

    def close(self) -> None:
        """释放内部线程池。

        Note:
            不调用也会由解释器在进程退出时回收；显式调用便于长驻服务
            在停用时立刻释放线程资源。
        """
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
                self._pool = None

    def retrieve(self, query: str) -> list[tuple[Document, float]]:
        """主检索入口：三路并发召回 → RRF 融合 → Cross-Encoder 重排序。

        Args:
            query: 运维诊断问题。

        Returns:
            按相关性降序排列的 (Document, 分数) 列表，
            长度不超过配置项 TOP_K_RERANK（缺省 5）；全部召回为空时返回空列表。

        Note:
            三路召回经线程池并发执行且彼此异常隔离，任一路失败不会中断整体链路；
            融合顺序固定为「向量 → BM25 → 图谱」，结果与串行版本逐位一致。
        """
        settings = get_settings()
        vector_results, bm25_results, graph_results = self._gather_sources(
            query, settings.TOP_K_RETRIEVAL
        )
        fused = self.rrf_fusion(vector_results, bm25_results, graph_results)
        return self.rerank(query, fused, top_k=settings.TOP_K_RERANK)

    async def aretrieve(self, query: str) -> list[tuple[Document, float]]:
        """异步主检索入口：三路并发召回 → RRF 融合 → Cross-Encoder 重排序。

        与 :meth:`retrieve` 逻辑等价，供已在事件循环中的调用方（如 FastAPI 的
        SSE 诊断接口）直接 ``await``，避免在运行中的事件循环里再套一层
        ``asyncio.run`` 而抛 ``RuntimeError``。

        Args:
            query: 运维诊断问题。

        Returns:
            与 :meth:`retrieve` 完全一致的 ``(Document, 分数)`` 列表。

        Note:
            三路召回经 ``asyncio.to_thread`` 派发到工作线程，不阻塞事件循环；
            异常隔离与融合顺序均与同步版本相同。
        """
        settings = get_settings()
        vector_results, bm25_results, graph_results = await asyncio.gather(
            asyncio.to_thread(self.vector_search, query, settings.TOP_K_RETRIEVAL),
            asyncio.to_thread(self.bm25_search, query, settings.TOP_K_RETRIEVAL),
            asyncio.to_thread(self.graph_search, query),
        )
        fused = self.rrf_fusion(vector_results, bm25_results, graph_results)
        return self.rerank(query, fused, top_k=settings.TOP_K_RERANK)

    @staticmethod
    def _dedup_ranked(ranked: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """按 chunk_id 去重并按分数降序整理单路召回结果。

        Args:
            ranked: 单路召回的 (chunk_id, 分数) 列表。

        Returns:
            去重后的 (chunk_id, 分数) 列表，同一标识保留最高分；
            空标识会被丢弃。
        """
        best: dict[str, float] = {}
        for chunk_id, score in ranked:
            if not chunk_id:
                continue
            value = float(score)
            if chunk_id not in best or value > best[chunk_id]:
                best[chunk_id] = value
        return sorted(best.items(), key=lambda item: item[1], reverse=True)
