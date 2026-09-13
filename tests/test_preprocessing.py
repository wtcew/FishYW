"""``core.preprocessing`` 模块测试（pytest）。

覆盖用户验收要求的六类用例：

1. **test_load_pdf** —— 构造 PDF 字节流，验证 ``Document`` 返回与 ``page_count``；
2. **test_load_markdown** —— 构造 MD 字节流，验证 ``Document`` 返回与 ``paragraph_count``；
3. **test_load_docx** —— 构造 DOCX 字节流，验证 ``Document`` 返回与 ``paragraph_count``；
4. **test_clean_text** —— 控制字符、页眉页脚、多余换行的清洗结果；
5. **test_split_documents** —— chunk 数量、``chunk_id`` 唯一性、``chunk_index`` 连续性；
6. **test_ingest_documents_flow** —— 多格式集成测试，验证 ``IngestResult`` 结构完整。

另附加边界与契约用例：类型校验、异常包装、embedding 缓存单例、
LLM 配置传递与非法 JSON 处理（全部 mock，不做真实推理、不落盘）。

运行方式::

    python -m pytest tests/test_preprocessing.py -p no:cacheprovider -q

``-p no:cacheprovider`` 禁用 ``.pytest_cache`` 写入，保持零磁盘约束。
"""

from __future__ import annotations

import io
import uuid
from unittest import mock

import numpy as np
import pytest
from docx import Document as DocxDocument
from langchain_core.documents import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import core.preprocessing as preprocessing
from core.preprocessing import (
    ENTITY_BATCH_SIZE,
    SPLIT_SEPARATORS,
    IngestError,
    IngestResult,
    clean_text,
    embed_chunks,
    extract_entities,
    ingest_documents,
    load_document,
    split_documents,
)
from src.settings import get_settings


# ═══════════ 内存构造工具（全程 BytesIO，不落盘） ═══════════


def _make_pdf_bytes(pages: int = 1) -> bytes:
    """在内存中构造含真实文本层的 PDF。

    Args:
        pages: 页数。

    Returns:
        PDF 字节流，每页含一行可被 ``extract_text`` 提取的文本。
    """
    writer = PdfWriter()
    for index in range(pages):
        writer.add_blank_page(width=612, height=792)
        page = writer.pages[index]
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 24 Tf 72 700 Td (Ops fault page {index + 1}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    """在内存中构造 DOCX 字节流。

    Args:
        paragraphs: 段落文本列表。

    Returns:
        DOCX 字节流。
    """
    document = DocxDocument()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _long_document() -> Document:
    """构造足够长、可切成多块的中文文档。"""
    return Document(
        page_content="设备频繁重启故障排查流程。".replace("。", "。\n") * 120,
        metadata={"filename": "long.md", "file_type": "md"},
    )


@pytest.fixture(autouse=True)
def _reset_embedder_cache():
    """每个用例前后清空 embedding 模型缓存，隔离 mock 注入。"""
    preprocessing._get_embedder.cache_clear()
    yield
    preprocessing._get_embedder.cache_clear()


# ═══════════ 验收用例 1-3：load_document 三种格式 ═══════════


class TestLoadDocument:
    """load_document：pdf / md / docx 三种格式加载。"""

    def test_load_pdf(self) -> None:
        """PDF 字节流应返回 Document 且 metadata 含 page_count。"""
        document = load_document(_make_pdf_bytes(pages=3), "pdf", "manual.pdf")

        assert isinstance(document, Document)
        assert document.metadata["page_count"] == 3
        assert document.metadata["filename"] == "manual.pdf"
        assert document.metadata["file_type"] == "pdf"
        assert "Ops fault page 1" in document.page_content
        assert "Ops fault page 3" in document.page_content

    def test_load_markdown(self) -> None:
        """MD 字节流应返回 Document 且 metadata 含 paragraph_count。"""
        raw = "# 标题\n\n第一段内容\n\n第二段内容".encode("utf-8")

        document = load_document(raw, "md", "note.md")

        assert isinstance(document, Document)
        assert document.metadata["paragraph_count"] == 3
        assert document.metadata["filename"] == "note.md"
        assert "第一段内容" in document.page_content

    def test_load_docx(self) -> None:
        """DOCX 字节流应返回 Document 且 metadata 含 paragraph_count。"""
        raw = _make_docx_bytes(["设备重启故障", "电源模块老化", "风扇转速异常"])

        document = load_document(raw, "docx", "report.docx")

        assert isinstance(document, Document)
        assert document.metadata["paragraph_count"] == 3
        assert "设备重启故障" in document.page_content
        assert "风扇转速异常" in document.page_content

    def test_file_type_is_case_insensitive_with_dot(self) -> None:
        """file_type 应大小写不敏感且容忍前导点。"""
        document = load_document(b"# t\n\np", ".MD", "x.md")
        assert document.metadata["file_type"] == "md"

    def test_unsupported_type_raises_value_error(self) -> None:
        """不支持的类型应抛 ValueError。"""
        with pytest.raises(ValueError, match="Unsupported file type"):
            load_document(b"plain", "txt", "a.txt")

    @mock.patch("core.preprocessing.PdfReader", side_effect=RuntimeError("corrupted stream"))
    def test_pdf_parse_failure_wrapped_as_ingest_error(self, _mock_reader: mock.Mock) -> None:
        """PDF 底层解析异常应包装为 IngestError 且带文件名。"""
        with pytest.raises(IngestError, match="Failed to load broken.pdf"):
            load_document(b"broken", "pdf", "broken.pdf")

    @mock.patch("core.preprocessing.DocxDocument", side_effect=RuntimeError("bad zip"))
    def test_docx_parse_failure_wrapped_as_ingest_error(self, _mock_docx: mock.Mock) -> None:
        """DOCX 底层解析异常同样应包装为 IngestError。"""
        with pytest.raises(IngestError, match="Failed to load broken.docx"):
            load_document(b"broken", "docx", "broken.docx")

    def test_invalid_utf8_markdown_wrapped_as_ingest_error(self) -> None:
        """MD 非 UTF-8 字节流应包装为 IngestError。"""
        with pytest.raises(IngestError, match="Failed to load bad.md"):
            load_document(b"\xff\xfe\x00", "md", "bad.md")


# ═══════════ 验收用例 4：clean_text ═══════════


class TestCleanText:
    """clean_text：控制字符、页眉页脚、多余换行。"""

    def test_clean_text(self) -> None:
        """含控制字符、中英文页眉页脚、多余换行的文本应被完整清洗。"""
        raw = (
            "第 1 页 共 10 页\n"
            "  设备\x00频繁\x07重启  \r\n"
            "Page 2 of 10\n"
            "\n\n\n\n"
            "散热风道堵塞排查…"
        )

        cleaned = clean_text(raw)

        assert "\x00" not in cleaned
        assert "\x07" not in cleaned
        assert "\r" not in cleaned
        assert "共" not in cleaned
        assert "Page" not in cleaned
        assert "设备频繁重启" in cleaned
        assert "散热风道堵塞排查…" in cleaned
        assert "\n\n\n" not in cleaned
        assert not any(line != line.strip() for line in cleaned.split("\n"))

    def test_crlf_and_cr_normalized(self) -> None:
        """\\r 与 \\r\\n 应统一为 \\n。"""
        assert clean_text("a\rb") == "a\nb"
        assert clean_text("a\r\nb") == "a\nb"

    def test_control_characters_removed_but_newline_kept(self) -> None:
        """控制字符应被剔除，换行保留。"""
        assert clean_text("a\x00b\x07c\x1fd") == "abcd"
        assert "\n" in clean_text("a\nb")

    def test_page_footers_case_insensitive(self) -> None:
        """中英文页眉页脚去除应大小写不敏感。"""
        assert "page" not in clean_text("body\npage 3 of 9").lower()
        assert "第" not in clean_text("正文\n第12页共30页\n尾")


# ═══════════ 验收用例 5：split_documents ═══════════


class TestSplitDocuments:
    """split_documents：chunk 数量、chunk_id 唯一性、chunk_index 连续性。"""

    def test_split_documents(self) -> None:
        """长文本应切出多块，chunk_id 唯一且 chunk_index 从 0 连续。"""
        chunks = split_documents([_long_document()])

        assert len(chunks) > 1
        identifiers = [chunk.metadata["chunk_id"] for chunk in chunks]
        assert len(identifiers) == len(set(identifiers))
        assert all(uuid.UUID(value).version == 4 for value in identifiers)
        assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))

    def test_chunk_count_grows_with_content(self) -> None:
        """内容翻倍时块数应增加（验证真实切分而非单块兜底）。"""
        short = split_documents([Document(page_content="短文本。", metadata={})])
        long = split_documents([_long_document()])
        assert len(long) > len(short) >= 1

    def test_source_metadata_preserved(self) -> None:
        """原文 metadata 应保留到每个分块。"""
        for chunk in split_documents([_long_document()]):
            assert chunk.metadata["filename"] == "long.md"
            assert chunk.metadata["file_type"] == "md"

    def test_chunk_size_respects_settings(self) -> None:
        """分块长度不应超过配置的 CHUNK_SIZE。"""
        limit = get_settings().CHUNK_SIZE
        for chunk in split_documents([_long_document()]):
            assert len(chunk.page_content) <= limit

    def test_multiple_documents_share_global_index(self) -> None:
        """多文档切分时 chunk_index 全局连续。"""
        chunks = split_documents([_long_document(), _long_document()])
        assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))

    def test_empty_input_returns_empty_list(self) -> None:
        """空输入应返回空列表。"""
        assert split_documents([]) == []


# ═══════════ embed_chunks / extract_entities 契约（mock，不真实推理） ═══════════


class TestEmbedChunks:
    """embed_chunks：缓存单例、参数传递与异常包装。"""

    def test_empty_input_returns_empty_matrix(self) -> None:
        """空输入应返回 shape=(0, 0) 的 float32 数组。"""
        result = embed_chunks([])
        assert result.shape == (0, 0)
        assert result.dtype == np.float32

    @mock.patch("core.preprocessing.SentenceTransformer")
    def test_encode_parameters_and_output_dtype(self, mock_transformer: mock.Mock) -> None:
        """应使用规范 encode 参数并返回 float32 矩阵。"""
        model = mock_transformer.return_value
        model.encode.return_value = np.ones((2, 4), dtype=np.float64)
        chunks = [
            Document(page_content="甲", metadata={"chunk_id": "c1"}),
            Document(page_content="乙", metadata={"chunk_id": "c2"}),
        ]

        result = embed_chunks(chunks)

        mock_transformer.assert_called_once_with(get_settings().EMBEDDING_MODEL_NAME)
        args, kwargs = model.encode.call_args
        assert args[0] == ["甲", "乙"]
        assert kwargs["batch_size"] == 64
        assert kwargs["show_progress_bar"] is True
        assert kwargs["normalize_embeddings"] is True
        assert result.dtype == np.float32
        assert result.shape == (2, 4)

    @mock.patch("core.preprocessing.SentenceTransformer")
    def test_embedder_is_cached_singleton(self, mock_transformer: mock.Mock) -> None:
        """连续两次调用只应加载一次模型（P1 修复回归用例）。"""
        mock_transformer.return_value.encode.return_value = np.zeros((1, 2), dtype=np.float32)
        chunks = [Document(page_content="x", metadata={"chunk_id": "c"})]

        embed_chunks(chunks)
        embed_chunks(chunks)

        assert mock_transformer.call_count == 1

    @mock.patch("core.preprocessing.SentenceTransformer", side_effect=RuntimeError("unavailable"))
    def test_model_failure_wrapped_as_ingest_error(self, _mock_transformer: mock.Mock) -> None:
        """模型加载失败应包装为 IngestError。"""
        with pytest.raises(IngestError, match="Failed to embed chunks"):
            embed_chunks([Document(page_content="x", metadata={"chunk_id": "c"})])


class TestExtractEntities:
    """extract_entities：LLM 配置、JSON 解析与异常包装。"""

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_parses_payload_and_attaches_source_chunk_id(self, mock_llm_class: mock.Mock) -> None:
        """应解析 JSON 并为每条记录附加 source_chunk_id。"""
        llm = mock_llm_class.return_value
        llm.invoke.return_value = mock.Mock(
            content=(
                '{"entities": [{"name": "设备A", "type": "device"}], '
                '"relations": [{"source": "设备A", "target": "频繁重启", '
                '"type": "device_fault"}]}'
            )
        )
        chunks = [Document(page_content="片段", metadata={"chunk_id": "cid-1"})]

        records = extract_entities(chunks)

        entities = [item for item in records if item["kind"] == "entity"]
        relations = [item for item in records if item["kind"] == "relation"]
        assert len(entities) == 1
        assert entities[0]["name"] == "设备A"
        assert entities[0]["source_chunk_id"] == "cid-1"
        assert relations[0]["target"] == "频繁重启"
        assert relations[0]["source_chunk_id"] == "cid-1"

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_llm_configured_with_deepseek_settings(
        self, mock_llm_class: mock.Mock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配了 DeepSeek 凭据时用主模型（DeepSeek 配置 + json_object 模式）。"""
        settings = get_settings()
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-test-deepseek-key")
        mock_llm_class.return_value.invoke.return_value = mock.Mock(
            content='{"entities": [], "relations": []}'
        )

        extract_entities([Document(page_content="x", metadata={"chunk_id": "c"})])

        _, kwargs = mock_llm_class.call_args
        assert kwargs["model"] == settings.DEEPSEEK_MODEL_NAME
        assert kwargs["base_url"] == settings.DEEPSEEK_BASE_URL
        assert kwargs["api_key"] == settings.DEEPSEEK_API_KEY
        assert kwargs["model_kwargs"] == {"response_format": {"type": "json_object"}}

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_llm_falls_back_to_glm_without_deepseek_key(
        self, mock_llm_class: mock.Mock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 DeepSeek 凭据时回落 GLM 免费档（否则知识摄取必然中断）。

        2026-09-12 修正：原实现无条件用 DeepSeek，用户只配 GLM_API_KEY 时
        `llm.invoke` 必失败 → IngestError → 摄取中断；现与研判链路同规则回落 GLM，
        且**不传 thinking**（该非标准字段会被透传给结构化输出解析并报错）。
        """
        settings = get_settings()
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")
        mock_llm_class.return_value.invoke.return_value = mock.Mock(
            content='{"entities": [], "relations": []}'
        )

        extract_entities([Document(page_content="x", metadata={"chunk_id": "c"})])

        _, kwargs = mock_llm_class.call_args
        assert (kwargs["model"], kwargs["api_key"]) == (
            settings.GLM_MODEL_NAME, settings.GLM_API_KEY,
        )
        assert "thinking" not in kwargs["model_kwargs"]
        assert kwargs["model_kwargs"] == {"response_format": {"type": "json_object"}}

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_invalid_json_wrapped_as_ingest_error(self, mock_llm_class: mock.Mock) -> None:
        """非法 JSON 应抛 IngestError。"""
        mock_llm_class.return_value.invoke.return_value = mock.Mock(content="这不是 JSON")
        with pytest.raises(IngestError, match="Failed to extract entities"):
            extract_entities([Document(page_content="x", metadata={"chunk_id": "c"})])

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_invoked_once_per_batch(self, mock_llm_class: mock.Mock) -> None:
        """每 ENTITY_BATCH_SIZE 个分块合并为一次 LLM 调用（7 块 → 2 次）。"""
        mock_llm_class.return_value.invoke.return_value = mock.Mock(
            content='{"entities": [], "relations": []}'
        )
        chunks = [
            Document(page_content=f"片段{i}", metadata={"chunk_id": f"c{i}"}) for i in range(7)
        ]

        extract_entities(chunks)

        assert mock_llm_class.return_value.invoke.call_count == 2

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_batch_prompt_carries_segment_markers(self, mock_llm_class: mock.Mock) -> None:
        """批量请求正文应带 [片段 N] 标记，并声明 chunk_index 回填约定。"""
        llm = mock_llm_class.return_value
        llm.invoke.return_value = mock.Mock(content='{"entities": [], "relations": []}')
        chunks = [
            Document(page_content=f"正文{i}", metadata={"chunk_id": f"c{i}"}) for i in range(3)
        ]

        extract_entities(chunks)

        prompt = llm.invoke.call_args[0][0]
        assert "[片段 0]" in prompt
        assert "[片段 1]" in prompt
        assert "[片段 2]" in prompt
        assert "chunk_index" in prompt

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_batch_payload_maps_chunk_index_to_source_chunk_id(
        self, mock_llm_class: mock.Mock
    ) -> None:
        """批量请求下，模型回填的 chunk_index 应映射为对应分块的 chunk_id。"""
        llm = mock_llm_class.return_value
        llm.invoke.return_value = mock.Mock(
            content=(
                '{"entities": ['
                '{"chunk_index": 0, "name": "设备A", "type": "device"}, '
                '{"chunk_index": 2, "name": "电源模块", "type": "component"}], '
                '"relations": [{"chunk_index": 1, "source": "设备A", '
                '"target": "频繁重启", "type": "device_fault"}]}'
            )
        )
        chunks = [
            Document(page_content=f"片段{i}", metadata={"chunk_id": f"c{i}"}) for i in range(3)
        ]

        records = extract_entities(chunks)

        entities = {item["name"]: item for item in records if item["kind"] == "entity"}
        assert entities["设备A"]["source_chunk_id"] == "c0"
        assert entities["电源模块"]["source_chunk_id"] == "c2"
        relations = [item for item in records if item["kind"] == "relation"]
        assert relations[0]["source_chunk_id"] == "c1"

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_out_of_range_chunk_index_falls_back_to_first(
        self, mock_llm_class: mock.Mock
    ) -> None:
        """chunk_index 越界或缺失时回落到该批首个分块，记录仍可溯源。"""
        llm = mock_llm_class.return_value
        llm.invoke.return_value = mock.Mock(
            content=(
                '{"entities": [{"chunk_index": 99, "name": "越界实体", "type": "device"}, '
                '{"name": "缺字段实体", "type": "device"}], "relations": []}'
            )
        )
        chunks = [
            Document(page_content=f"片段{i}", metadata={"chunk_id": f"c{i}"}) for i in range(2)
        ]

        records = extract_entities(chunks)

        assert len(records) == 2
        assert all(item["source_chunk_id"] == "c0" for item in records)

    @mock.patch("core.preprocessing.ChatOpenAI")
    def test_empty_chunks_return_empty_records(self, mock_llm_class: mock.Mock) -> None:
        """空输入不触发 LLM，返回空列表。"""
        assert extract_entities([]) == []
        mock_llm_class.return_value.invoke.assert_not_called()


# ═══════════ 验收用例 6：ingest_documents 集成流程 ═══════════


class TestIngestDocuments:
    """ingest_documents：多格式集成流程与 IngestResult 结构。"""

    @mock.patch("core.preprocessing.extract_entities")
    @mock.patch("core.preprocessing.embed_chunks")
    def test_ingest_documents_flow(
        self, mock_embed: mock.Mock, mock_extract: mock.Mock
    ) -> None:
        """多格式（md+docx+pdf）集成：五步串联，IngestResult 结构完整。"""

        def _fake_embed(chunks: list) -> np.ndarray:
            """mock 向量化：按实际 chunk 数返回，验证矩阵对齐。"""
            return np.random.rand(len(chunks), 8).astype(np.float32)

        mock_embed.side_effect = _fake_embed
        mock_extract.return_value = [
            {"source_chunk_id": "any", "name": "设备A", "type": "device", "kind": "entity"}
        ]
        files = [
            ("# 运维手册\n\n设备频繁重启。".encode("utf-8"), "md", "manual.md"),
            (_make_docx_bytes(["电源模块老化", "风扇转速异常"]), "docx", "report.docx"),
            (_make_pdf_bytes(pages=2), "pdf", "trace.pdf"),
        ]

        result = ingest_documents(files)

        assert isinstance(result, IngestResult)
        # chunks：覆盖三个来源，metadata 完整
        source_types = {chunk.metadata["file_type"] for chunk in result.chunks}
        assert source_types == {"md", "docx", "pdf"}
        for chunk in result.chunks:
            assert chunk.metadata["chunk_id"]
            assert isinstance(chunk.metadata["chunk_index"], int)
            assert chunk.metadata["filename"] in {"manual.md", "report.docx", "trace.pdf"}
        indices = [chunk.metadata["chunk_index"] for chunk in result.chunks]
        assert indices == list(range(len(result.chunks)))
        # embeddings：与 chunks 对齐的 float32 矩阵
        assert result.embeddings.dtype == np.float32
        assert result.embeddings.shape == (len(result.chunks), 8)
        # entities：原样透传
        assert result.entities[0]["kind"] == "entity"
        # 下游各被调用一次
        mock_embed.assert_called_once()
        mock_extract.assert_called_once()

    @mock.patch("core.preprocessing.embed_chunks")
    def test_cleaning_applied_before_split(self, mock_embed: mock.Mock) -> None:
        """加载后的文本应先经 clean_text 清洗再进入分块。"""
        mock_embed.return_value = np.zeros((1, 4), dtype=np.float32)
        raw = "第 1 页 共 9 页\n设备\u0000重启".encode("utf-8")
        with mock.patch("core.preprocessing.extract_entities", return_value=[]):
            result = ingest_documents([(raw, "md", "ops.md")])

        joined = "\n".join(chunk.page_content for chunk in result.chunks)
        assert "\u0000" not in joined
        assert "共" not in joined

    @mock.patch("core.preprocessing.extract_entities", side_effect=RuntimeError("boom"))
    @mock.patch("core.preprocessing.embed_chunks")
    def test_unexpected_error_wrapped_as_ingest_error(
        self, mock_embed: mock.Mock, _mock_extract: mock.Mock
    ) -> None:
        """非 IngestError 的异常应被包装为 IngestError。"""
        mock_embed.return_value = np.zeros((1, 4), dtype=np.float32)
        files = [(_make_docx_bytes(["设备重启"]), "docx", "ops.docx")]

        with pytest.raises(IngestError, match="Failed to ingest documents"):
            ingest_documents(files)

    def test_unsupported_file_type_raises(self) -> None:
        """不支持的文件类型应在主流程中抛 IngestError（ValueError 被包装）。"""
        with pytest.raises((IngestError, ValueError)):
            ingest_documents([(b"plain", "txt", "a.txt")])


# ═══════════ 模块契约 ═══════════


class TestModuleContract:
    """模块级常量与类型契约。"""

    def test_ingest_result_fields(self) -> None:
        """IngestResult 字段恰为 chunks/embeddings/entities。"""
        result = IngestResult(chunks=[], embeddings=np.zeros((0, 0)), entities=[])
        assert list(result.__dataclass_fields__) == ["chunks", "embeddings", "entities"]

    def test_constants_match_spec(self) -> None:
        """常量应与规范一致。"""
        assert SPLIT_SEPARATORS == ["\n\n", "\n", "。", ".", " ", ""]
        assert ENTITY_BATCH_SIZE == 5

    def test_public_functions_exist(self) -> None:
        """六个公共函数应全部可调用。"""
        for name in (
            "load_document",
            "clean_text",
            "split_documents",
            "embed_chunks",
            "extract_entities",
            "ingest_documents",
        ):
            assert callable(getattr(preprocessing, name)), name
