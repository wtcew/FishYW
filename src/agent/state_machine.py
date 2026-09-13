"""Agent 状态机编排（LangGraph）。

实现五节点两路由的诊断状态机::

    意图拆解 → 混合检索 → 充分性反思 → 答案生成 → 幻觉校验 → END
                    ↑_________|          ↑_________|
              （充分性不足时回溯）    （幻觉未通过时重生成）

- 反思后由 :func:`route_after_reflect` 决定进入生成还是回溯到拆解；
- 幻觉校验后由 :func:`route_after_hallucination` 决定进入生成还是结束。

零磁盘约束（最高级别硬约束）：

* 中间状态只存在于 :class:`AgentState` 字典，随图流转，不做任何落盘；
* 检查点使用 :class:`langgraph.checkpoint.memory.MemorySaver`（进程内内存）；
* 语义缓存使用 :class:`cachetools.TTLCache`（``maxsize=100``、``ttl=300``）。

Note:
    模型凭据仅从环境变量读取（经 :mod:`src.settings` 注入），
    源码中不出现任何明文密钥。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import uuid4

from cachetools import TTLCache
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agent.tools import check_hallucination, format_sources, message_text
from src.settings import get_settings

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期引入检索层的重依赖
    from core.retrieval import HybridRetriever

logger = logging.getLogger(__name__)

#: 编译后的图类型别名。
CompiledGraph = CompiledStateGraph

#: 意图拆解提示词模板。
#:
#: Note:
#:     JSON 示例的花括号已转义为双花括号，以便 :meth:`str.format` 填充占位符。
#: 多轮对话最多携带的轮数（一轮 = 一问一答）。
MAX_HISTORY_TURNS: int = 6

#: 单条历史消息保留的最大字符数，避免历史挤占参考文档的上下文预算。
MAX_HISTORY_CHARS: int = 600


def _format_history(
    history: list[dict[str, str]] | None,
    max_turns: int = MAX_HISTORY_TURNS,
    hint: str = "",
) -> str:
    """把多轮对话历史格式化成可嵌入提示词的片段。

    Args:
        history: 形如 [{"role": "user"|"assistant", "content": "..."}] 的列表，按时间正序。
        max_turns: 最多保留的最近轮数。
        hint: 仅在存在历史时追加的提示句。单轮问答里若仍写着「结合上面的对话
            历史」，模型会去编造一段并不存在的历史，因此必须随之消失。

    Returns:
        提示词片段；无历史时返回空串，不留下多余空行。

    Note:
        追问（「那这个怎么处理」）单独看是检索不到的：指代与省略只能靠历史补全，
        因此历史要同时进「拆解」与「生成」两处提示词，而不是只喂给最终答案。
    """
    turns = [item for item in (history or []) if str(item.get("content") or "").strip()]
    if not turns:
        return ""
    lines: list[str] = []
    for item in turns[-max_turns * 2 :]:
        who = "用户" if item.get("role") == "user" else "助手"
        text = str(item.get("content") or "").strip()
        if len(text) > MAX_HISTORY_CHARS:
            text = text[:MAX_HISTORY_CHARS] + "…"
        lines.append(f"{who}：{text}")
    head = f"对话历史（最近 {len(lines)} 条，仅用于理解指代，不得当作事实依据）："
    block = head + "\n" + "\n".join(lines)
    return block + ("\n" + hint if hint else "")


DECOMPOSE_PROMPT: str = """你是一名资深运维工程师（SRE），专注于软件项目与软件工程一线：应用服务与接口、发布与变更、依赖链路、日志与性能、数据库与中间件。请把用户的运维问题拆解为 2 到 4 个可独立检索的子问题，覆盖故障现象、可能根因与排查解决三个方向。
{history}
请以 JSON 格式返回：
{{
  "sub_queries": ["子问题1", "子问题2"]
}}
用户问题：{query}"""

#: 检索充分性反思提示词模板。
REFLECT_PROMPT: str = """你是一名资深运维工程师（SRE），正在评估手头证据能否支撑一次可靠的故障研判。请判断下列已检索文档是否足以回答用户问题。
请以 JSON 格式返回：
{{
  "sufficient": true/false,
  "reason": "判断理由",
  "missing": ["仍然缺失的信息"]
}}
用户问题：{query}
当前子问题：{sub_queries}
已检索文档：
{documents}"""

#: 答案生成提示词模板。
GENERATE_PROMPT: str = """你是一名资深运维工程师（SRE），长期处置软件项目生产环境的告警与故障（应用服务、接口、发布变更、依赖链路、数据库与性能问题）。请严格依据参考文档，以一线运维的口吻给出可落地的处置结论，不得引入参考文档之外的信息。
在每个结论句末尾用 chunk_id:xxx 标注依据的文档标识（xxx 取参考文档的 chunk_id）。
{history}
请以 JSON 格式返回：
{{
  "answer": "完整答案，含 chunk_id:xxx 引用标记"
}}
用户问题：{query}
参考文档：
{documents}"""

#: 重生成时附加的修正提示（仅在检出幻觉后重新生成时拼接）。
REGENERATE_HINT: str = """
上一轮答案经校验存在幻觉，幻觉句子：{sentences}
请依据参考文档重新作答，不得重复上述无依据的内容。"""

#: 图中各节点的推理档位（复杂逻辑，按项目约定取 high）。
NODE_REASONING_EFFORT: str = "high"

#: 单次拆解允许的最大子问题数。
MAX_SUB_QUERIES: int = 5

#: 单轮检索合并去重后保留的最大文档数。
MAX_RETRIEVED_DOCS: int = 20

#: 幻觉校验次数上限：达到上限即结束，不再重生成。
MAX_HALLUCINATION_CHECKS: int = 2

#: 提示词内单篇文档的最大字符数，避免上下文膨胀。
MAX_DOC_CHARS_IN_PROMPT: int = 400


class AgentState(TypedDict):
    """状态机在节点间流转的共享状态。

    状态仅存在于内存字典中，随图流转，不做任何持久化。

    Attributes:
        query: 用户原始问题。
        sub_queries: 意图拆解得到的子问题列表。
        retrieved_docs: 混合检索召回的文档记录列表。
        iterations: 已执行的反思轮次计数。
        answer: 生成的诊断答案（含引用标记，经格式化后为可读来源）。
        sources: 答案依据的来源记录列表。
        reflection: 充分性反思结果，含 ``sufficient`` / ``reason`` / ``missing``。
        error: 最近一次可恢复降级的说明；链路无降级时为 ``None``。
        has_hallucination: 最近一次幻觉校验是否检出幻觉。
        hallucination_checks: 已执行的幻觉校验次数，用于限定重生成上限。
        hallucination_sentences: 最近一次检出的幻觉句子，供重生成提示引用。
        history: 多轮对话历史（按时间正序），用于补全追问里的指代与省略。

    Note:
        ``has_hallucination`` / ``hallucination_checks`` / ``hallucination_sentences``
        是重生成上限语义所需的附加字段：节点在检出幻觉时递增计数并保留幻觉句子，
        路由据此在达到 ``MAX_HALLUCINATION_CHECKS`` 后结束，避免无限重试。
    """

    query: str
    sub_queries: list[str]
    retrieved_docs: list[dict]
    iterations: int
    answer: str
    sources: list[dict]
    reflection: dict
    error: str | None
    has_hallucination: bool
    hallucination_checks: int
    hallucination_sentences: list[str]
    history: list[dict[str, str]]


def _build_llm(reasoning_effort: str) -> ChatOpenAI | None:
    """构造状态机节点使用的 LLM 客户端。

    Args:
        reasoning_effort: 推理档位，取值 ``low`` / ``high`` / ``max``。

    Returns:
        已开启思考模式与 JSON 输出、指向 GLM 服务的 :class:`ChatOpenAI` 实例；
        凭据缺失等构造失败场景返回 ``None``。

    Note:
        凭据取自环境变量（经 :mod:`src.settings`），不落盘、不硬编码。
        构造失败不抛出，以免图在缺少凭据的环境下完全无法构建；
        此时节点经 :func:`_require_llm` 判空并走各自的降级分支。
    """
    try:
        settings = get_settings()
        if settings.DEEPSEEK_API_KEY:
            # 项目主模型：DeepSeek 官方 API（OpenAI 兼容协议）。
            # 它不接受 GLM 的 thinking 扩展与 reasoning_effort，多传会直接 400。
            return ChatOpenAI(
                model=settings.DEEPSEEK_MODEL_NAME,
                base_url=settings.DEEPSEEK_BASE_URL,
                api_key=settings.DEEPSEEK_API_KEY,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
        return ChatOpenAI(
            model=settings.GLM_MODEL_NAME,
            base_url=settings.GLM_BASE_URL,
            api_key=settings.GLM_API_KEY,
            reasoning_effort=reasoning_effort,
            # 只保留 response_format。不传 thinking：GLM 的思考模式本就默认开启
            # （实测 glm-4.5-flash 不传该字段仍返回 reasoning_content），而
            # model_kwargs 里的非标准字段会被透传给结构化输出的 Completions.parse()，
            # 触发 "unexpected keyword argument 'thinking'" 使节点全部降级。
            model_kwargs={"response_format": {"type": "json_object"}},
        )
    except Exception as exc:  # noqa: BLE001 - 凭据缺失不得阻断图构建
        logger.error("LLM 客户端构造失败，节点将以降级模式运行: %s", exc)
        return None


def _require_llm(llm: ChatOpenAI | None, node: str) -> ChatOpenAI:
    """校验 LLM 客户端可用。

    Args:
        llm: 待校验的客户端。
        node: 调用方节点名，用于错误信息。

    Returns:
        可用的客户端。

    Raises:
        RuntimeError: 客户端为 ``None``（凭据缺失或构造失败）。
    """
    if llm is None:
        raise RuntimeError(f"LLM client unavailable for node {node}")
    return llm


def _parse_json_object(response: object) -> dict[str, Any]:
    """解析 LLM 响应中的 JSON 对象。

    Args:
        response: LLM 响应对象。

    Returns:
        解析得到的字典。

    Raises:
        ValueError: 响应内容不是合法 JSON 对象。
    """
    payload = json.loads(message_text(response))
    if not isinstance(payload, dict):
        raise ValueError("LLM response is not a JSON object")
    return payload


def _document_record(document: object, score: float) -> dict[str, Any]:
    """把检索返回的文档对象规整为状态字典记录。

    Args:
        document: 检索返回的文档对象（``Document``）。
        score: 相关性分数。

    Returns:
        含 ``chunk_id`` / ``content`` / ``filename`` / ``chunk_index`` / ``score``
        以及溯源字段 ``doc_id`` / ``kb_id`` / ``char_start`` / ``char_end`` 的记录字典。

    Note:
        溯源字段必须在检索节点就带上：状态在图中流转时只认这些扁平字段，
        一旦在此丢弃，后面无论怎么拼都拿不到字符区间，前端引用就只剩
        「无法定位」——这正是引用可定位最容易断在中间环节的地方。
    """
    metadata = getattr(document, "metadata", None) or {}
    return {
        "chunk_id": metadata.get("chunk_id"),
        "content": getattr(document, "page_content", ""),
        "filename": metadata.get("filename"),
        "chunk_index": metadata.get("chunk_index"),
        "doc_id": metadata.get("doc_id"),
        "kb_id": metadata.get("kb_id"),
        "char_start": metadata.get("char_start"),
        "char_end": metadata.get("char_end"),
        "score": float(score),
    }


def _summarize_documents(documents: list[dict]) -> str:
    """把文档记录压缩为提示词可用的文本块。

    Args:
        documents: 文档记录列表。

    Returns:
        每条形如 ``[chunk_id=xxx 文件=yyy 第n段] 正文…`` 的多行文本；
        正文超过 ``MAX_DOC_CHARS_IN_PROMPT`` 时截断。
    """
    if not documents:
        return "（无）"
    lines: list[str] = []
    for document in documents:
        content = str(document.get("content") or "")[:MAX_DOC_CHARS_IN_PROMPT]
        lines.append(
            f"[chunk_id={document.get('chunk_id')} "
            f"文件={document.get('filename')} "
            f"第{document.get('chunk_index')}段] {content}"
        )
    return "\n".join(lines)


def decompose_node(state: AgentState, llm: ChatOpenAI | None) -> AgentState:
    """拆解子问题节点。

    调用 LLM 把原始问题拆解为若干子问题；解析失败或结果为空时，
    降级为「以原始问题作为唯一子问题」，保证链路继续。

    Args:
        state: 当前状态。
        llm: LLM 客户端；为 ``None`` 时走降级分支。

    Returns:
        写入 ``sub_queries`` 后的状态；降级时同时写入 ``error``。
    """
    query = str(state.get("query") or "")
    try:
        client = _require_llm(llm, "decompose")
        response = client.invoke(
            DECOMPOSE_PROMPT.format(
                query=query,
                history=_format_history(
                    state.get("history"),
                    hint=(
                        "注意：若用户问题含「它 / 这个 / 那个 / 上述」等指代，或省略了设备与"
                        "现象，必须结合上面的对话历史把子问题补全为可独立检索的完整问题。"
                    ),
                ),
            )
        )
        payload = _parse_json_object(response)
        sub_queries = [
            str(item).strip() for item in payload.get("sub_queries") or [] if str(item).strip()
        ]
        if not sub_queries:
            raise ValueError("empty sub_queries in LLM response")
    except Exception as exc:  # noqa: BLE001 - 拆解失败须降级而非中断
        logger.error("意图拆解失败，降级为原始问题: %s", exc)
        fallback = [query] if query else []
        return {**state, "sub_queries": fallback, "error": f"decompose degraded: {exc}"}
    return {**state, "sub_queries": sub_queries[:MAX_SUB_QUERIES]}


def retrieve_node(state: AgentState, retriever: HybridRetriever) -> AgentState:
    """检索节点。

    对每个子问题调用 ``retriever.retrieve``，按下标标识合并去重
    （同一分块保留最高分），并按分数降序截断至 ``MAX_RETRIEVED_DOCS``。

    Args:
        state: 当前状态。
        retriever: 混合检索器。

    Returns:
        写入 ``retrieved_docs`` 后的状态；检索异常时保留原文档并写入 ``error``。
    """
    sub_queries = list(state.get("sub_queries") or [])
    if not sub_queries:
        query = str(state.get("query") or "")
        sub_queries = [query] if query else []

    merged: dict[str, dict[str, Any]] = {}
    try:
        for sub_query in sub_queries:
            for document, score in retriever.retrieve(sub_query):
                record = _document_record(document, float(score))
                key = str(
                    record["chunk_id"]
                    or f"{record['filename']}#{record['chunk_index']}"
                )
                existing = merged.get(key)
                if existing is None or record["score"] > existing["score"]:
                    merged[key] = record
    except Exception as exc:  # noqa: BLE001 - 单轮检索失败不得中断链路
        logger.error("检索失败，保留既有召回结果: %s", exc)
        return {**state, "error": f"retrieve degraded: {exc}"}

    documents = sorted(merged.values(), key=lambda item: item["score"], reverse=True)
    logger.info("检索完成: sub_queries=%d docs=%d", len(sub_queries), len(documents))
    return {**state, "retrieved_docs": documents[:MAX_RETRIEVED_DOCS]}


def reflect_node(state: AgentState, llm: ChatOpenAI | None) -> AgentState:
    """反思节点。

    调用 LLM 判断检索是否充分，解析 JSON 写入 ``reflection``，并累加
    ``iterations``（反思轮次）。解析失败时降级为「充分」，使链路直接进入生成。

    Args:
        state: 当前状态。
        llm: LLM 客户端；为 ``None`` 时走降级分支。

    Returns:
        写入 ``reflection`` 与 ``iterations`` 后的状态；降级时同时写入 ``error``。
    """
    iterations = int(state.get("iterations") or 0) + 1
    try:
        client = _require_llm(llm, "reflect")
        response = client.invoke(
            REFLECT_PROMPT.format(
                query=state.get("query") or "",
                sub_queries=state.get("sub_queries") or [],
                documents=_summarize_documents(list(state.get("retrieved_docs") or [])),
            )
        )
        payload = _parse_json_object(response)
        missing = payload.get("missing") or []
        reflection = {
            "sufficient": bool(payload.get("sufficient", False)),
            "reason": str(payload.get("reason") or ""),
            "missing": [str(item) for item in missing] if isinstance(missing, list) else [],
        }
    except Exception as exc:  # noqa: BLE001 - 反思失败须降级而非中断
        logger.error("充分性反思失败，降级为充分以结束回溯: %s", exc)
        return {
            **state,
            "reflection": {"sufficient": True, "reason": "反思失败，降级为充分", "missing": []},
            "iterations": iterations,
            "error": f"reflect degraded: {exc}",
        }
    return {**state, "reflection": reflection, "iterations": iterations}


def generate_node(state: AgentState, llm: ChatOpenAI | None) -> AgentState:
    """生成节点。

    调用 LLM 依据召回文档生成答案，并把答案中的 ``chunk_id:xxx`` 标记
    渲染为可读来源；检出幻觉后的重生成会附加上一轮的幻觉句子作为修正提示。

    Args:
        state: 当前状态。
        llm: LLM 客户端；为 ``None`` 时走降级分支。

    Returns:
        写入 ``answer`` 与 ``sources`` 后的状态；生成失败时保留原答案并写入 ``error``。
    """
    documents = list(state.get("retrieved_docs") or [])
    # sources 契约（前端 Agent 链路/研判证据面板消费）：除标识字段外，
    # 还须带 score / locatable / snippet——此前只回 chunk_id/filename/chunk_index，
    # 导致链路页相关度恒显示「—」、每条都被误标「不可定位」
    # （2026-09-13 UI 穷举测试 B-5）。
    sources = [
        {
            "chunk_id": document.get("chunk_id"),
            "filename": document.get("filename"),
            "chunk_index": document.get("chunk_index"),
            "score": document.get("score"),
            "locatable": bool(document.get("chunk_id")),
            "snippet": str(document.get("content") or "")[:200],
        }
        for document in documents
    ]
    prompt = GENERATE_PROMPT.format(
        query=state.get("query") or "",
        history=_format_history(state.get("history")),
        documents=_summarize_documents(documents),
    )
    if state.get("has_hallucination"):
        prompt += REGENERATE_HINT.format(
            sentences=state.get("hallucination_sentences") or "（未提供）"
        )
    try:
        client = _require_llm(llm, "generate")
        response = client.invoke(prompt)
        payload = _parse_json_object(response)
        raw_answer = str(payload.get("answer") or "").strip()
        if not raw_answer:
            raise ValueError("empty answer in LLM response")
    except Exception as exc:  # noqa: BLE001 - 生成失败须保留既有答案
        logger.error("答案生成失败，保留上一轮答案: %s", exc)
        return {**state, "error": f"generate degraded: {exc}"}
    return {
        **state,
        "answer": format_sources(raw_answer, sources),
        "sources": sources,
        "has_hallucination": False,
    }


def hallucination_check_node(state: AgentState) -> AgentState:
    """幻觉校验节点。

    调用 :func:`check_hallucination` 核查答案；检出幻觉且校验次数未达
    ``MAX_HALLUCINATION_CHECKS`` 时，用修正后的答案覆盖 ``answer``
    并递增 ``hallucination_checks``。

    Args:
        state: 当前状态。

    Returns:
        写入 ``has_hallucination`` / ``hallucination_checks``（必要时含
        ``answer``）后的状态。答案为空时跳过校验，避免无谓的 LLM 调用。
    """
    answer = str(state.get("answer") or "")
    checks = int(state.get("hallucination_checks") or 0)
    if not answer.strip():
        logger.warning("答案为空，跳过幻觉校验")
        return {**state, "has_hallucination": False, "hallucination_checks": checks}

    result = check_hallucination(answer, list(state.get("retrieved_docs") or []))
    if result.has_hallucination and checks < MAX_HALLUCINATION_CHECKS:
        corrected = result.corrected_answer.strip() or answer
        logger.warning("检出幻觉，采用修正答案: checks=%d", checks + 1)
        return {
            **state,
            "answer": corrected,
            "hallucination_sentences": result.hallucinated_sentences,
            "has_hallucination": True,
            "hallucination_checks": checks + 1,
        }
    return {
        **state,
        "has_hallucination": result.has_hallucination,
        "hallucination_checks": checks,
    }


def route_after_reflect(state: AgentState) -> str:
    """反思后路由。

    检索充分或反思轮次已达 ``MAX_AGENT_ITERATIONS`` 时进入生成，
    否则回到拆解节点重新规划子问题（回溯）。

    Args:
        state: 当前状态。

    Returns:
        ``"generate"`` 或 ``"decompose"``。
    """
    reflection = state.get("reflection") or {}
    sufficient = bool(reflection.get("sufficient", False))
    iterations = int(state.get("iterations") or 0)
    if sufficient or iterations >= get_settings().MAX_AGENT_ITERATIONS:
        return "generate"
    return "decompose"


def route_after_hallucination(state: AgentState) -> str:
    """幻觉校验后路由。

    无幻觉或校验次数已达 ``MAX_HALLUCINATION_CHECKS`` 时结束，
    否则回到生成节点重新作答。

    Args:
        state: 当前状态。

    Returns:
        ``"end"`` 或 ``"regenerate"``。
    """
    has_hallucination = bool(state.get("has_hallucination", False))
    checks = int(state.get("hallucination_checks") or 0)
    if has_hallucination and checks < MAX_HALLUCINATION_CHECKS:
        return "regenerate"
    return "end"


def _bind(node: Callable[..., AgentState], **dependencies: Any) -> Callable[[AgentState], AgentState]:
    """把节点依赖（LLM / 检索器）绑定到只接收状态的图节点函数。

    Args:
        node: 形如 ``(state, dep...) -> AgentState`` 的节点函数。
        **dependencies: 需要绑定的依赖对象。

    Returns:
        仅接收状态字典的可调用对象，供 ``StateGraph.add_node`` 使用。
    """

    def runner(state: AgentState) -> AgentState:
        return node(state, **dependencies)

    runner.__name__ = getattr(node, "__name__", "node")
    return runner


def build_agent_graph(retriever: HybridRetriever) -> CompiledGraph:
    """构建 LangGraph 状态机。

    节点连接：``decompose → retrieve → reflect →（条件）generate →
    hallucination_check →（条件）END``；反思不足时条件边回到 ``decompose``，
    幻觉未通过时条件边回到 ``generate``。检查点使用进程内 :class:`MemorySaver`。

    Args:
        retriever: 混合检索器，供检索节点调用。

    Returns:
        已编译的图对象（带内存检查点）。

    Raises:
        RuntimeError: 图构建或编译失败。
    """
    try:
        llm = _build_llm(NODE_REASONING_EFFORT)
        workflow: StateGraph = StateGraph(AgentState)
        workflow.add_node("decompose", _bind(decompose_node, llm=llm))
        workflow.add_node("retrieve", _bind(retrieve_node, retriever=retriever))
        workflow.add_node("reflect", _bind(reflect_node, llm=llm))
        workflow.add_node("generate", _bind(generate_node, llm=llm))
        workflow.add_node("hallucination_check", hallucination_check_node)

        workflow.set_entry_point("decompose")
        workflow.add_edge("decompose", "retrieve")
        workflow.add_edge("retrieve", "reflect")
        workflow.add_conditional_edges(
            "reflect",
            route_after_reflect,
            {"generate": "generate", "decompose": "decompose"},
        )
        workflow.add_edge("generate", "hallucination_check")
        workflow.add_conditional_edges(
            "hallucination_check",
            route_after_hallucination,
            {"regenerate": "generate", "end": END},
        )
        graph = workflow.compile(checkpointer=MemorySaver())
    except Exception as exc:  # noqa: BLE001 - 统一包装为 RuntimeError
        logger.error("状态图构建失败: %s", exc)
        raise RuntimeError(f"Failed to build agent graph: {exc}") from exc
    logger.info("Agent 状态图已构建并编译（MemorySaver 检查点）")
    return graph


#: 语义缓存：TTL 300 秒、容量 100，仅存于进程内存。
_semantic_cache: TTLCache = TTLCache(maxsize=100, ttl=300)


def initial_state(
    query: str, history: list[dict[str, str]] | None = None
) -> AgentState:
    """构造状态机的初始状态。

    Args:
        query: 用户运维诊断问题。

    Returns:
        字段齐备的初始 :class:`AgentState`。
    """
    return {
        "query": query,
        "history": list(history or []),
        "sub_queries": [],
        "retrieved_docs": [],
        "iterations": 0,
        "answer": "",
        "sources": [],
        "reflection": {},
        "error": None,
        "has_hallucination": False,
        "hallucination_checks": 0,
        "hallucination_sentences": [],
    }


def _cache_key(query: str, history: list[dict[str, str]] | None) -> str:
    """语义缓存的键：多轮下必须带历史指纹。

    只按 query 做键，会让「同一句话出现在不同对话里」命中彼此的缓存——
    第二轮追问拿到第一轮的结果，是多轮场景下最不容易察觉的错误。
    """
    turns = history or []
    if not turns:
        return query
    tail = str(turns[-1].get("content") or "")[:120]
    return query + "||" + str(len(turns)) + "||" + tail


async def run_agent(
    query: str,
    graph: CompiledGraph,
    history: list[dict[str, str]] | None = None,
    on_node: Callable[[str], None] | None = None,
) -> dict:
    """Agent 异步运行入口。

    先查语义缓存，命中则直接返回缓存结果、**跳过全部 LLM 调用**；
    未命中则执行状态机并把最终状态写入缓存。

    Args:
        query: 用户运维诊断问题。
        graph: :func:`build_agent_graph` 编译得到的图对象。
        history: 多轮历史（可选）。
        on_node: 节点级回调（可选）。提供时改用 ``astream`` 逐节点推进，
            每完成一个节点以节点名回调一次（``hallucination_check`` 原样透传，
            由调用方决定是否改写事件名）；缓存命中时不会触发任何回调。

    Returns:
        最终状态字典（缓存命中时为其副本）。

    Raises:
        Exception: 图执行过程中的未捕获异常，由调用方决定降级策略。
    """
    key = _cache_key(query, history)
    cached = _semantic_cache.get(key)
    if cached is not None:
        logger.info("语义缓存命中，跳过图执行: query_chars=%d", len(query))
        return dict(cached)

    config = {"configurable": {"thread_id": uuid4().hex}}
    initial = initial_state(query, history)
    if on_node is None:
        result = await graph.ainvoke(initial, config)
        state = dict(result)
    else:
        state: dict = dict(initial)
        # stream_mode="updates"：每个节点结束时产出 {节点名: 增量状态}，
        # 增量合并后与 ainvoke 的最终状态等价。
        async for chunk in graph.astream(initial, config, stream_mode="updates"):
            for node, delta in chunk.items():
                if on_node is not None:
                    try:
                        on_node(str(node))
                    except Exception:  # noqa: BLE001 - 观察者回调不得中断研判
                        logger.warning("节点回调失败（已忽略）: node=%s", node)
                if isinstance(delta, dict):
                    state.update(delta)
    _semantic_cache[key] = state
    logger.info(
        "Agent 执行完成: iterations=%s docs=%d cached=True",
        state.get("iterations"),
        len(state.get("retrieved_docs") or []),
    )
    return dict(state)


def run_agent_sync(
    query: str, graph: CompiledGraph, history: list[dict[str, str]] | None = None
) -> dict:
    """同步包装：在无事件循环的调用场景下运行 :func:`run_agent`。

    Args:
        query: 用户运维诊断问题。
        graph: 已编译的图对象。

    Returns:
        最终状态字典。
    """
    return asyncio.run(run_agent(query, graph, history))
