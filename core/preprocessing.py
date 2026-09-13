"""数据预处理模块。

Agentic RAG 诊断链路的数据入口层，按以下顺序处理原始文档：

1. :func:`load_document` —— 从字节流解析 ``pdf`` / ``md`` / ``docx``，得到 ``Document``；
2. :func:`clean_text` —— 去除控制字符、页眉页脚与冗余空白；
3. :func:`split_documents` —— 按配置切分为带 ``chunk_id`` 的分块；
4. :func:`embed_chunks` —— 调用 BGE-M3 生成归一化向量矩阵；
5. :func:`extract_entities` —— 调用 DeepSeek 抽取实体与关系。

整个流程零磁盘占用：文档解析全部在 ``io.BytesIO`` 内存流上完成，
不生成任何中间文件。

Note:
    ``Document`` 统一采用 :class:`langchain_core.documents.Document`；
    ``python-docx`` 的同名类在本模块内以 ``DocxDocument`` 别名引入，以避让命名冲突。
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from uuid import uuid4

import numpy as np
from docx import Document as DocxDocument
from langchain_core.documents import Document
from langchain_openai import ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer

from src.constants import FILE_TYPES_SUPPORTED
from src.settings import get_settings

logger = logging.getLogger(__name__)

#: 实体抽取阶段单次送入 LLM 的最大分块数。
ENTITY_BATCH_SIZE: int = 5

#: 文本分块使用的分隔符优先级（中文标点优先）。
SPLIT_SEPARATORS: list[str] = ["\n\n", "\n", "。", ".", " ", ""]

#: 实体与关系抽取的提示词模板；``{text}`` 为一至多个带 ``[片段 N]`` 标记的文档片段。
#: 要求模型在每条记录上回填 ``chunk_index``，以便批量请求后仍能溯源到具体分块。
ENTITY_EXTRACTION_PROMPT: str = """你是一个运维知识图谱实体抽取专家。请从以下运维文档片段中抽取实体和关系。
要求抽取：设备名(device)、故障类型(fault_type)、解决方案(solution)、组件(component)。
要求抽取关系：设备-故障(device_fault)、故障-解决方案(fault_solution)、组件-设备(component_device)。
输入可能包含多个片段，每个片段以「[片段 N]」标记，N 从 0 开始编号。
输出的每一条实体与关系都必须带 "chunk_index" 字段，标明它来自哪个片段编号。
输出严格JSON格式：
{{
  "entities": [{{"chunk_index": 0, "name": "xxx", "type": "device|fault_type|solution|component"}}],
  "relations": [{{"chunk_index": 0, "source": "xxx", "target": "yyy", "type": "device_fault|fault_solution|component_device"}}]
}}
文档片段：
{text}"""


@dataclass
class IngestResult:
    """文档摄取流程的汇总结果。

    Attributes:
        chunks: 切分后的文档分块列表，每块 ``metadata`` 含 ``chunk_id`` 与 ``chunk_index``。
        embeddings: 与 ``chunks`` 顺序一致的向量矩阵，``dtype`` 为 ``float32``。
        entities: 抽取出的实体与关系记录，每条含 ``source_chunk_id``。
    """

    chunks: list[Document]
    embeddings: np.ndarray
    entities: list[dict]


class IngestError(Exception):
    """文档摄取流程异常。

    统一包装加载、清洗、分块、向量化与实体抽取阶段的底层异常，
    使上层可以按单一异常类型做降级处理。
    """


class EntityItem(BaseModel):
    """单个运维实体。

    Attributes:
        chunk_index: 该实体来源片段在本次请求内的编号（0 起）；缺省 0，
            使单片段请求与未标注来源的模型输出都能正常解析。
        name: 实体名称。
        type: 实体类型，取值 ``device`` / ``fault_type`` / ``solution`` / ``component``。
    """

    chunk_index: int = 0
    name: str
    type: str


class RelationItem(BaseModel):
    """单条实体关系。

    Attributes:
        chunk_index: 该关系来源片段在本次请求内的编号（0 起）；缺省 0。
        source: 头实体名称。
        target: 尾实体名称。
        type: 关系类型，取值 ``device_fault`` / ``fault_solution`` / ``component_device``。
    """

    chunk_index: int = 0
    source: str
    target: str
    type: str


class ExtractionPayload(BaseModel):
    """LLM 返回的实体抽取载荷，用于校验 JSON 结构。

    Attributes:
        entities: 实体列表。
        relations: 关系列表。
    """

    entities: list[EntityItem] = Field(default_factory=list)
    relations: list[RelationItem] = Field(default_factory=list)


def load_document(
    file_bytes: bytes, file_type: str, filename: str, doc_id: str | None = None
) -> Document:
    """从字节流加载单个文档并抽取纯文本。

    Args:
        file_bytes: 文档原始字节流。
        file_type: 文档类型，取值 ``pdf`` / ``md`` / ``docx``（不区分大小写，允许带前导点）。
        filename: 原始文件名，用于错误信息与 ``metadata``。

    Returns:
        解析得到的 :class:`Document`；``metadata`` 至少包含 ``filename``、``file_type``
        以及格式相关计数——PDF 为 ``page_count``，Markdown 与 DOCX 为 ``paragraph_count``。

    Raises:
        ValueError: ``file_type`` 不在支持列表内。
        IngestError: 底层解析库抛出异常（文档损坏、编码错误等）。
    """
    normalized = file_type.lower().lstrip(".")
    # 文档级唯一标识：删除文档、按文档聚合分段、引用回溯都依赖它。
    # 必须在解析前生成，保证同一份文档的元数据在三种格式下都带同一标识。
    # 允许外部指定：异步摄取任务需要提前把 doc_id 交给前端轮询，
    # 否则任务标识与最终文档标识会对不上。
    doc_id = doc_id or str(uuid4())
    if normalized not in FILE_TYPES_SUPPORTED:
        raise ValueError(
            f"Unsupported file type: {file_type}; supported: {FILE_TYPES_SUPPORTED}"
        )

    try:
        if normalized == "pdf":
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            metadata = {
                "filename": filename,
                "file_type": normalized,
                "page_count": len(reader.pages),
            }
        elif normalized == "md":
            text = file_bytes.decode("utf-8")
            metadata = {
                "filename": filename,
                "file_type": normalized,
                "paragraph_count": len(text.split("\n\n")),
            }
        else:  # normalized == "docx"
            docx_document = DocxDocument(io.BytesIO(file_bytes))
            text = "\n".join([paragraph.text for paragraph in docx_document.paragraphs])
            metadata = {
                "filename": filename,
                "file_type": normalized,
                "paragraph_count": len(docx_document.paragraphs),
            }
    except Exception as exc:  # noqa: BLE001 - 统一包装为 IngestError
        logger.error("加载文档失败: filename=%s type=%s", filename, normalized)
        raise IngestError(f"Failed to load {filename}: {str(exc)}") from exc

    logger.info(
        "已加载文档: filename=%s type=%s chars=%d", filename, normalized, len(text)
    )
    metadata["doc_id"] = doc_id
    metadata["kb_id"] = "default"
    metadata["char_length"] = len(text)
    return Document(page_content=text, metadata=metadata)


def clean_text(text: str) -> str:
    """清洗文本：归一换行、去控制字符、去页眉页脚、压缩空行、去行首尾空白。

    Args:
        text: 原始文本。

    Returns:
        清洗后的文本。

    Raises:
        TypeError: ``text`` 不是字符串（由 ``str.replace`` 自然抛出，本函数不做包装）。

    Note:
        清洗顺序固定为：``\\r`` 归一 → 控制字符去除 → 中英文页眉页脚去除 →
        连续换行压缩 → 逐行去首尾空白。顺序不可调换，否则页脚残留的空白会
        影响后续压缩效果。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"第\s*\d+\s*页\s*共\s*\d+\s*页", "", text)
    text = re.sub(r"Page\s+\d+\s+of\s+\d+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join([line.strip() for line in text.split("\n")])
    return text


def split_documents(docs: list[Document]) -> list[Document]:
    """将文档切分为带唯一标识的分块。

    分块大小与重叠长度取自全局配置 ``CHUNK_SIZE`` 与 ``CHUNK_OVERLAP``，
    分隔符优先级为 ``["\\n\\n", "\\n", "。", ".", " ", ""]``。

    Args:
        docs: 待切分的 :class:`Document` 列表。

    Returns:
        分块后的 :class:`Document` 列表；每个分块的 ``metadata`` 在原文
        ``metadata`` 基础上追加 ``chunk_index``（从 0 开始的全局连续序号）
        与 ``chunk_id``（``uuid4`` 字符串）。

    Raises:
        Exception: 配置非法（如 ``CHUNK_OVERLAP >= CHUNK_SIZE``）或输入类型
            错误时由 splitter 抛出底层异常，本函数不做包装，由
            :func:`ingest_documents` 统一收口为 :class:`IngestError`。
    """
    settings = get_settings()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=settings.CHUNK_OVERLAP,
        separators=SPLIT_SEPARATORS,
        # 关键：偏移必须由切分器给出，不能事后用 find 反查。
        # 重复/周期性文本下 find 只会取最早匹配，区间会静默错位——而且
        # 由于「落在区间里的子串恰好等于分块内容」，自洽性断言根本发现不了。
        add_start_index=True,
    )
    chunks = splitter.split_documents(docs)

    for index, chunk in enumerate(chunks):
        chunk.metadata["chunk_index"] = index
        chunk.metadata["chunk_id"] = str(uuid4())

        # start_index 由切分器在切分过程中记录，对重复文本同样精确；
        # 统一改名为 char_start/char_end，全项目只用这一套口径。
        start = chunk.metadata.pop("start_index", None)
        if start is None:
            continue
        chunk.metadata["char_start"] = int(start)
        chunk.metadata["char_end"] = int(start) + len(chunk.page_content)

    logger.info("已切分文档: docs=%d chunks=%d", len(docs), len(chunks))
    return chunks


@lru_cache(maxsize=1)
def _get_embedder(model_name: str) -> SentenceTransformer:
    """加载并缓存向量化模型（进程内单例）。

    Args:
        model_name: 模型名称，如 ``BAAI/bge-m3``。

    Returns:
        已加载的 :class:`SentenceTransformer` 实例。

    Note:
        模型权重量级约 2GB，进程内只应加载一次；
        每次调用重复加载会带来秒级延迟与内存抖动。
    """
    return SentenceTransformer(model_name)


def embed_chunks(chunks: list[Document]) -> np.ndarray:
    """将分块文本编码为归一化向量矩阵。

    使用进程内缓存的 ``SentenceTransformer``（模型取 ``EMBEDDING_MODEL_NAME``，
    默认 ``BAAI/bge-m3``），批量编码并做 L2 归一化。

    Args:
        chunks: 分块 :class:`Document` 列表。

    Returns:
        ``dtype`` 为 ``float32`` 的二维数组，``shape=(len(chunks), embedding_dim)``；
        当 ``chunks`` 为空时返回 ``shape=(0, 0)`` 的空数组。

    Raises:
        IngestError: 模型加载失败或编码过程异常。
    """
    if not chunks:
        return np.zeros((0, 0), dtype=np.float32)

    settings = get_settings()
    try:
        model = _get_embedder(settings.EMBEDDING_MODEL_NAME)
        texts = [chunk.page_content for chunk in chunks]
        embeddings = model.encode(
            texts,
            batch_size=64,
            show_progress_bar=True,
            normalize_embeddings=True,
        )
    except Exception as exc:  # noqa: BLE001 - 统一包装为 IngestError
        logger.error("向量化失败: model=%s", settings.EMBEDDING_MODEL_NAME)
        raise IngestError(f"Failed to embed chunks: {str(exc)}") from exc

    logger.info("已生成向量: chunks=%d dim=%d", len(chunks), np.shape(embeddings)[-1])
    return embeddings.astype(np.float32)


def _content_to_text(content: object) -> str:
    """把 LLM 响应内容规整为纯文本。

    Args:
        content: ``ChatOpenAI`` 响应对象的 ``content`` 字段，可能是字符串或内容块列表。

    Returns:
        拼接后的纯文本。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
        return "".join(parts)
    return str(content)


def _format_batch(batch: list[Document]) -> str:
    """把一批分块渲染为带片段编号的请求正文。

    Args:
        batch: 本次请求包含的分块列表。

    Returns:
        形如 ``[片段 0]`` + 换行 + 正文的多片段文本，片段间以空行分隔。
    """
    return "\n\n".join(
        f"[片段 {index}]\n{chunk.page_content}" for index, chunk in enumerate(batch)
    )


def _resolve_chunk_id(batch: list[Document], chunk_index: int) -> str:
    """把模型回填的片段编号解析为该片段的 ``chunk_id``。

    Args:
        batch: 本次请求包含的分块列表。
        chunk_index: 模型标注的片段编号。

    Returns:
        对应分块的 ``chunk_id``；编号越界或为负时回落到该批首个分块，
        保证每条记录始终带可用的溯源标识。
    """
    index = chunk_index if 0 <= chunk_index < len(batch) else 0
    return str(batch[index].metadata.get("chunk_id", ""))


def _records_from_payload(
    payload: ExtractionPayload, batch: list[Document]
) -> list[dict]:
    """把校验后的载荷展开为带 chunk_id 归属的记录列表。

    Args:
        payload: 通过 pydantic 校验的抽取结果。
        batch: 本次请求包含的分块列表。

    Returns:
        实体记录（``kind="entity"``）与关系记录（``kind="relation"``）列表，
        每条均含 ``source_chunk_id``。
    """
    records: list[dict] = []
    for entity in payload.entities:
        records.append(
            {
                "source_chunk_id": _resolve_chunk_id(batch, entity.chunk_index),
                "name": entity.name,
                "type": entity.type,
                "kind": "entity",
            }
        )
    for relation in payload.relations:
        records.append(
            {
                "source_chunk_id": _resolve_chunk_id(batch, relation.chunk_index),
                "source": relation.source,
                "target": relation.target,
                "type": relation.type,
                "kind": "relation",
            }
        )
    return records


def extract_entities(chunks: list[Document]) -> list[dict]:
    """从分块中抽取运维实体与关系。

    通过 ``ChatOpenAI`` 调用 DeepSeek（``DEEPSEEK_MODEL_NAME`` /
    ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY``），开启 ``json_object``
    响应格式。每 ``ENTITY_BATCH_SIZE`` 个分块**合并为一次请求**：片段以
    ``[片段 N]`` 标记送入，模型在每条记录上回填 ``chunk_index``，
    再据此映射回该片段的 ``chunk_id``，从而把调用次数压缩到
    ``ceil(len(chunks) / ENTITY_BATCH_SIZE)``，同时保持逐条可溯源。

    Args:
        chunks: 分块 :class:`Document` 列表，其 ``metadata`` 须含 ``chunk_id``。

    Returns:
        实体与关系记录列表。实体记录形如
        ``{"source_chunk_id": ..., "name": ..., "type": ..., "kind": "entity"}``；
        关系记录形如
        ``{"source_chunk_id": ..., "source": ..., "target": ..., "type": ...,
        "kind": "relation"}``。空输入返回空列表且不触发 LLM 调用。

    Raises:
        IngestError: LLM 调用失败，或返回内容无法通过 pydantic 校验。

    Note:
        模型给出的 ``chunk_index`` 越界（或未给出、取缺省 0）时，
        归属回落到该批的首个分块，确保记录始终带可用的 ``source_chunk_id``。
    """
    if not chunks:
        return []

    settings = get_settings()
    # 与研判链路同规则选型：配置了 DeepSeek 凭据用主模型，否则回落 GLM 免费档，
    # 保证零 DeepSeek 凭据的环境（用户仅配 GLM_API_KEY）知识摄取也能完成。
    if settings.DEEPSEEK_API_KEY:
        llm = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL_NAME,
            base_url=settings.DEEPSEEK_BASE_URL,
            api_key=settings.DEEPSEEK_API_KEY,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
    else:
        llm = ChatOpenAI(
            model=settings.GLM_MODEL_NAME,
            base_url=settings.GLM_BASE_URL,
            api_key=settings.GLM_API_KEY,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

    results: list[dict] = []
    for start in range(0, len(chunks), ENTITY_BATCH_SIZE):
        batch = chunks[start : start + ENTITY_BATCH_SIZE]
        prompt = ENTITY_EXTRACTION_PROMPT.format(text=_format_batch(batch))
        try:
            response = llm.invoke(prompt)
            payload = ExtractionPayload.model_validate_json(
                _content_to_text(getattr(response, "content", response))
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装为 IngestError
            logger.error("实体抽取失败: batch_start=%d batch_size=%d", start, len(batch))
            raise IngestError(f"Failed to extract entities: {str(exc)}") from exc

        results.extend(_records_from_payload(payload, batch))

    logger.info("已抽取实体与关系: records=%d chunks=%d", len(results), len(chunks))
    return results


#: 摄取各阶段的名称与完成比例上界，供任务状态机展示进度。
class IngestCancelled(Exception):
    """摄取被调用方取消：由进度回调抛出，用于立即中断。

    Note:
        它必须穿透摄取流程。进度上报为了「观测失败不影响摄取」会吞掉普通异常，
        若取消也被一并吞掉，任务就会带着「已取消」的标签把文件真正写完入库——
        界面说取消了，库里却多出一份数据。
    """


INGEST_STAGES: tuple[tuple[str, float], ...] = (
    ("parse", 0.4),
    ("chunk", 0.5),
    ("embed", 0.7),
    ("entity", 0.95),
)


def ingest_documents(files: list[tuple[bytes, str, str]]) -> IngestResult:
    """文档摄取主入口。

    依次执行：加载与清洗 → 分块 → 向量化 → 实体抽取，
    最后封装为 :class:`IngestResult`。

    Args:
        files: 三元组列表，每项为 ``(file_bytes, file_type, filename)``。

    Returns:
        汇总后的 :class:`IngestResult`。

    Raises:
        IngestError: 任一阶段失败（底层原始异常已包装并保留异常链）。
    """
    return ingest_documents_with_progress(files, None, None)


def ingest_documents_with_progress(
    files: list[tuple[bytes, str, str]],
    on_stage: Callable[[str, float], None] | None = None,
    doc_id: str | None = None,
) -> IngestResult:
    """与 :func:`ingest_documents` 同一流程，但在阶段边界回报进度。

    Args:
        files: 三元组列表，每项为 ``(file_bytes, file_type, filename)``。
        on_stage: 可选回调 ``(阶段名, 完成比例)``；为 None 时行为与
            :func:`ingest_documents` 完全一致。
        doc_id: 可选的文档标识；多文件时只作用于首个文件，单文件上传请始终传入。

    Returns:
        汇总后的 :class:`IngestResult`。

    Raises:
        IngestError: 任一阶段失败（底层原始异常已包装并保留异常链）。

    Note:
        进度回调做成可选参数，而不是直接改 :func:`ingest_documents` 的签名：
        后者被既有测试与全部调用方共用，为了报进度去动它会一次性打断所有人。
        回调自身异常也不得影响摄取 —— 它只是观测，不该成为新的失败点。
    """

    def report(stage: str, ratio: float) -> None:
        """安全回报进度：回调抛错只记日志，但取消信号必须穿透。

        Raises:
            IngestCancelled: 调用方用它表达「停下来」；这一条不能被容错吞掉。
        """
        if on_stage is None:
            return
        try:
            on_stage(stage, ratio)
        except IngestCancelled:
            raise
        except Exception:  # noqa: BLE001 - 观测失败不得影响摄取
            logger.warning("进度回调失败，已忽略: stage=%s", stage)

    try:
        documents: list[Document] = []
        total = max(1, len(files))
        for index, (file_bytes, file_type, filename) in enumerate(files):
            document = load_document(file_bytes, file_type, filename, doc_id)
            document.page_content = clean_text(document.page_content)
            # char_length 必须与清洗后的正文一致：它和 char_start/char_end 是同一把尺子，
            # 若记的是清洗前长度，两个字段基准不同，前端按长度校验区间就会对不上。
            document.metadata["char_length"] = len(document.page_content)
            documents.append(document)
            report("parse", (index + 1) / total * INGEST_STAGES[0][1])

        report("chunk", INGEST_STAGES[1][1])
        chunks = split_documents(documents)

        report("embed", INGEST_STAGES[2][1])
        embeddings = embed_chunks(chunks)

        report("entity", INGEST_STAGES[3][1])
        entities = extract_entities(chunks)
    except (IngestError, IngestCancelled):
        # 取消不是失败：包装成 IngestError 会让「用户主动取消」显示成报错。
        raise
    except Exception as exc:  # noqa: BLE001 - 统一包装为 IngestError
        logger.error("文档摄取失败: files=%d", len(files))
        raise IngestError(f"Failed to ingest documents: {str(exc)}") from exc

    report("done", 1.0)
    return IngestResult(chunks=chunks, embeddings=embeddings, entities=entities)
