"""字符区间（char_start/char_end）正确性测试。

这两个字段是「引用可定位」的唯一依据，一旦算错就是**静默错误**：
内容仍能对上（区间里取出的子串恰好等于分块正文），只有位置是错的，前端高亮会指向无关段落。

因此断言一律使用**外部不变量**，刻意不用 text[start:end] == content ——
那种自洽断言在重复文本下恒为真，正是它让上一版的错位缺陷全绿通过。

运行方式::

    python -B -m pytest tests/test_span_offsets.py -p no:cacheprovider -q
"""

from __future__ import annotations

from collections import defaultdict

from langchain_core.documents import Document

from core.preprocessing import load_document, split_documents
from src.settings import get_settings


def build(text: str, name: str = "doc.md") -> tuple[Document, list[Document]]:
    """加载并切分，返回原文与分块。"""
    doc = load_document(text.encode("utf-8"), "md", name)
    return doc, split_documents([doc])


def covered(chunks: list[Document]) -> set[int]:
    """所有分块字符区间的并集 —— 位置错了这个集合就会露馅。"""
    out: set[int] = set()
    for chunk in chunks:
        start = chunk.metadata.get("char_start")
        end = chunk.metadata.get("char_end")
        assert start is not None and end is not None, "每个分块都必须带字符区间"
        out.update(range(int(start), int(end)))
    return out


def test_unique_text_covers_entire_document() -> None:
    """唯一文本：区间并集覆盖全文，一块不漏。"""
    text = "".join("S%04d." % i for i in range(200))
    doc, chunks = build(text)
    assert covered(chunks) == set(range(len(doc.page_content)))


def test_repeated_text_covers_entire_document() -> None:
    """重复文本：上一版 find 定位法失败的场景（尾部 883 字符无覆盖）。

    find 只取最早匹配，游标又没推进到块终点，后几块区间全退回开头。
    覆盖并集是外部可观测的，位置错了必然暴露。
    """
    text = "REPEAT." * 200
    doc, chunks = build(text)
    assert covered(chunks) == set(range(len(doc.page_content)))


def test_project_like_repetitive_text_covers_document() -> None:
    """项目测试同款重复句：尾部未覆盖字符应接近 0。"""
    text = "风扇转速异常通常由风道堵塞引起，需清理滤网。 " * 24
    doc, chunks = build(text)
    missing = set(range(len(doc.page_content))) - covered(chunks)
    assert len(missing) <= 2, "尾部未覆盖字符过多: %d" % len(missing)


def test_starts_are_monotonic_and_within_bounds() -> None:
    """起点单调不减，且都落在原文范围内。"""
    text = "REPEAT." * 200
    doc, chunks = build(text)
    starts = [int(c.metadata["char_start"]) for c in chunks]
    assert starts == sorted(starts), "同文档内起点不得回退"
    assert all(0 <= s < len(doc.page_content) for s in starts)


def test_span_length_matches_content_and_chunk_size() -> None:
    """区间长度等于正文长度，且不超过配置的 chunk_size。"""
    text = "".join("S%04d." % i for i in range(200))
    _, chunks = build(text)
    limit = get_settings().CHUNK_SIZE
    for chunk in chunks:
        start = int(chunk.metadata["char_start"])
        end = int(chunk.metadata["char_end"])
        assert end - start == len(chunk.page_content)
        assert end - start <= limit, "分块长度不得超过 chunk_size"


def test_overlap_is_close_to_configured_value() -> None:
    """相邻分块重叠量应接近 CHUNK_OVERLAP，不得退化成巨大重叠。

    上一版在重复文本下 overlap 达 504/511（配置 50 的十倍），这种量级异常
    用覆盖并集看不出来，必须单独断言。
    """
    text = "REPEAT." * 200
    _, chunks = build(text)
    configured = get_settings().CHUNK_OVERLAP
    overlaps = [
        int(prev.metadata["char_end"]) - int(cur.metadata["char_start"])
        for prev, cur in zip(chunks, chunks[1:])
    ]
    assert overlaps, "至少需要两个分块才能检查重叠"
    for value in overlaps:
        assert value <= configured + 2, "重叠量 %d 远超配置 %d" % (value, configured)
        assert value >= 0, "相邻分块不得出现负重叠（区间倒挂）"


def test_two_documents_do_not_share_spans() -> None:
    """两块文档同时切分时区间不得串台。"""
    a = load_document(("AAA." * 300).encode("utf-8"), "md", "a.md")
    b = load_document(("BBB." * 300).encode("utf-8"), "md", "b.md")
    chunks = split_documents([a, b])
    by_doc: dict[str, list[Document]] = defaultdict(list)
    for chunk in chunks:
        by_doc[str(chunk.metadata.get("doc_id"))].append(chunk)
    assert len(by_doc) == 2
    for doc_id, group in by_doc.items():
        source = a.page_content if doc_id == a.metadata["doc_id"] else b.page_content
        assert max(covered(group)) < len(source), "区间越界，定位串到了另一份文档"
