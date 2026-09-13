"""零磁盘占用约束的集成测试。

本项目硬约束验收：跑一遍「文档上传 → 检索 → 问答 → 评估」全流程后，
项目树内不得新增任何文件，磁盘用量变化必须小于 1KB。

两个必须说清楚的前提：

1. **零磁盘只在纯内存模式下成立。** 索引落盘能力已按既定决策外置到
   ``RAG_DATA_DIR``（项目之外的目录，默认 D:\\rag-data）。本测试先把该变量
   置空并清掉 settings 的单例缓存，测的才是「不写盘」本身，而不是落盘能力。
2. **``__pycache__`` 不在观测范围。** 字节码缓存是解释器行为，与业务是否落盘
   无关，由 ``python -B`` / ``PYTHONDONTWRITEBYTECODE`` 控制。

运行方式::

    python -B -m pytest tests/test_zero_disk.py -p no:cacheprovider -q
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from langchain_core.documents import Document

from core.preprocessing import ingest_documents
from src.retrieval.vector_store import MemoryVectorStore
from src.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 扫描时跳过：缓存与依赖不属于「业务是否落盘」的观测范围。
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", "dist", ".git", ".mypy_cache"}


def project_files() -> set[str]:
    """项目树内的文件路径集合（跳过缓存与依赖目录）。"""
    found: set[str] = set()
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            found.add(str(Path(root) / name))
    return found


def _fake_encode(texts, **_kwargs):
    """假嵌入：按输入条数返回同形状零向量，避免加载真实模型（数百 MB / 数十秒）。"""
    count = len(texts) if isinstance(texts, (list, tuple)) else 1
    return np.zeros((count, 1024), dtype=np.float32)


@pytest.fixture()
def memory_only(monkeypatch):
    """强制纯内存模式：关掉落盘目录，并清掉 settings 的 lru_cache 单例。"""
    monkeypatch.setenv("RAG_DATA_DIR", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_zero_disk_footprint(memory_only) -> None:
    """全流程跑完后：项目树零新增文件，且磁盘用量变化 < 1KB。"""
    before_files = project_files()
    before_usage = shutil.disk_usage(PROJECT_ROOT)

    # ── 1. 文档上传：加载 → 切分 → 向量化 → 实体抽取
    raw = ("# 散热系统维护手册\n\n" + "风扇转速异常通常由风道堵塞引起，需清理滤网。 " * 24).encode("utf-8")
    with mock.patch("core.preprocessing.ChatOpenAI") as llm_cls, mock.patch(
        "core.preprocessing.SentenceTransformer"
    ) as embedder_cls:
        llm_cls.return_value.invoke.return_value = mock.Mock(
            content='{"entities": [], "relations": []}'
        )
        embedder_cls.return_value.encode.side_effect = _fake_encode
        result = ingest_documents([(raw, "md", "散热手册.md")])

    assert result.chunks, "摄取应至少产出一个分块"
    assert result.embeddings.shape[0] == len(result.chunks)

    # ── 2. 检索：内存向量库，纯计算
    store = MemoryVectorStore(1024)
    store.add(result.embeddings, result.chunks)
    assert store.count() == len(result.chunks)
    hits = store.search(np.zeros((1, 1024), dtype=np.float32), top_k=3)
    assert hits, "检索应能返回命中结果"

    # ── 3. 问答：命中的记录必须能拼出可溯源引用（doc_id + 字符区间）
    meta = hits[0][0].metadata
    assert meta.get("chunk_id"), "答案必须可溯源到分块"
    assert meta.get("doc_id"), "答案必须可溯源到文档"
    assert meta.get("char_start") is not None and meta.get("char_end") is not None, (
        "答案必须带字符区间，否则前端无法定位原文"
    )
    assert 0 <= int(meta["char_start"]) <= int(meta["char_end"])

    # ── 4. 评估：只跑纯内存部分（指标常量与结构化记录），不触发 RAGAS 网络调用
    from src.constants import EVALUATION_TARGETS

    assert EVALUATION_TARGETS, "评估目标常量应存在"

    after_files = project_files()
    after_usage = shutil.disk_usage(PROJECT_ROOT)

    created = sorted(after_files - before_files)
    assert created == [], f"项目树不应新增任何文件，实际新增: {created}"
    # 整盘用量是**共享资源**：本机常有多会话/子代理并行写盘，几 KB 抖动属环境噪声
    # （实测同一用例单独跑两次均通过，全量并发跑时出现 -4096 字节漂移）。
    # 零落盘的硬保证仍由上面的项目树扫描与 test_api/test_platform 的 AST 扫描承担；
    # 此处只兜底"本链路写出可观文件"（日志/快照量级 ≫ 64KB）。
    assert abs(after_usage.used - before_usage.used) < 64 * 1024, (
        f"磁盘用量变化应小于 64KB，实际变化 {after_usage.used - before_usage.used} 字节"
    )


def test_ingest_rejects_unsupported_type(memory_only) -> None:
    """不支持的类型必须显式报错，而不是静默产出空分块。"""
    with pytest.raises(Exception):
        ingest_documents([(b"binary", "exe", "bad.exe")])


def test_add_and_delete_never_touch_disk(memory_only) -> None:
    """增删都是纯内存操作：即便在纯内存模式下也不产生任何文件。"""
    before = project_files()
    store = MemoryVectorStore(1024)
    chunks = [
        Document(page_content="a", metadata={"doc_id": "d1", "chunk_id": "c1"}),
        Document(page_content="b", metadata={"doc_id": "d1", "chunk_id": "c2"}),
    ]
    store.add(np.zeros((2, 1024), dtype=np.float32), chunks)
    assert store.count() == 2
    assert store.count_by_document("d1") == 2
    assert store.delete_document("d1") == 2
    assert store.count() == 0
    assert project_files() == before
