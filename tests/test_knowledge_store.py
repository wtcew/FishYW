"""内存向量库的文档级能力与持久化加固测试。

补这组的原因（来自独立审查）：

* 原 test_vector_store.py 对 list_documents / count_by_document / delete_document /
  all_documents / save / load **零用例**，而它的零磁盘 AST 扫描恰好把 save/load 列为
  豁免 —— 等于给自己开了个未测白名单；
* 并发 save 与坏快照是实测出过问题的：前者 120 轮里 119 轮产出混合快照，
  后者能让检索返回 score=1.0 的错误文本。

运行方式::

    python -B -m pytest tests/test_knowledge_store.py -p no:cacheprovider -q
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import numpy as np
import pytest
from langchain_core.documents import Document

from src.retrieval.vector_store import MemoryVectorStore

DIM = 8


def make_store(tag: str, count: int = 3) -> MemoryVectorStore:
    """构造一个带指定 doc_id 的库。"""
    store = MemoryVectorStore(DIM)
    chunks = [
        Document(
            page_content="%s-%d" % (tag, i),
            metadata={"doc_id": tag, "chunk_id": "%s-%d" % (tag, i), "filename": tag + ".md"},
        )
        for i in range(count)
    ]
    store.add(np.random.rand(count, DIM).astype(np.float32), chunks)
    return store


def test_list_documents_aggregates_by_doc_id() -> None:
    """按 doc_id 聚合，chunks 数由分块实时计数得出。"""
    store = make_store("A", 2)
    other = make_store("B", 3)
    store.add(np.random.rand(3, DIM).astype(np.float32), other.all_documents())
    docs = {d["doc_id"]: d for d in store.list_documents()}
    assert docs["A"]["chunks"] == 2
    assert docs["B"]["chunks"] == 3
    assert store.count() == 5


def test_count_by_document() -> None:
    """按文档计数与全量计数一致。"""
    store = make_store("A", 4)
    assert store.count_by_document("A") == 4
    assert store.count_by_document("nope") == 0


def test_delete_document_removes_only_target() -> None:
    """删除只影响目标文档，且删除后检索不再命中它。"""
    store = make_store("A", 2)
    store.add(np.random.rand(2, DIM).astype(np.float32), make_store("B", 2).all_documents())
    removed = store.delete_document("A")
    assert removed == 2
    assert store.count_by_document("A") == 0
    assert store.count_by_document("B") == 2
    assert store.count() == 2
    hits = store.search(np.random.rand(1, DIM).astype(np.float32), top_k=10)
    assert all(hit[0].metadata["doc_id"] == "B" for hit in hits)


def test_delete_missing_document_returns_zero() -> None:
    """删不存在的文档返回 0，不抛异常。"""
    store = make_store("A", 2)
    assert store.delete_document("nope") == 0
    assert store.count() == 2


def test_all_documents_returns_snapshot_copy() -> None:
    """all_documents 返回副本，改动它不影响库内状态。"""
    store = make_store("A", 2)
    snapshot = store.all_documents()
    snapshot.clear()
    assert store.count() == 2


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    """落盘后能原样恢复（含文档内容与条数）。"""
    store = make_store("A", 3)
    store.save(str(tmp_path))
    restored = MemoryVectorStore(DIM)
    assert restored.load(str(tmp_path)) is True
    assert restored.count() == 3
    assert [c.page_content for c in restored.all_documents()] == [c.page_content for c in store.all_documents()]


def test_load_rejects_missing_files(tmp_path: Path) -> None:
    """目录为空或缺文件时必须拒绝，而不是当作空库。"""
    assert MemoryVectorStore(DIM).load(str(tmp_path)) is False


def test_load_rejects_dimension_mismatch(tmp_path: Path) -> None:
    """维度不符的快照必须拒绝。"""
    make_store("A", 2).save(str(tmp_path))
    assert MemoryVectorStore(DIM * 2).load(str(tmp_path)) is False


def test_load_rejects_missing_meta(tmp_path: Path) -> None:
    """缺 meta.json 的快照必须拒绝 —— 没有指纹就无法判断索引与文档是否同源。"""
    make_store("A", 2).save(str(tmp_path))
    os.remove(str(tmp_path / "meta.json"))
    assert MemoryVectorStore(DIM).load(str(tmp_path)) is False


def test_load_rejects_tampered_chunks(tmp_path: Path) -> None:
    """篡改 chunks.json 后指纹不符，必须拒绝而非静默装载错配数据。"""
    make_store("A", 3).save(str(tmp_path))
    chunk_path = tmp_path / "chunks.json"
    payload = json.loads(chunk_path.read_text(encoding="utf-8"))
    payload[0]["page_content"] = "tampered"
    chunk_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert MemoryVectorStore(DIM).load(str(tmp_path)) is False


def test_load_rejects_count_mismatch(tmp_path: Path) -> None:
    """meta 条数与索引不符必须拒绝。"""
    make_store("A", 3).save(str(tmp_path))
    meta_path = tmp_path / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["count"] = 999
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    assert MemoryVectorStore(DIM).load(str(tmp_path)) is False


def test_failed_load_keeps_current_state(tmp_path: Path) -> None:
    """恢复失败不得破坏当前状态（宁可保持现状也不要半截数据）。"""
    live = make_store("LIVE", 2)
    assert live.load(str(tmp_path)) is False
    assert live.count() == 2
    assert [c.metadata["doc_id"] for c in live.all_documents()] == ["LIVE", "LIVE"]


def test_concurrent_save_never_mixes_sources(tmp_path: Path) -> None:
    """并发 save 不得产出「索引来自 A、文档来自 B」的混合快照。

    修复前实测 120 轮里 119 轮错配：faiss 写入在锁内、json 写入在锁外。
    这里用同源断言检出——恢复出来的文档必须全部来自同一个库。
    """
    a = make_store("A", 3)
    b = make_store("B", 3)
    for _ in range(10):
        threads = [
            threading.Thread(target=store.save, args=(str(tmp_path),))
            for store in (a, b)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        probe = MemoryVectorStore(DIM)
        # 安全属性是「要么同源、要么明确拒绝」，而不是「必然同源」：
        # 两个 store 实例各有自己的锁，跨实例写同一目录仍可能交错；
        # 此时 meta.json 的内容指纹会判定不一致并让 load 返回 False ——
        # 宁可拒绝装载，也绝不返回索引与文档错配的库。
        if not probe.load(str(tmp_path)):
            continue
        owners = {c.metadata["doc_id"] for c in probe.all_documents()}
        assert len(owners) == 1, "装载成功却出现混合快照: %s" % owners
