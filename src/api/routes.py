"""接口层：REST 与 SSE 服务入口。

启动方式::

    uvicorn src.api.routes:app --reload --port 8000

端点一览（前缀 /api/v1）：

  GET  /health              健康检查与存储规模
  POST /diagnose            SSE 流式诊断（五节点事件）
  POST /query               非流式诊断（一次性返回）
  POST /upload              文档摄取（内存处理，零磁盘）
  POST /retrieve            仅混合检索（调参面板）
  POST /evaluate            触发后台评估
  GET  /evaluate/results    取评估结果
  GET  /trace/{trace_id}    链路明细
  POST /feedback            人工反馈回流

零磁盘约束：

* 上传文件经 await file.read() 直接取字节流交给摄取链路，不落临时文件；
* 链路明细、评估结果与反馈全部存于 app.state 的内存结构；
* 所有响应均为 JSON，不含任何文件下载链接。

Note:
    生命周期使用 FastAPI 的 lifespan，而非已废弃的 @app.on_event(startup)；
    组件初始化失败不会阻止服务启动，相关端点会以 503 明示「尚未就绪」，
    而不是静默返回空数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# 必须先于任何 HuggingFace 系依赖导入：huggingface_hub 在 import 时读取
# HF_HOME / HF_HUB_OFFLINE，而本模块的检索组件是懒加载的——若不在进程
# 最早注入环境变量，启动会卡在对 huggingface.co 的联网探测重试上。
import src.settings  # noqa: F401,E402 - 环境变量注入副作用，见上

from src.api.jobs import IngestCancelled, JobRegistry  # noqa: E402 - 同上，须在环境变量注入后

logger = logging.getLogger(__name__)

#: 内存中保留的链路明细条数上限。
MAX_TRACES: int = 200

#: 内存中保留的人工反馈条数上限。
MAX_FEEDBACK: int = 500


class ChatTurn(BaseModel):
    """一轮历史对话。

    Attributes:
        role: user 或 assistant。
        content: 该轮文本；服务端只取最近若干条并截断超长内容。
    """

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


def _history_payload(history: list[ChatTurn]) -> list[dict[str, str]]:
    """把请求里的历史轮次转成状态机使用的字典列表。"""
    return [{"role": turn.role, "content": turn.content} for turn in history]


class DiagnoseRequest(BaseModel):
    """诊断请求。

    Attributes:
        query: 运维诊断问题。
        topK: 召回候选数；缺省沿用全局配置。
        rerankK: 重排保留数；缺省沿用全局配置。
        useCache: 是否启用语义缓存。
    """

    query: str = Field(min_length=1, description="运维诊断问题")
    topK: int | None = Field(default=None, ge=1, le=100)
    rerankK: int | None = Field(default=None, ge=1, le=50)
    useCache: bool = True
    history: list[ChatTurn] = Field(
        default_factory=list, max_length=12, description="多轮对话历史，按时间正序"
    )


class QueryRequest(BaseModel):
    """非流式查询请求。

    Attributes:
        query: 运维诊断问题。
    """

    query: str = Field(min_length=1)


class QueryResponse(BaseModel):
    """非流式查询响应。

    Attributes:
        answer: 诊断答案。
        sources: 来源记录列表。
        latency_ms: 端到端耗时（毫秒）。
        trace_id: 本次链路标识。
    """

    answer: str
    sources: list[dict[str, Any]]
    latency_ms: float
    trace_id: str


class UploadAcceptedResponse(BaseModel):
    """上传受理响应（202）。

    上传不再等摄取跑完：受理即返回，每个文件建一个任务，前端轮询
    /knowledge/jobs/{doc_id} 取进度与失败原因。

    Attributes:
        status: 固定为 queued，表示已受理、尚未完成。
        jobs: 每个文件对应的任务快照。
    """

    status: str
    jobs: list[dict[str, Any]]


class RetrieveRequest(BaseModel):
    """检索调参请求。

    Attributes:
        query: 查询文本。
        topK: 召回条数。
        rerankK: 重排保留条数。
    """

    query: str = Field(min_length=1)
    topK: int | None = Field(default=None, ge=1, le=100)
    rerankK: int | None = Field(default=None, ge=1, le=50)


class HealthResponse(BaseModel):
    """健康检查响应。

    Attributes:
        status: 服务与组件状态。
        vector_count: 向量索引中的条数。
        graph_nodes: 图谱节点数。
        graph_edges: 图谱边数。
    """

    status: str
    vector_count: int
    graph_nodes: int
    graph_edges: int


class FeedbackRequest(BaseModel):
    """人工反馈请求。

    Attributes:
        trace_id: 关联的链路标识。
        rating: 评分，取值 up 或 down。
        comment: 可选文字说明。
    """

    trace_id: str = Field(min_length=1)
    rating: str = Field(pattern="^(up|down)$")
    comment: str = ""


class ErrorResponse(BaseModel):
    """错误响应。

    Attributes:
        error: 错误类型。
        detail: 详细信息。
    """

    error: str
    detail: str


class AppState:
    """进程内组件与运行数据容器。

    全部字段均驻留内存，进程退出即释放，不产生任何持久化文件。

    Attributes:
        vector_store: 内存向量存储。
        graph_store: 内存知识图谱。
        retriever: 混合检索器。
        agent_graph: 已编译的 Agent 状态图。
        traces: 链路明细（有界双端队列）。
        feedback: 人工反馈记录。
        evaluation_result: 最近一次评估结果。
        evaluation_status: 评估任务状态。
        ready: 组件是否初始化完成。
    """

    def __init__(self) -> None:
        """初始化空容器。"""
        self.vector_store: Any = None
        self.graph_store: Any = None
        self.retriever: Any = None
        self.agent_graph: Any = None
        self.traces: deque[dict[str, Any]] = deque(maxlen=MAX_TRACES)
        self.feedback: deque[dict[str, Any]] = deque(maxlen=MAX_FEEDBACK)
        self.evaluation_result: dict[str, Any] | None = None
        self.evaluation_status: str = "idle"
        self.ready: bool = False
        self.jobs: Any = None
        #: 待重试文件的内存副本（doc_id -> (字节流, 类型, 文件名)）；保存它，
        #: 失败任务才能原样重跑，而不必要求用户重新选择文件。
        self.upload_payloads: dict[str, tuple[bytes, str, str]] = {}


async def _init_components(state: AppState) -> None:
    """初始化检索、图谱与状态机组件。

    Args:
        state: 应用状态容器。

    Note:
        任一组件初始化失败都不会阻止服务启动：记录日志并把 ready 保持为
        False，相关端点据此返回 503，而不是静默给出空结果。
    """
    try:
        from core.retrieval import HybridRetriever
        from src.agent.state_machine import build_agent_graph
        from src.knowledge.graph_builder import MemoryGraphStore
        from src.retrieval.vector_store import MemoryVectorStore, document_matches
        from src.settings import get_settings

        settings = get_settings()
        # 注意：这里必须传向量维度（bge-m3 = 1024），不能传 TOP_K_RETRIEVAL，
        # 否则写入时会因维度不符而 500。
        state.vector_store = MemoryVectorStore(settings.EMBEDDING_DIM)
        state.graph_store = MemoryGraphStore()
        # 检索器必须显式装配：此前 state.retriever 恒为 None，导致 /retrieve 恒 503、
        # 诊断的检索节点恒拿到 0 篇文档（表现为「知识库中没有相关内容」）。
        # 嵌入模型加载较重，单独兜底：失败时仅检索链路不可用，其余接口照常。
        try:
            from core.preprocessing import _get_embedder

            embedder = _get_embedder(settings.EMBEDDING_MODEL_NAME)
            state.retriever = HybridRetriever(state.vector_store, state.graph_store, embedder)
        except Exception as exc:  # noqa: BLE001
            logger.error("检索器装配失败，检索相关端点将返回 503: %s", exc)
            state.retriever = None
        # 启动即尝试恢复上次的知识库；目录不存在或快照损坏都只是空库启动，不阻断服务。
        if (
            settings.RAG_DATA_DIR
            and state.vector_store is not None
            and hasattr(state.vector_store, "load")
        ):
            if state.vector_store.load(settings.RAG_DATA_DIR):
                logger.info("已从 %s 恢复知识库", settings.RAG_DATA_DIR)
                # BM25 索引与 chunk 映射不随向量落盘，必须按恢复出来的文档重建，
                # 否则会出现「向量库非空但检索恒为空」的静默失效。
                if state.retriever is not None:
                    restored = state.vector_store.all_documents()
                    if restored:
                        state.retriever.build_bm25_index(restored)
                        logger.info("已重建 BM25 索引: %d 条", len(restored))
            else:
                logger.info("未找到可用快照，按空库启动（目录 %s）", settings.RAG_DATA_DIR)
        state.agent_graph = build_agent_graph(state.retriever)
        state.ready = True
        logger.info("组件初始化完成")
    except Exception as exc:  # noqa: BLE001 - 启动不得因组件失败而中断
        logger.error("组件初始化失败，相关端点将返回 503: %s", exc)
        state.ready = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时装配内存组件。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None；退出时无需释放磁盘资源（全部为内存对象）。
    """
    state = AppState()
    state.jobs = JobRegistry()
    app.state.rag = state
    await _init_components(state)
    yield
    logger.info("服务退出，内存组件随进程释放")


app = FastAPI(
    title="FishCloud · 智能运维平台",
    description="资产登记 + 事件中心 + AIOps 研判的真实运维工作流平台（零 C 盘：数据驻留内存与 D 盘）",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter(prefix="/api/v1", tags=["diagnose"])

# 公开端点：仅健康检查。桌面启动器（desktop/fishcloud_desktop.py 的 wait_ready）
# 与运维监控依赖它匿名探活，故必须留在鉴权之外。
public_router = APIRouter(prefix="/api/v1", tags=["public"])

# AIOps 接口鉴权（2026-09-13 安全加固）：RAG 链路此前完全匿名可访问——匿名可
# 触发评估烧模型额度、上传/删除知识库。现按读写分档：
#   读（viewer+）  ：检索 /retrieve、链路 /trace、知识库与评估结果的查询；
#   写（operator+）：问答与诊断（消耗模型额度并落库）、上传/删除知识库文档、
#                    任务取消/重试、触发评估、反馈提交。
# /health 在 public_router，保持公开。
#
# 开关：环境变量 RAG_REQUIRE_AUTH=0/false/no 可关闭（仅用于未接入登录的旧部署
# 与本仓库存量测试；生产默认开启，联网环境不要关闭）。
# 依赖列表在此处定义（早于各端点装饰器引用它们的 dependencies=）。
import os as _os  # noqa: E402 - 紧随 router 定义，供本文件内端点装饰器引用

_RAG_AUTH_ENABLED: bool = _os.environ.get("RAG_REQUIRE_AUTH", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
if _RAG_AUTH_ENABLED:
    from fastapi import Depends as _Depends  # noqa: E402
    from src.platform.deps import require_roles as _require_roles  # noqa: E402

    _RAG_READ_DEPS: list = [_Depends(_require_roles("viewer"))]
    _RAG_WRITE_DEPS: list = [_Depends(_require_roles("operator"))]
else:
    _RAG_READ_DEPS = []
    _RAG_WRITE_DEPS = []


def _state(request: Request) -> AppState:
    """取出应用状态容器。

    Args:
        request: 当前请求。

    Returns:
        进程内 AppState。
    """
    return request.app.state.rag


def _require(state: AppState, component: str) -> None:
    """断言组件已就绪。

    Args:
        state: 应用状态容器。
        component: 组件名，用于错误信息。

    Raises:
        HTTPException: 组件尚未初始化完成时返回 503。
    """
    if not state.ready:
        raise HTTPException(
            status_code=503,
            detail=f"component not ready: {component}; check server logs",
        )


def _sse(event: str, payload: dict[str, Any]) -> str:
    """把事件与载荷编码为 SSE 帧。

    Args:
        event: 事件名。
        payload: 事件载荷。

    Returns:
        形如 event 行 + data 行 + 空行的帧文本。
    """
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def node_event(node: str) -> str:
    """把图节点名映射为前端约定的事件名。

    Args:
        node: LangGraph 节点名。

    Returns:
        事件名；hallucination_check 收敛为 hallucination。
    """
    return "hallucination" if node == "hallucination_check" else node


def _citation(item: Any, score: float) -> dict[str, Any]:
    """构造结构化引用记录。

    Args:
        item: 检索命中的文档，可能是 ``Document`` 或状态里流转的 dict。
        score: 相关度分数。

    Returns:
        统一字段的引用字典。

    Note:
        /retrieve、/query、/diagnose 三处必须返回同一套字段，前端才能统一渲染与
        定位；缺少字符坐标时 locatable=False 并给出 degrade_reason —— 宁可在界面上
        明说「这份来源无法定位」，也不要给用户一个点不动的引用。
    """
    if isinstance(item, dict):
        meta = item.get("metadata") or item
        content = str(item.get("content") or item.get("page_content") or "")
    else:
        meta = getattr(item, "metadata", None) or {}
        content = str(getattr(item, "page_content", "") or "")
    start = meta.get("char_start")
    end = meta.get("char_end")
    locatable = start is not None and end is not None
    return {
        "doc_id": str(meta.get("doc_id") or ""),
        "chunk_id": str(meta.get("chunk_id") or ""),
        "kb_id": str(meta.get("kb_id") or "default"),
        "filename": str(meta.get("filename") or ""),
        "chunk_index": int(meta.get("chunk_index") or 0),
        "score": round(float(score), 6),
        "snippet": content[:200],
        "content": content,
        "char_start": start,
        "char_end": end,
        "locatable": locatable,
        "degrade_reason": "" if locatable else "no_text_coordinate",
    }


def node_payload(node: str, state: dict[str, Any]) -> dict[str, Any]:
    """按节点裁剪出前端需要的事件载荷。

    Args:
        node: 节点名。
        state: 该节点输出后的状态快照。

    Returns:
        与 README 事件契约对应的载荷字典。
    """
    if node == "decompose":
        return {"subQuestions": state.get("sub_queries") or []}
    if node == "retrieve":
        documents = state.get("retrieved_docs") or []
        scores = [float(item.get("score") or 0.0) for item in documents]
        return {
            "docCount": len(documents),
            "topScore": round(max(scores), 4) if scores else 0.0,
            "documents": [_citation(item, float(item.get("score") or 0.0)) for item in documents],
        }
    if node == "reflect":
        reflection = state.get("reflection") or {}
        return {
            "sufficient": bool(reflection.get("sufficient", False)),
            "missing": reflection.get("missing") or [],
        }
    if node == "generate":
        return {"delta": state.get("answer") or ""}
    if node == "hallucination_check":
        return {
            "hasHallucination": bool(state.get("has_hallucination", False)),
            "sentences": state.get("hallucination_sentences") or [],
        }
    return {}


@public_router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """健康检查。

    Args:
        request: 当前请求。

    Returns:
        服务状态与三类存储的规模。
    """
    state = _state(request)
    nodes, edges = (0, 0)
    if state.graph_store is not None:
        nodes, edges = state.graph_store.count()
    return HealthResponse(
        status="ok" if state.ready else "degraded",
        vector_count=state.vector_store.count() if state.vector_store else 0,
        graph_nodes=nodes,
        graph_edges=edges,
    )


@router.post(
    "/query",
    response_model=QueryResponse,
    responses={500: {"model": ErrorResponse}},
    dependencies=_RAG_WRITE_DEPS,
)
async def query_endpoint(request: Request, body: QueryRequest) -> QueryResponse:
    """非流式诊断：一次性返回答案与来源。

    Args:
        request: 当前请求。
        body: 查询请求体。

    Returns:
        含答案、来源与耗时的响应。

    Raises:
        HTTPException: 组件未就绪（503）或诊断过程失败（500）。
    """
    state = _state(request)
    _require(state, "agent_graph")

    from src.agent.state_machine import run_agent

    started = time.perf_counter()
    try:
        result = await run_agent(body.query, state.agent_graph)
    except Exception as exc:  # noqa: BLE001 - 统一转换为 500
        logger.error("诊断失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    trace_id = f"{int(time.time() * 1000)}-{len(state.traces) + 1}"
    record = {
        "trace_id": trace_id,
        "query": body.query,
        "answer": result.get("answer") or "",
        "sources": result.get("sources") or [],
        "iterations": result.get("iterations", 0),
        "elapsed_ms": round(elapsed_ms, 2),
        "error": result.get("error"),
    }
    state.traces.append(record)

    return QueryResponse(
        answer=record["answer"],
        sources=record["sources"],
        latency_ms=round(elapsed_ms, 2),
        trace_id=trace_id,
    )


@router.post("/diagnose", dependencies=_RAG_WRITE_DEPS)
async def diagnose(request: Request, body: DiagnoseRequest) -> StreamingResponse:
    """SSE 流式诊断。

    事件序列与状态机五节点一一对应：
    decompose → retrieve → reflect → generate → hallucination → done。

    Args:
        request: 当前请求。
        body: 诊断请求体。

    Returns:
        text/event-stream 流式响应。

    Raises:
        HTTPException: 组件未就绪时返回 503。
    """
    state = _state(request)
    _require(state, "agent_graph")

    async def event_stream() -> AsyncIterator[str]:
        """按节点推进状态机并逐段推送事件。"""
        from src.agent.state_machine import initial_state

        started = time.perf_counter()
        trace_id = f"{int(time.time() * 1000)}-stream"
        seen: set[str] = set()
        final: dict[str, Any] = {}
        try:
            config = {"configurable": {"thread_id": trace_id}}
            async for chunk in state.agent_graph.astream(
                initial_state(body.query, _history_payload(body.history)), config
            ):
                # 用户点了「停止生成」：客户端断开后立即停止推进，不再跑后续节点
                # （幻觉校验等节点本身也是大模型调用，白跑就是白烧额度）。
                if await request.is_disconnected():
                    # 用 warning 而非 info：应用 logger 在 uvicorn 下没有 handler，
                    # 只有 WARNING 及以上才会经 lastResort 落到控制台。断开是运维
                    # 需要看得见的事件，不能只写在看不见的地方。
                    logger.warning("客户端已断开，停止推进状态机: %s", trace_id)
                    return
                for node, node_state in (chunk or {}).items():
                    if node in seen:
                        continue
                    seen.add(node)
                    final = dict(node_state)
                    yield _sse(node_event(node), node_payload(node, node_state))
            yield _sse(
                "done",
                {
                    "traceId": trace_id,
                    "elapsedMs": round((time.perf_counter() - started) * 1000.0, 2),
                },
            )
            state.traces.append(
                {
                    "trace_id": trace_id,
                    "query": body.query,
                    "answer": final.get("answer") or "",
                    "sources": final.get("sources") or [],
                    "iterations": final.get("iterations", 0),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
                    "error": final.get("error"),
                }
            )
        except asyncio.CancelledError:
            # 客户端主动中止属正常停止，不是故障：不推 error 事件、不记错误日志。
            logger.warning("流式诊断被客户端中止: %s", trace_id)
            raise
        except Exception as exc:  # noqa: BLE001 - 流已开始，只能以事件形式报错
            logger.error("流式诊断失败: %s", exc)
            yield _sse("error", {"error": type(exc).__name__, "detail": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _jobs(state: AppState) -> JobRegistry:
    """取出任务表，缺失时惰性创建。

    Args:
        state: 应用状态容器。

    Returns:
        进程内任务表；lifespan 之外构造的 AppState（如测试）也能直接用。
    """
    registry = getattr(state, "jobs", None)
    if registry is None:
        registry = JobRegistry()
        state.jobs = registry
    return registry


async def _run_ingest(
    state: AppState,
    registry: JobRegistry,
    doc_id: str,
    filename: str,
    payload: tuple[bytes, str, str],
) -> None:
    """后台执行单个文件的摄取，并把阶段进度写回任务表。

    这是「上传即受理」的执行侧：请求线程只负责建任务，真正的解析、分块、
    向量化与实体抽取在这里跑，因此前端随时能查到「卡在哪一步、到几成」。

    Args:
        state: 应用状态容器。
        registry: 任务表。
        doc_id: 任务主键，同时作为入库文档标识。
        filename: 原始文件名，仅用于日志与错误信息。
        payload: (字节流, 文件类型, 文件名) 三元组。

    Note:
        取消是协作式的：阶段回调处检查取消标记，一旦用户在等待中点取消，
        就抛 IngestCancelled 中止后续阶段，避免把算力白烧在已放弃的文件上。
    """

    def abort_if_cancelled() -> bool:
        """取消后统一收尾，返回是否已中止。

        取消是协作式的：阶段回调处与阶段返回处各查一次。只查回调处是不够的——
        用户可能在某个阶段执行的中途点取消，而该阶段直到跑完才回调，此时若不
        复查，任务会被已经放弃的文件重新改写成 success。
        """
        if not registry.is_cancelled(doc_id):
            return False
        registry.update(doc_id, status="cancelled", stage="cancelled", error="已取消")
        # 故意不在这里清标记：清掉之后，后续任何一个检查点都会重新认为「没取消」，
        # 取消就此失效。标记只由 retry_ingest_job 显式清除。
        return True

    def on_stage(stage: str, ratio: float) -> None:
        if abort_if_cancelled():
            raise IngestCancelled(doc_id)
        registry.update(doc_id, status="running", stage=stage, progress=ratio)

    from core.preprocessing import ingest_documents_with_progress

    registry.update(doc_id, status="running", stage="parse", progress=0.05)
    try:
        # 摄取含分块与 LLM 实体抽取，是同步阻塞操作；放线程池执行，
        # 否则会占满事件循环，健康检查等请求全部无响应。
        result = await asyncio.to_thread(
            ingest_documents_with_progress, [payload], on_stage, doc_id
        )
    except IngestCancelled:
        abort_if_cancelled()
        return
    except Exception as exc:  # noqa: BLE001 - 失败要落表，前端才看得到原因
        logger.error("文档摄取失败 [%s]: %s", filename, exc)
        registry.update(doc_id, status="failed", stage="failed", error=str(exc))
        return

    # 阶段跑完后复查：取消可能落在「已经进线程、尚未回调」的窗口里。
    if abort_if_cancelled():
        return

    try:
        if state.vector_store is not None and len(result.chunks):
            state.vector_store.add(result.embeddings, result.chunks)
        if state.graph_store is not None and result.entities:
            state.graph_store.add_entities(result.entities)
        # 摄取成功即落盘，使知识库跨重启存活（目录在项目之外，不污染项目树）。
        # 落盘属增强而非核心：存储实现可能不支持（如测试替身），或磁盘临时不可写，
        # 这些都不应让已经成功的摄取白做，故整体降级为告警。
        from src.settings import get_settings

        data_dir = get_settings().RAG_DATA_DIR
        if data_dir and state.vector_store is not None and hasattr(state.vector_store, "save"):
            try:
                await asyncio.to_thread(state.vector_store.save, data_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("知识库落盘失败，本次摄取仍成功: %s", exc)
        if state.retriever is not None:
            # 必须用全量文档重建：若只用本次上传的 chunks，BM25 索引与
            # chunk_map 会丢弃此前入库的知识——词法路召回不到旧文档，
            # 重排序也无法按 chunk_id 回溯旧分块（表现为"上传后旧知识失效"）。
            await asyncio.to_thread(
                state.retriever.build_bm25_index, state.vector_store.all_documents()
            )
    except Exception as exc:  # noqa: BLE001 - 入库阶段失败同样如实落表
        logger.error("文档入库失败 [%s]: %s", filename, exc)
        registry.update(doc_id, status="failed", stage="failed", error=str(exc))
        return

    registry.update(
        doc_id,
        status="success",
        stage="done",
        progress=1.0,
        chunk_count=len(result.chunks),
        entity_count=len(result.entities),
    )


@router.post(
    "/upload",
    response_model=UploadAcceptedResponse,
    status_code=202,
    dependencies=_RAG_WRITE_DEPS,
)
async def upload_endpoint(
    request: Request, files: list[UploadFile] = File(...)
) -> UploadAcceptedResponse:
    """上传运维文档并立即受理（202）。

    文件字节流经 await file.read() 直接读入内存，全程不落磁盘。受理后每个文件
    建一个后台任务，解析、分块、向量化与实体抽取都在任务里跑，请求不等结果——
    再大的文件也不会让界面卡在无反馈的等待里。

    Args:
        request: 当前请求。
        files: 上传的文件列表。

    Returns:
        202 与每个文件的任务快照（doc_id/status/stage/progress）。

    Raises:
        HTTPException: 组件未就绪（503）或未提供文件（400）。
    """
    state = _state(request)
    _require(state, "vector_store")
    registry = _jobs(state)

    payloads: list[tuple[str, bytes, str, str]] = []
    for upload in files:
        content = await upload.read()  # 零磁盘：直接取字节流
        name = upload.filename or "unnamed"
        file_type = name.rsplit(".", 1)[-1].lower()
        payloads.append((uuid.uuid4().hex, content, file_type, name))

    if not payloads:
        raise HTTPException(status_code=400, detail="no file provided")

    jobs: list[dict[str, Any]] = []
    for doc_id, content, file_type, name in payloads:
        # 留存内存副本，供失败后重试（重试不必让用户重新选文件）。
        state.upload_payloads[doc_id] = (content, file_type, name)
        job = registry.create(doc_id, name)
        jobs.append(job.to_dict())
        # 任务表持有任务对象、事件循环持有协程引用，无需额外保存 task 句柄；
        # 任何失败都落回任务表，前端轮询即可看到原因。
        asyncio.create_task(  # noqa: RUF006
            _run_ingest(state, registry, doc_id, name, (content, file_type, name))
        )

    return UploadAcceptedResponse(status="queued", jobs=jobs)


@router.get("/knowledge/jobs")
async def list_ingest_jobs(request: Request) -> dict[str, Any]:
    """列出全部摄取任务（新任务在前）。

    Args:
        request: 当前请求。

    Returns:
        jobs 列表与 total；每条含 status/stage/progress/error/chunk_count。
    """
    state = _state(request)
    registry = _jobs(state)
    jobs = [job.to_dict() for job in registry.list()]
    return {"jobs": jobs, "total": len(jobs)}


@router.get("/knowledge/jobs/{doc_id}")
async def get_ingest_job(request: Request, doc_id: str) -> dict[str, Any]:
    """查询单个摄取任务的进度。

    Args:
        request: 当前请求。
        doc_id: 任务标识。

    Returns:
        任务快照（status/stage/progress/error 等）。

    Raises:
        HTTPException: 任务不存在（404）。
    """
    state = _state(request)
    job = _jobs(state).get(doc_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@router.post("/knowledge/jobs/{doc_id}/cancel", dependencies=_RAG_WRITE_DEPS)
async def cancel_ingest_job(request: Request, doc_id: str) -> dict[str, Any]:
    """请求取消摄取任务（协作式：在阶段边界生效，不必等整份文档跑完）。

    Args:
        request: 当前请求。
        doc_id: 任务标识。

    Returns:
        取消后的任务快照。

    Raises:
        HTTPException: 任务不存在（404）或已进入终态（409）。
    """
    state = _state(request)
    registry = _jobs(state)
    if registry.get(doc_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not registry.cancel(doc_id):
        raise HTTPException(status_code=409, detail="job already finished")
    return registry.get(doc_id).to_dict()  # type: ignore[union-attr]


@router.post("/knowledge/jobs/{doc_id}/retry", dependencies=_RAG_WRITE_DEPS)
async def retry_ingest_job(request: Request, doc_id: str) -> dict[str, Any]:
    """重试摄取任务（复用同一 doc_id 与内存中的文件副本）。

    Args:
        request: 当前请求。
        doc_id: 任务标识。

    Returns:
        重新排队后的任务快照。

    Raises:
        HTTPException: 任务不存在（404）、仍在运行（409）或文件副本已回收（410）。

    Note:
        重试前先清掉该 doc_id 的旧分块：否则重跑会把同一份内容再入库一遍，
        表现为「重试成功但检索结果里同段话出现两次」。
    """
    state = _state(request)
    registry = _jobs(state)
    job = registry.get(doc_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="job still running")
    payload = getattr(state, "upload_payloads", {}).get(doc_id)
    if payload is None:
        raise HTTPException(status_code=410, detail="原文件已不在内存中，请重新上传")

    if state.vector_store is not None and hasattr(state.vector_store, "delete_document"):
        removed = state.vector_store.delete_document(doc_id)
        if removed and state.retriever is not None:
            state.retriever.build_bm25_index(state.vector_store.all_documents())

    registry.clear_cancel(doc_id)
    registry.update(
        doc_id,
        status="queued",
        stage="queued",
        progress=0.0,
        error=None,
        chunk_count=0,
        entity_count=0,
    )
    content, file_type, name = payload
    asyncio.create_task(  # noqa: RUF006
        _run_ingest(state, registry, doc_id, job.filename, (content, file_type, name))
    )
    return registry.get(doc_id).to_dict()  # type: ignore[union-attr]


@router.get("/knowledge/documents")
async def list_knowledge_documents(request: Request) -> dict[str, Any]:
    """列出知识库中的文档（按 doc_id 聚合）。

    Args:
        request: 当前请求。

    Returns:
        documents（含 doc_id/filename/chunks/chars/char_length）、
        total_documents 与 total_chunks；chunks 由分块实时计数，与库内一致。
    """
    state = _state(request)
    _require(state, "vector_store")
    documents = state.vector_store.list_documents()
    return {
        "documents": documents,
        "total_documents": len(documents),
        "total_chunks": state.vector_store.count(),
    }


@router.delete("/knowledge/documents/{doc_id}", dependencies=_RAG_WRITE_DEPS)
async def delete_knowledge_document(request: Request, doc_id: str) -> dict[str, Any]:
    """删除某文档的全部分块，并同步重建 BM25 索引与落盘快照。

    Args:
        request: 当前请求。
        doc_id: 文档标识。

    Returns:
        删除的分块数与删除后的总块数。

    Raises:
        HTTPException: 组件未就绪（503）或文档不存在（404）。

    Note:
        删除后必须重建 BM25 索引：词法路持有独立的分块副本，不重建就会
        继续召回已删文档，表现为「界面已删、检索仍命中」的幽灵结果。
    """
    state = _state(request)
    _require(state, "vector_store")
    removed = state.vector_store.delete_document(doc_id)
    if removed == 0:
        raise HTTPException(status_code=404, detail="document not found")
    if state.retriever is not None:
        state.retriever.build_bm25_index(state.vector_store.all_documents())
    from src.settings import get_settings

    data_dir = get_settings().RAG_DATA_DIR
    if data_dir and hasattr(state.vector_store, "save"):
        try:
            await asyncio.to_thread(state.vector_store.save, data_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除后落盘失败（不影响本次删除）: %s", exc)
    return {
        "status": "deleted",
        "doc_id": doc_id,
        "removed_chunks": removed,
        "total_chunks": state.vector_store.count(),
    }


@router.get("/knowledge/documents/{doc_id}/chunks")
async def list_knowledge_chunks(request: Request, doc_id: str) -> dict[str, Any]:
    """列出某文档的全部分块（含字符区间，供界面定位与高亮）。

    Args:
        request: 当前请求。
        doc_id: 文档标识。

    Returns:
        按 chunk_index 升序的分块列表，每项带 chunk_id / 字符区间 /
        locatable 标志（无区间时为 false，前端据此显式降级而非给出点不动的引用）。
    """
    state = _state(request)
    _require(state, "vector_store")
    chunks: list[dict[str, Any]] = []
    for chunk in state.vector_store.all_documents():
        meta = chunk.metadata or {}
        if not document_matches(meta, doc_id):
            continue
        start = meta.get("char_start")
        end = meta.get("char_end")
        chunks.append(
            {
                "chunk_id": str(meta.get("chunk_id") or ""),
                "doc_id": doc_id,
                "kb_id": str(meta.get("kb_id") or "default"),
                "filename": str(meta.get("filename") or ""),
                "chunk_index": int(meta.get("chunk_index") or 0),
                "char_start": start,
                "char_end": end,
                "locatable": start is not None and end is not None,
                "degrade_reason": "" if start is not None else "no_text_coordinate",
                "content": chunk.page_content,
            }
        )
    chunks.sort(key=lambda item: item["chunk_index"])
    return {"doc_id": doc_id, "chunks": chunks, "count": len(chunks)}


@router.post("/retrieve")
async def retrieve_endpoint(request: Request, body: RetrieveRequest) -> dict[str, Any]:
    """仅执行混合检索，供调参面板使用。

    Args:
        request: 当前请求。
        body: 调参请求体。

    Returns:
        候选文档记录与检索统计。

    Raises:
        HTTPException: 检索器未就绪时返回 503。
    """
    state = _state(request)
    _require(state, "retriever")

    results = state.retriever.retrieve(body.query)
    return {
        "query": body.query,
        "count": len(results),
        "documents": [_citation(document, score) for document, score in results],
    }


async def _run_evaluation(state: AppState) -> None:
    """后台执行黄金测试集评估。

    Args:
        state: 应用状态容器。

    Note:
        评估结果仅写入 state.evaluation_result，不落任何文件。
    """
    from src.evaluation.evaluator import evaluate_rag, generate_golden_samples

    state.evaluation_status = "running"
    try:
        report = await evaluate_rag(state.agent_graph, generate_golden_samples())
        state.evaluation_result = {
            "overall_metrics": report.overall_metrics,
            "single_hop_metrics": report.single_hop_metrics,
            "multi_hop_metrics": report.multi_hop_metrics,
            "passed": report.passed,
            "sample_count": len(report.per_sample_results),
        }
        state.evaluation_status = "done"
    except Exception as exc:  # noqa: BLE001 - 后台任务失败仅记录
        logger.error("评估任务失败: %s", exc)
        state.evaluation_status = "failed"
        state.evaluation_result = {"error": str(exc)}


@router.post("/evaluate", dependencies=_RAG_WRITE_DEPS)
async def trigger_evaluate(request: Request) -> dict[str, str]:
    """触发后台评估任务。

    Args:
        request: 当前请求。

    Returns:
        含任务状态与当前评估状态的字典。

    Raises:
        HTTPException: 组件未就绪时返回 503。
    """
    state = _state(request)
    _require(state, "agent_graph")

    asyncio.create_task(_run_evaluation(state))
    return {"status": "running", "evaluation_status": state.evaluation_status}


@router.get("/evaluate/results")
async def get_evaluate_results(request: Request) -> dict[str, Any]:
    """获取评估结果。

    Args:
        request: 当前请求。

    Returns:
        无结果时 status 为 no_result，否则返回评估指标。
    """
    state = _state(request)
    if state.evaluation_result is None:
        return {"status": "no_result", "evaluation_status": state.evaluation_status}
    return {"status": "ok", **state.evaluation_result}


@router.get("/trace/{trace_id}")
async def get_trace(request: Request, trace_id: str) -> dict[str, Any]:
    """按链路标识取明细。

    Args:
        request: 当前请求。
        trace_id: 链路标识。

    Returns:
        链路明细；未找到时 found 为 False。

    Note:
        明细存于内存，最多保留 MAX_TRACES 条。
    """
    state = _state(request)
    for record in reversed(state.traces):
        if record["trace_id"] == trace_id:
            return {"found": True, **record}
    return {"found": False, "trace_id": trace_id}


@router.post("/feedback", dependencies=_RAG_WRITE_DEPS)
async def post_feedback(request: Request, body: FeedbackRequest) -> dict[str, Any]:
    """提交人工反馈。

    Args:
        request: 当前请求。
        body: 反馈请求体。

    Returns:
        含接收状态与当前反馈总数的字典。
    """
    state = _state(request)
    state.feedback.append(
        {
            "trace_id": body.trace_id,
            "rating": body.rating,
            "comment": body.comment,
            "created_at": time.time(),
        }
    )
    return {"status": "accepted", "feedback_count": len(state.feedback)}


app.include_router(public_router)
app.include_router(router, dependencies=_RAG_READ_DEPS)

# FishCloud 平台链路（auth/assets/events/admin）：与既有 RAG 路由共存，互不影响。
from src.api.platform import platform_router  # noqa: E402 - 须在 app 定义后挂载

app.include_router(platform_router)
