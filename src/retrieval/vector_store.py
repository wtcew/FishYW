"""内存向量存储。

以 ``faiss.IndexFlatIP`` 承载向量索引，配合 L2 归一化实现余弦相似度检索。
索引与文档列表常驻内存、进程退出即释放，全程零磁盘写入。

Note:
    该模块是阶段二「内存存储层」的向量部分；
    :func:`reciprocal_rank_fusion` 是混合检索使用的通用融合原语。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any

import faiss
import numpy as np
from langchain_core.documents import Document

logger = logging.getLogger(__name__)


def document_key(metadata: dict[str, Any] | None) -> str:
    """算出一个分块所属的文档键。

    优先 doc_id；历史数据没有 doc_id 时退回文件名；两者都没有的孤儿分块用
    ``chunk:<chunk_id>`` 兜底 —— 必须有唯一键，否则这类行在界面上既无法区分、
    也永远删不掉（旧实现统一塞进哨兵 "unknown"，删除时匹配 0 条直接 404）。
    """
    meta = metadata or {}
    own = str(meta.get("doc_id") or "")
    if own:
        return own
    name = str(meta.get("filename") or "")
    if name:
        return name
    return "chunk:" + str(meta.get("chunk_id") or "orphan")


def document_matches(metadata: dict[str, Any] | None, key: str) -> bool:
    """判断分块是否属于指定文档键。

    列表、计数、分块查询、删除四处必须共用这一个判定，否则会出现
    「列表显示 4 块、计数返回 0、分块接口返回空」的自相矛盾。
    """
    return document_key(metadata) == key


class MemoryVectorStore:
    """线程安全的内存向量存储。

    使用内积索引（``IndexFlatIP``）配合 L2 归一化：入库与查询前都对向量做
    L2 归一化，因此内积等价于余弦相似度，分数范围 ``[-1, 1]``。

    Attributes:
        embedding_dim: 向量维度，须与 Embedding 模型输出一致。
    """

    def __init__(self, embedding_dim: int) -> None:
        """初始化向量存储。

        Args:
            embedding_dim: 向量维度，必须为正整数。

        Raises:
            ValueError: ``embedding_dim`` 非正数。

        Note:
            全部状态由 ``_index`` / ``_documents`` / ``_lock`` 三个私有属性构成，
            均驻留内存，不产生任何持久化文件。
        """
        if embedding_dim <= 0:
            raise ValueError(f"embedding_dim must be positive, got {embedding_dim}")
        self.embedding_dim: int = embedding_dim
        self._index: faiss.IndexFlatIP = faiss.IndexFlatIP(embedding_dim)
        self._documents: list[Document] = []
        self._lock: threading.Lock = threading.Lock()

    def add(self, embeddings: np.ndarray, chunks: list[Document]) -> None:
        """批量写入向量及其对应文档。

        Args:
            embeddings: 形状为 ``(n, embedding_dim)`` 的向量矩阵；
                非 ``float32`` 会被就地转换，非连续内存会被复制（faiss 要求）。
            chunks: 与向量一一对应的文档列表，长度须与 ``embeddings`` 一致。

        Raises:
            AssertionError: 向量条数与文档条数不一致。
            ValueError: 向量维度与初始化维度不符。

        Note:
            归一化与类型转换在锁外完成（纯本地计算，无共享状态），
            索引写入与文档追加在同一锁内完成，确保两者始终一一对应。

        Warning:
            当输入矩阵已是 ``float32`` 且内存连续时，``faiss.normalize_L2``
            会**就地修改调用方数组**；若调用方需保留原始向量，请自行先复制。
        """
        assert len(embeddings) == len(chunks), "Embeddings and chunks count mismatch"

        embeddings = np.ascontiguousarray(embeddings, dtype=np.float32)
        if embeddings.ndim != 2 or embeddings.shape[1] != self.embedding_dim:
            raise ValueError(
                f"embeddings shape {embeddings.shape} incompatible with "
                f"embedding_dim={self.embedding_dim}"
            )
        faiss.normalize_L2(embeddings)

        with self._lock:
            self._index.add(embeddings)
            self._documents.extend(chunks)
        logger.info("向量入库: added=%d total=%d", len(chunks), self._index.ntotal)

    def search(
        self, query_embedding: np.ndarray, top_k: int = 15
    ) -> list[tuple[Document, float]]:
        """按余弦相似度检索最相近的文档。

        Args:
            query_embedding: 查询向量，形状为 ``(embedding_dim,)`` 或
                ``(1, embedding_dim)``；内部会转 ``float32`` 并 reshape 为二维。
            top_k: 返回的最大结果数，缺省 15。

        Returns:
            按相似度严格降序排列的 ``(文档, 分数)`` 列表；
            索引为空时返回空列表。

        Note:
            实际检索条数为 ``min(top_k, 索引条数)``，因此不会返回哨兵项。

        Note:
            向量检索与文档回溯必须在同一把锁内完成：若在锁外读取
            ``_documents``，并发的 ``clear()`` 会造成 ``IndexError`` 或
            文档错位（陈旧索引领用被清空后的列表）。
        """
        query_embedding = query_embedding.astype(np.float32).reshape(1, -1)
        faiss.normalize_L2(query_embedding)

        with self._lock:
            if self._index.ntotal == 0:
                return []
            scores, indices = self._index.search(
                query_embedding, min(top_k, self._index.ntotal)
            )
            results = [
                (self._documents[idx], float(score))
                for idx, score in zip(indices[0], scores[0])
                if idx != -1
            ]

        results.sort(key=lambda item: item[1], reverse=True)
        return results

    def list_documents(self) -> list[dict[str, Any]]:
        """按文档聚合出文档级视图（供知识库管理界面展示）。

        Returns:
            每项含 doc_id / kb_id / filename / file_type / chunks / chars / char_length，
            按文件名排序。chunks 一律由分块实时计数得出，不维护独立计数器，
            从根上避免「界面显示 3 块、库里其实 0 块」这类口径不一致。
        """
        with self._lock:
            snapshot = list(self._documents)
        aggregated: dict[str, dict[str, Any]] = {}
        for chunk in snapshot:
            meta = chunk.metadata or {}
            key = document_key(meta)
            item = aggregated.setdefault(
                key,
                {
                    "doc_id": key,
                    "kb_id": str(meta.get("kb_id") or "default"),
                    "filename": str(meta.get("filename") or ""),
                    "file_type": str(meta.get("file_type") or ""),
                    "chunks": 0,
                    "chars": 0,
                    "char_length": int(meta.get("char_length") or 0),
                    # 标记历史数据：早期入库的分块没有 doc_id，只能靠文件名兜底。
                    # 前端据此提示用户重新上传补齐溯源字段，而不是让条目默默删不掉。
                    "legacy": not bool(meta.get("doc_id")),
                },
            )
            item["chunks"] += 1
            item["chars"] += len(chunk.page_content)
        return sorted(aggregated.values(), key=lambda entry: entry["filename"])

    def count_by_document(self, doc_id: str) -> int:
        """统计指定文档当前的分块数（与列表、删除共用同一套匹配规则）。"""
        with self._lock:
            return sum(
                1 for chunk in self._documents if document_matches(chunk.metadata, doc_id)
            )

    def delete_document(self, doc_id: str) -> int:
        """删除某文档的全部分块并重建索引。

        Args:
            doc_id: 文档标识。

        Returns:
            实际删除的分块数；文档不存在时返回 0。

        Note:
            faiss 的 ``IndexFlatIP`` 不支持按下标删除，因此走「取回保留向量 →
            建新索引 → 整体替换」。全过程在同一把锁内完成，避免并发检索读到
            「索引已换、文档未换」的错位状态 —— 那会让向量配上无关文本。
            取回的是入库时已归一化的向量，重建后余弦语义保持一致。
        """
        def belongs_to(chunk: Document) -> bool:
            """判断分块是否属于待删文档（与列表、计数共用同一套匹配规则）。"""
            return document_matches(chunk.metadata, doc_id)

        with self._lock:
            keep_mask = [not belongs_to(chunk) for chunk in self._documents]
            removed = keep_mask.count(False)
            if removed == 0:
                return 0
            kept_documents = [
                chunk for chunk, keep in zip(self._documents, keep_mask) if keep
            ]
            new_index = faiss.IndexFlatIP(self.embedding_dim)
            if kept_documents:
                kept_vectors = np.vstack(
                    [
                        self._index.reconstruct(position)
                        for position, keep in enumerate(keep_mask)
                        if keep
                    ]
                ).astype(np.float32)
                new_index.add(kept_vectors)
            self._index = new_index
            self._documents = kept_documents
        logger.info(
            "已删除文档: doc_id=%s removed=%d remain=%d",
            doc_id,
            removed,
            len(kept_documents),
        )
        return removed

    def save(self, directory: str) -> int:
        """把索引与文档落盘到指定目录（目录由调用方决定，可为项目外路径）。

        Args:
            directory: 目标目录，不存在会自动创建。

        Returns:
            实际写入的文档条数。

        Note:
            faiss 索引与文档列表必须同源同序，否则恢复后会用错误文本解释向量；
            因此两者在同一把锁内快照，且先写索引再写文档，最后写 meta 作为提交标记。
        """
        os.makedirs(directory, exist_ok=True)
        # 整个写盘过程都在同一把锁内。曾经把 json 写入留在锁外，实测两路并发
        # save（上传后 / 删除后各走一次 asyncio.to_thread）在 120 轮里有 119 轮
        # 产出「索引来自 A、文档来自 B」的混合快照 —— 条数还一样，load 根本挡不住。
        with self._lock:
            index_path = os.path.join(directory, "vectors.faiss")
            chunk_path = os.path.join(directory, "chunks.json")
            meta_path = os.path.join(directory, "meta.json")
            faiss.write_index(self._index, index_path)
            payload = [
                {"page_content": item.page_content, "metadata": item.metadata or {}}
                for item in self._documents
            ]
            total = len(payload)
            text = json.dumps(payload, ensure_ascii=False)
            with open(chunk_path, "w", encoding="utf-8") as handle:
                handle.write(text)
        # meta.json 里存内容指纹：写入它在锁内完成，读取它则可判定
        # 「索引与文档是否同源」。没有指纹时，同条数的坏快照会被静默装载，
        # 检索随即返回 score=1.0 的错误文本 —— 比直接报错危险得多。
        with open(os.path.join(directory, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "embedding_dim": self.embedding_dim,
                    "count": total,
                    "chunks_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                },
                handle,
                ensure_ascii=False,
            )
        logger.info("向量库已落盘: dir=%s count=%d", directory, total)
        return total

    def load(self, directory: str) -> bool:
        """从目录恢复索引与文档。

        Args:
            directory: 之前 :meth:`save` 写入的目录。

        Returns:
            恢复成功返回 True；目录不存在、维度不符或文件损坏均返回 False，
            且**不改变当前状态** —— 启动时宁可空库也不要半截数据。
        """
        index_path = os.path.join(directory, "vectors.faiss")
        chunk_path = os.path.join(directory, "chunks.json")
        meta_path = os.path.join(directory, "meta.json")
        if not (
            os.path.exists(index_path)
            and os.path.exists(chunk_path)
            and os.path.exists(meta_path)
        ):
            return False
        try:
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
            index = faiss.read_index(index_path)
            # 删除文档依赖 reconstruct 取回保留向量；索引类型不支持时必须拒绝装载，
            # 否则库能读、却一删就炸。
            if not callable(getattr(index, "reconstruct", None)):
                logger.warning("索引类型不支持 reconstruct，拒绝装载: %s", type(index))
                return False
            if int(meta.get("count", -1)) != int(index.ntotal):
                logger.warning(
                    "meta 与索引条数不符，忽略该快照: meta=%s ntotal=%d",
                    meta.get("count"),
                    int(index.ntotal),
                )
                return False
            with open(chunk_path, encoding="utf-8") as handle:
                chunk_text = handle.read()
            if hashlib.sha256(chunk_text.encode("utf-8")).hexdigest() != meta.get(
                "chunks_sha256"
            ):
                logger.warning("chunks.json 指纹不符，判定为混合快照并忽略: dir=%s", directory)
                return False
            if index.d != self.embedding_dim:
                logger.warning(
                    "向量库维度不符，忽略该快照: file=%d expected=%d",
                    index.d,
                    self.embedding_dim,
                )
                return False
            with open(chunk_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            documents = [
                Document(page_content=item["page_content"], metadata=item.get("metadata") or {})
                for item in raw
            ]
            if len(documents) != int(index.ntotal):
                logger.warning(
                    "向量库与文档条数不一致，忽略该快照: ntotal=%d docs=%d",
                    int(index.ntotal),
                    len(documents),
                )
                return False
            with self._lock:
                self._index = index
                self._documents = documents
            logger.info("向量库已恢复: dir=%s count=%d", directory, len(documents))
            return True
        except Exception as exc:  # noqa: BLE001 - 损坏快照不得阻断启动
            logger.error("向量库恢复失败，按空库启动: %s", exc)
            return False

    def all_documents(self) -> list[Document]:
        """返回当前索引内的全部文档（副本列表，顺序与索引位置一致）。

        Returns:
            文档列表；调用方修改该列表不会影响内部状态，但元素本身是共享引用。

        Note:
            恢复快照后必须用它重建 BM25 索引与 chunk 映射 —— 那两者不随向量一起
            持久化，不重建会导致向量路能召回、却因缺映射而在融合阶段丢结果。
        """
        with self._lock:
            return list(self._documents)

    def count(self) -> int:
        """返回索引中的向量条数。

        Returns:
            当前索引内的向量总数。
        """
        with self._lock:
            return int(self._index.ntotal)

    def clear(self) -> None:
        """清空索引与文档列表，保留原向量维度。

        Note:
            通过重建 ``IndexFlatIP(dim)`` 释放旧索引占用的内存，
            不使用任何磁盘落盘方式。
        """
        with self._lock:
            dim = self._index.d
            self._index = faiss.IndexFlatIP(dim)
            self._documents.clear()
        logger.info("向量存储已清空: dim=%d", dim)


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = 60,
    top_k: int = 20,
) -> list[tuple[str, float]]:
    """融合多路召回结果。

    融合公式为 ``score = Σ 1 / (k + rank)``。

    Args:
        ranked_lists: 多路召回的有序结果列表，每路按名次升序排列。
        k: RRF 平滑常数，缺省 60。
        top_k: 融合后返回的结果数量。

    Returns:
        按融合分数严格降序排列的 ``(文档标识, 分数)`` 列表。

    Raises:
        ValueError: 当 ``k`` 或 ``top_k`` 非正数时。

    Note:
        本函数是混合检索的**通用原语**，不依赖向量索引本身；
        三路召回编排放 :mod:`core.retrieval`。单路之内的重复标识只按
        首次出现的名次计分一次，空标识会被跳过且不占用名次。
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    if top_k <= 0:
        raise ValueError(f"RRF top_k must be positive, got {top_k}")

    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        seen: set[str] = set()
        rank = 0
        for doc_id in ranked:
            if not doc_id or doc_id in seen:
                continue
            seen.add(doc_id)
            rank += 1
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
