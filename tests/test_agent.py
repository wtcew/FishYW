"""Agent 集成验证（pytest）。

与 :mod:`tests.test_agent_state_machine` 的分工：

* 后者是**单元测试**——逐节点、逐路由函数验证，依赖 monkeypatch 注入替身；
* 本文件是**集成验证**——编译真实图并检查其结构（节点、静态边、条件边、
  入口与检查点），并在集成层面补上任务验收点名、而单元测试未覆盖的
  「LLM 超时」降级路径。

三个验证维度：

1. **图结构** —— build_agent_graph 返回真实 CompiledStateGraph，
   五个业务节点齐备，三条静态边与两条条件边的落点与设计一致；
2. **检查点** —— 编译使用 MemorySaver，全内存、无磁盘落地；
3. **超时降级** —— check_hallucination 在 TimeoutError /
   httpx.TimeoutException / ConnectionError 以及**构造阶段**超时时，
   一律降级为 has_hallucination=False 而非抛出。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/test_agent.py
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from src.agent import state_machine, tools
from src.agent.state_machine import build_agent_graph
from src.agent.tools import HallucinationResult, check_hallucination

#: 设计要求的五个业务节点。
EXPECTED_NODES = frozenset(
    {"decompose", "retrieve", "reflect", "generate", "hallucination_check"}
)

#: 降级后的期望结果：未检出幻觉、无句子、无修正答案。
DEGRADED = HallucinationResult(False, [], "")


class StubRetriever:
    """占位检索器：仅满足构造签名，图构建期不会被调用。"""

    def retrieve(self, query: str) -> list[Any]:  # noqa: ARG002
        """返回空候选列表。

        Args:
            query: 查询文本（本替身忽略）。

        Returns:
            空列表。
        """
        return []


def make_graph() -> CompiledStateGraph:
    """构建真实编译图（LLM 以 None 注入，避免依赖凭据）。

    Returns:
        build_agent_graph 的编译产物。
    """
    with mock.patch.object(state_machine, "_build_llm", return_value=None):
        return build_agent_graph(StubRetriever())


def edge_pairs(drawn: Any) -> set[tuple[str, str]]:
    """把图的边列表规整为 (source, target) 集合。

    Args:
        drawn: CompiledStateGraph.get_graph 返回的结构快照。

    Returns:
        边端点二元组集合。
    """
    return {(edge.source, edge.target) for edge in drawn.edges}


# ═══════════ 维度一：图结构 ═══════════


def test_compiled_graph_type() -> None:
    """build_agent_graph 返回 LangGraph 编译产物。"""
    assert isinstance(make_graph(), CompiledStateGraph)


def test_graph_nodes_complete() -> None:
    """五个业务节点齐备（另含 LangGraph 自带的起止节点）。"""
    drawn = make_graph().get_graph()

    assert EXPECTED_NODES <= set(drawn.nodes)


def test_graph_entry_point_is_decompose() -> None:
    """入口点为 decompose。"""
    pairs = edge_pairs(make_graph().get_graph())

    assert ("__start__", "decompose") in pairs


def test_graph_static_edges() -> None:
    """三条静态边与设计一致。"""
    pairs = edge_pairs(make_graph().get_graph())

    assert ("decompose", "retrieve") in pairs
    assert ("retrieve", "reflect") in pairs
    assert ("generate", "hallucination_check") in pairs


def test_graph_conditional_edges_at_reflect() -> None:
    """reflect 的条件边分别通往 generate（充分）与 decompose（不足回检索）。"""
    pairs = edge_pairs(make_graph().get_graph())

    outgoing = {target for source, target in pairs if source == "reflect"}

    assert {"generate", "decompose"} <= outgoing


def test_graph_conditional_edges_at_hallucination() -> None:
    """hallucination_check 的条件边分别通往 generate（重生成）与结束节点。"""
    pairs = edge_pairs(make_graph().get_graph())

    outgoing = {target for source, target in pairs if source == "hallucination_check"}

    assert "generate" in outgoing
    assert outgoing & {"__end__", "end"}


# ═══════════ 维度二：检查点（无磁盘落地） ═══════════


def test_graph_checkpointer_is_memory_saver() -> None:
    """编译使用进程内 MemorySaver，全内存、不落盘。"""
    graph = make_graph()

    assert graph.checkpointer is not None
    assert isinstance(graph.checkpointer, MemorySaver)


# ═══════════ 维度三：超时降级 ═══════════


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("llm request timed out"),
        httpx.TimeoutException("read timeout"),
        httpx.ConnectTimeout("connect timeout"),
        ConnectionError("connection reset by peer"),
    ],
    ids=["builtin-timeout", "httpx-timeout", "httpx-connect-timeout", "connection-error"],
)
def test_check_hallucination_degrades_on_timeout(error: Exception) -> None:
    """LLM 超时或连接失败时降级为未检出幻觉，绝不抛出。

    Args:
        error: 待注入的异常实例。
    """
    llm = mock.Mock()
    llm.invoke.side_effect = error

    with mock.patch.object(tools, "ChatOpenAI", mock.Mock(return_value=llm)):
        result = check_hallucination("答案", [])

    assert result == DEGRADED


def test_check_hallucination_degrades_when_constructor_times_out() -> None:
    """ChatOpenAI 构造阶段即超时时同样降级（构造位于 try 块内）。"""
    with mock.patch.object(
        tools, "ChatOpenAI", side_effect=TimeoutError("connect timeout")
    ):
        result = check_hallucination("答案", [])

    assert result == DEGRADED
