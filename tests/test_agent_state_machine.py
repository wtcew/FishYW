"""Agent 状态机与工具集测试（pytest）。

覆盖 Goal 的三项验收检查点：

1. **build_agent_graph 成功返回 CompiledGraph 对象** —— 含凭据缺失环境下的可构建性；
2. **路由函数边界跳转** —— 反思达 ``MAX_AGENT_ITERATIONS``、幻觉达
   ``MAX_HALLUCINATION_CHECKS`` 时正确转向；
3. **语义缓存命中时跳过 LLM 调用** —— 直接对 LLM 调用计数断言。

另覆盖节点行为（拆解降级、检索合并去重、反思轮次、生成引用格式化、
幻觉重生成上限）、``tools`` 的提示词渲染与降级，以及零磁盘 AST 静态扫描。

全部使用替身 LLM 与替身检索器，不发起真实请求、不加载真实权重。

运行方式::

    python -m pytest tests/test_agent_state_machine.py -p no:cacheprovider -q
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from types import SimpleNamespace
from unittest import mock

import pytest
from cachetools import TTLCache
from langchain_core.documents import Document
from langgraph.graph.state import CompiledStateGraph

from src.agent import state_machine, tools
from src.agent.state_machine import (
    MAX_HALLUCINATION_CHECKS,
    AgentState,
    _semantic_cache,
    build_agent_graph,
    decompose_node,
    generate_node,
    hallucination_check_node,
    initial_state,
    reflect_node,
    retrieve_node,
    route_after_hallucination,
    route_after_reflect,
    run_agent,
)
from src.agent.tools import HallucinationResult, check_hallucination, format_sources
from src.settings import get_settings

MODULE_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "agent"
BANNED_ATTRS = {
    "write", "write_text", "write_bytes", "dump", "dump_index", "write_index",
    "write_graphml", "to_disk", "shelve", "savez", "save",
}
BANNED_NAMES = {"open", "pickle"}

MAX_ITER = get_settings().MAX_AGENT_ITERATIONS


# ═══════════ 替身 ═══════════


def make_doc(chunk_id: str, content: str, filename: str = "ops.md", index: int = 0) -> Document:
    """构造带溯源元数据的文档对象。"""
    return Document(
        page_content=content,
        metadata={"chunk_id": chunk_id, "filename": filename, "chunk_index": index},
    )


class StubRetriever:
    """替身检索器：按键返回预定文档，或抛出预定异常。"""

    def __init__(
        self,
        results: dict[str, list[tuple[Document, float]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = results or {}
        self.error = error
        self.queries: list[str] = []

    def retrieve(self, query: str) -> list[tuple[Document, float]]:
        """返回该查询的预定召回结果。"""
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.results.get(query, []))


DEFAULT_RESULTS = {
    "重启原因": [(make_doc("c1", "电源模块老化导致重启", index=0), 0.91)],
    "风扇异常根因": [(make_doc("c2", "散热风道堵塞", index=1), 0.88)],
}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """每个用例前后清空语义缓存，避免跨用例串味。"""
    _semantic_cache.clear()
    yield
    _semantic_cache.clear()


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """注入替身 LLM：按提示词特征分派 JSON 响应并记录调用。"""
    prompts: list[str] = []
    holder = {"suff": True, "answer": "根因是电源模块老化。chunk_id:c1"}

    def _dispatch(prompt: str) -> SimpleNamespace:
        prompts.append(prompt)
        if "拆解" in prompt:
            return SimpleNamespace(content='{"sub_queries": ["重启原因", "风扇异常根因"]}')
        if "是否足以回答" in prompt:
            payload = {"sufficient": holder["suff"], "reason": "ok", "missing": []}
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        if "严格依据" in prompt:
            return SimpleNamespace(content=json.dumps({"answer": holder["answer"]}, ensure_ascii=False))
        return SimpleNamespace(content="{}")

    llm = mock.Mock()
    llm.invoke.side_effect = _dispatch
    monkeypatch.setattr(state_machine, "ChatOpenAI", mock.Mock(return_value=llm))
    return SimpleNamespace(llm=llm, prompts=prompts, holder=holder)


# ═══════════ 验收检查点 1：图构建 ═══════════


class TestBuildAgentGraph:
    """build_agent_graph 返回可用的编译图。"""

    def test_build_agent_graph_returns_compiled_graph(self, fake_llm: SimpleNamespace) -> None:
        """成功返回 CompiledGraph，五个节点齐备且可 ainvoke。"""
        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))

        assert isinstance(graph, CompiledStateGraph)
        assert hasattr(graph, "ainvoke")
        nodes = set(graph.get_graph().nodes)
        assert {"decompose", "retrieve", "reflect", "generate", "hallucination_check"} <= nodes

    def test_build_agent_graph_survives_missing_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """凭据缺失（LLM 构造失败）时仍须返回可编译图，节点走降级分支。"""
        monkeypatch.setattr(
            state_machine, "ChatOpenAI", mock.Mock(side_effect=RuntimeError("missing credentials"))
        )

        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))

        assert isinstance(graph, CompiledStateGraph)

    def test_semantic_cache_matches_spec(self) -> None:
        """语义缓存必须是 TTLCache(maxsize=100, ttl=300)。"""
        assert isinstance(_semantic_cache, TTLCache)
        assert _semantic_cache.maxsize == 100
        assert _semantic_cache.ttl == 300

    def test_initial_state_has_all_fields(self) -> None:
        """初始状态字段齐备且为中性默认值。"""
        state = initial_state("查询")
        assert set(AgentState.__annotations__) <= set(state)
        assert state["query"] == "查询"
        assert state["error"] is None


# ═══════════ 验收检查点 2：路由边界 ═══════════


class TestRouteAfterReflect:
    """反思后路由：充分或达最大迭代即生成，否则回溯。"""

    def _state(self, sufficient: bool, iterations: int) -> AgentState:
        state = initial_state("查询")
        state["reflection"] = {"sufficient": sufficient}
        state["iterations"] = iterations
        return state

    def test_sufficient_goes_generate(self) -> None:
        """充分时直接进入生成。"""
        assert route_after_reflect(self._state(True, 0)) == "generate"

    def test_insufficient_below_cap_loops_back(self) -> None:
        """不充分且未达上限时回溯到拆解。"""
        assert route_after_reflect(self._state(False, 0)) == "decompose"
        assert route_after_reflect(self._state(False, MAX_ITER - 1)) == "decompose"

    @pytest.mark.parametrize("iterations", [MAX_ITER, MAX_ITER + 1])
    def test_max_iterations_boundary_goes_generate(self, iterations: int) -> None:
        """边界：迭代达到或超过上限时必须强制生成，杜绝无限回溯。"""
        assert route_after_reflect(self._state(False, iterations)) == "generate"

    def test_empty_reflection_is_bounded(self) -> None:
        """反思结果缺失时不抛异常，且行为受迭代上限约束。"""
        state = initial_state("查询")
        assert route_after_reflect(state) == "decompose"
        state["iterations"] = MAX_ITER
        assert route_after_reflect(state) == "generate"


class TestRouteAfterHallucination:
    """幻觉校验后路由：无幻觉或达校验上限即结束。"""

    def _state(self, has_hallucination: bool, checks: int) -> AgentState:
        state = initial_state("查询")
        state["has_hallucination"] = has_hallucination
        state["hallucination_checks"] = checks
        return state

    def test_no_hallucination_ends(self) -> None:
        """无幻觉直接结束。"""
        assert route_after_hallucination(self._state(False, 0)) == "end"

    def test_hallucination_below_cap_regenerates(self) -> None:
        """检出幻觉且未达上限时重生成。"""
        for checks in range(MAX_HALLUCINATION_CHECKS):
            assert route_after_hallucination(self._state(True, checks)) == "regenerate"

    @pytest.mark.parametrize("checks", [MAX_HALLUCINATION_CHECKS, MAX_HALLUCINATION_CHECKS + 1])
    def test_hallucination_at_cap_ends(self, checks: int) -> None:
        """边界：校验次数达上限后必须结束，杜绝无限重生成。"""
        assert route_after_hallucination(self._state(True, checks)) == "end"


# ═══════════ 验收检查点 3：语义缓存 ═══════════


class TestRunAgentCache:
    """run_agent 的缓存语义。"""

    def test_run_agent_cache_hit_skips_llm(self, fake_llm: SimpleNamespace) -> None:
        """同一查询第二次运行命中缓存，不再产生任何 LLM 调用。"""
        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))

        first = asyncio.run(run_agent("设备频繁重启", graph))
        calls_after_first = fake_llm.llm.invoke.call_count
        assert calls_after_first > 0, "首次运行应真实执行节点"

        second = asyncio.run(run_agent("设备频繁重启", graph))

        assert fake_llm.llm.invoke.call_count == calls_after_first
        assert second == first

    def test_different_query_misses_cache(self, fake_llm: SimpleNamespace) -> None:
        """不同查询不共享缓存，需重新执行。"""
        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))

        asyncio.run(run_agent("设备频繁重启", graph))
        calls = fake_llm.llm.invoke.call_count
        asyncio.run(run_agent("风扇转速异常", graph))

        assert fake_llm.llm.invoke.call_count > calls

    def test_cache_hit_returns_copy(self, fake_llm: SimpleNamespace) -> None:
        """缓存命中返回副本，外部改动不得污染缓存内容。"""
        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))
        asyncio.run(run_agent("设备频繁重启", graph))

        result = asyncio.run(run_agent("设备频繁重启", graph))
        result["answer"] = "被篡改"

        assert _semantic_cache["设备频繁重启"]["answer"] != "被篡改"


# ═══════════ 节点行为 ═══════════


class TestNodes:
    """五个节点的行为与降级路径。"""

    def test_decompose_node_parses_sub_queries(self, fake_llm: SimpleNamespace) -> None:
        """正常解析子问题。"""
        state = decompose_node(initial_state("设备频繁重启"), fake_llm.llm)
        assert state["sub_queries"] == ["重启原因", "风扇异常根因"]
        assert state["error"] is None

    def test_decompose_node_falls_back_on_invalid_json(self) -> None:
        """LLM 返回非 JSON 时降级为原始问题，并记录 error。"""
        llm = mock.Mock()
        llm.invoke.return_value = SimpleNamespace(content="这不是 JSON")
        state = decompose_node(initial_state("设备频繁重启"), llm)
        assert state["sub_queries"] == ["设备频繁重启"]
        assert "decompose degraded" in str(state["error"])

    def test_decompose_node_degrades_without_llm(self) -> None:
        """无 LLM 时不抛异常，降级为原始问题。"""
        state = decompose_node(initial_state("设备频繁重启"), None)
        assert state["sub_queries"] == ["设备频繁重启"]
        assert "LLM client unavailable" in str(state["error"])

    def test_retrieve_node_merges_and_dedupes(self) -> None:
        """多子问题召回合并去重，同一分块保留最高分并降序排列。"""
        retriever = StubRetriever(
            {
                "问题1": [(make_doc("c1", "甲", index=0), 0.70), (make_doc("c2", "乙", index=1), 0.95)],
                "问题2": [(make_doc("c1", "甲", index=0), 0.90)],
            }
        )
        state = initial_state("查询")
        state["sub_queries"] = ["问题1", "问题2"]

        result = retrieve_node(state, retriever)

        chunk_ids = [doc["chunk_id"] for doc in result["retrieved_docs"]]
        assert chunk_ids == ["c2", "c1"]
        assert result["retrieved_docs"][1]["score"] == pytest.approx(0.90)

    def test_retrieve_node_survives_failure(self) -> None:
        """检索抛异常时保留既有召回并记录 error。"""
        state = initial_state("查询")
        state["sub_queries"] = ["问题1"]
        state["retrieved_docs"] = [{"chunk_id": "old", "score": 0.5}]

        result = retrieve_node(state, StubRetriever(error=RuntimeError("retriever boom")))

        assert result["retrieved_docs"] == [{"chunk_id": "old", "score": 0.5}]
        assert "retrieve degraded" in str(result["error"])

    def test_reflect_node_counts_rounds(self, fake_llm: SimpleNamespace) -> None:
        """每轮反思累加 iterations 并写入 reflection。"""
        state = initial_state("查询")
        state["retrieved_docs"] = [{"chunk_id": "c1", "content": "内容"}]

        first = reflect_node(state, fake_llm.llm)
        second = reflect_node(first, fake_llm.llm)

        assert first["iterations"] == 1
        assert second["iterations"] == 2
        assert second["reflection"]["sufficient"] is True

    def test_reflect_node_degrades_to_sufficient(self) -> None:
        """反思失败时降级为充分（直接进入生成），不中断链路。"""
        llm = mock.Mock()
        llm.invoke.side_effect = RuntimeError("llm boom")
        state = reflect_node(initial_state("查询"), llm)
        assert state["reflection"]["sufficient"] is True
        assert "reflect degraded" in str(state["error"])

    def test_generate_node_formats_sources(self, fake_llm: SimpleNamespace) -> None:
        """生成节点渲染引用标记并回填 sources。"""
        state = initial_state("查询")
        state["retrieved_docs"] = [
            {
                "chunk_id": "c1",
                "content": "电源模块老化",
                "filename": "ops.md",
                "chunk_index": 0,
                "score": 0.9,
            }
        ]
        result = generate_node(state, fake_llm.llm)

        assert "[来源：ops.md 第0段]" in result["answer"]
        assert "chunk_id:c1" not in result["answer"]
        # sources 契约（2026-09-13 B-5）：除标识字段外须带 score/locatable/snippet，
        # 供前端 Agent 链路页与研判证据面板渲染相关度与"可定位"徽章。
        assert result["sources"] == [
            {
                "chunk_id": "c1",
                "filename": "ops.md",
                "chunk_index": 0,
                "score": 0.9,
                "locatable": True,
                "snippet": "电源模块老化",
            }
        ]

    def test_generate_node_appends_regenerate_hint(self, fake_llm: SimpleNamespace) -> None:
        """检出幻觉后重生成时，提示词携带上一轮幻觉句子。"""
        state = initial_state("查询")
        state["has_hallucination"] = True
        state["hallucination_sentences"] = ["无依据的结论"]
        state["retrieved_docs"] = [
            {"chunk_id": "c1", "content": "电源模块老化", "filename": "ops.md", "chunk_index": 0}
        ]
        generate_node(state, fake_llm.llm)

        assert "无依据的结论" in fake_llm.prompts[-1]

    def test_hallucination_node_skips_empty_answer(self) -> None:
        """答案为空时跳过校验，不产生 LLM 调用。"""
        with mock.patch.object(state_machine, "check_hallucination") as checker:
            result = hallucination_check_node(initial_state("查询"))
        checker.assert_not_called()
        assert result["has_hallucination"] is False

    def test_hallucination_node_updates_answer_and_counts(self) -> None:
        """检出幻觉时采用修正答案并递增校验次数。"""
        state = initial_state("查询")
        state["answer"] = "原始答案"
        with mock.patch.object(
            state_machine,
            "check_hallucination",
            return_value=HallucinationResult(True, ["幻觉句"], "修正答案"),
        ):
            result = hallucination_check_node(state)

        assert result["answer"] == "修正答案"
        assert result["has_hallucination"] is True
        assert result["hallucination_checks"] == 1
        assert result["hallucination_sentences"] == ["幻觉句"]

    def test_hallucination_node_stops_at_cap(self) -> None:
        """已达校验上限时不再更新答案、不再递增。"""
        state = initial_state("查询")
        state["answer"] = "原始答案"
        state["hallucination_checks"] = MAX_HALLUCINATION_CHECKS
        with mock.patch.object(
            state_machine,
            "check_hallucination",
            return_value=HallucinationResult(True, ["幻觉句"], "修正答案"),
        ):
            result = hallucination_check_node(state)

        assert result["answer"] == "原始答案"
        assert result["hallucination_checks"] == MAX_HALLUCINATION_CHECKS


# ═══════════ 端到端 ═══════════


class TestEndToEnd:
    """编译图端到端流转（替身 LLM + 替身检索器）。"""

    def test_full_flow_produces_sourced_answer(self, fake_llm: SimpleNamespace) -> None:
        """拆解→检索→反思→生成→校验→结束：答案带可读来源。"""
        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))

        with mock.patch.object(
            state_machine, "check_hallucination", return_value=HallucinationResult(False, [], "")
        ):
            state = asyncio.run(run_agent("设备频繁重启", graph))

        assert state["sub_queries"] == ["重启原因", "风扇异常根因"]
        assert "[来源：ops.md 第0段]" in state["answer"]
        assert state["sources"][0]["chunk_id"] == "c1"
        assert state["error"] is None

    def test_iteration_cap_limits_loop_backs(self, fake_llm: SimpleNamespace) -> None:
        """反思始终不充分时，回溯次数被 MAX_AGENT_ITERATIONS 截断。"""
        fake_llm.holder["suff"] = False
        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))

        with mock.patch.object(
            state_machine, "check_hallucination", return_value=HallucinationResult(False, [], "")
        ):
            state = asyncio.run(run_agent("设备频繁重启", graph))

        decompose_calls = sum("拆解" in prompt for prompt in fake_llm.prompts)
        assert state["iterations"] == MAX_ITER
        assert decompose_calls == MAX_ITER

    def test_hallucination_retry_cap_limits_regenerations(self, fake_llm: SimpleNamespace) -> None:
        """幻觉持续存在时，重生成次数受 MAX_HALLUCINATION_CHECKS 限制后结束。"""
        graph = build_agent_graph(StubRetriever(DEFAULT_RESULTS))

        with mock.patch.object(
            state_machine,
            "check_hallucination",
            return_value=HallucinationResult(True, ["幻觉句"], "修正后的答案"),
        ):
            state = asyncio.run(run_agent("设备频繁重启", graph))

        generate_calls = sum("严格依据" in prompt for prompt in fake_llm.prompts)
        # 首次生成 + 一次重生成，随后校验次数达上限而结束
        assert generate_calls == MAX_HALLUCINATION_CHECKS
        assert state["hallucination_checks"] == MAX_HALLUCINATION_CHECKS
        assert state["answer"] == "修正后的答案"


# ═══════════ tools ═══════════


class TestCheckHallucination:
    """check_hallucination 的构造、解析与降级。"""

    def test_prompt_renders_json_example_literally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """提示词须成功渲染：JSON 示例保留字面花括号，占位符被填充。

        回归用例：若模板中 JSON 花括号未转义，``str.format`` 会抛 KeyError
        并使校验恒走降级分支。
        """
        llm = mock.Mock()
        llm.invoke.return_value = SimpleNamespace(
            content='{"has_hallucination": false, "hallucinated_sentences": [], "corrected_answer": ""}'
        )
        monkeypatch.setattr(tools, "ChatOpenAI", mock.Mock(return_value=llm))

        check_hallucination("答案文本", [{"chunk_id": "c1"}])

        prompt = llm.invoke.call_args.args[0]
        assert '"has_hallucination"' in prompt
        assert "答案文本" in prompt
        assert '"chunk_id": "c1"' in prompt

    def test_llm_configured_with_glm_reasoning_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LLM 须以 GLM 配置 + reasoning_effort=max 构造，且**不得**传 thinking。

        2026-09-12 实测：``thinking`` 是非标准扩展字段，会被透传给结构化输出的
        ``Completions.parse()`` 并抛 ``unexpected keyword argument 'thinking'``，
        使幻觉校验永久降级；GLM 思考模式默认开启（不传仍返回 reasoning_content）。
        """
        constructor = mock.Mock(
            return_value=mock.Mock(
                invoke=mock.Mock(
                    return_value=SimpleNamespace(
                        content='{"has_hallucination": false, "hallucinated_sentences": [], '
                        '"corrected_answer": ""}'
                    )
                )
            )
        )
        monkeypatch.setattr(tools, "ChatOpenAI", constructor)
        monkeypatch.setattr(get_settings(), "DEEPSEEK_API_KEY", "")
        settings = get_settings()

        check_hallucination("答案", [])

        _, kwargs = constructor.call_args
        assert kwargs["model"] == settings.GLM_MODEL_NAME
        assert kwargs["base_url"] == settings.GLM_BASE_URL
        assert kwargs["api_key"] == settings.GLM_API_KEY
        assert kwargs["reasoning_effort"] == "max"
        assert "thinking" not in kwargs["model_kwargs"]
        assert kwargs["model_kwargs"]["response_format"] == {"type": "json_object"}

    def test_parses_valid_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """合法 JSON 正确解析为结果对象。"""
        llm = mock.Mock()
        llm.invoke.return_value = SimpleNamespace(
            content='{"has_hallucination": true, "hallucinated_sentences": ["句1", "句2"], '
            '"corrected_answer": "修正"}'
        )
        monkeypatch.setattr(tools, "ChatOpenAI", mock.Mock(return_value=llm))

        result = check_hallucination("答案", [])

        assert result == HallucinationResult(True, ["句1", "句2"], "修正")

    def test_degrades_on_invalid_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 JSON 响应降级为未检出幻觉。"""
        llm = mock.Mock()
        llm.invoke.return_value = SimpleNamespace(content="不是 JSON")
        monkeypatch.setattr(tools, "ChatOpenAI", mock.Mock(return_value=llm))

        result = check_hallucination("答案", [])

        assert result.has_hallucination is False
        assert result.corrected_answer == ""

    def test_degrades_on_llm_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LLM 调用异常降级为未检出幻觉。"""
        llm = mock.Mock()
        llm.invoke.side_effect = RuntimeError("api boom")
        monkeypatch.setattr(tools, "ChatOpenAI", mock.Mock(return_value=llm))

        assert check_hallucination("答案", []).has_hallucination is False

    def test_partial_payload_uses_safe_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """字段缺失时按安全默认值填充，不抛异常。"""
        llm = mock.Mock()
        llm.invoke.return_value = SimpleNamespace(content='{"has_hallucination": true}')
        monkeypatch.setattr(tools, "ChatOpenAI", mock.Mock(return_value=llm))

        result = check_hallucination("答案", [])

        assert result == HallucinationResult(True, [], "")


class TestFormatSources:
    """format_sources 的引用渲染。"""

    SOURCES = [{"chunk_id": "c1", "filename": "ops.md", "chunk_index": 2}]

    def test_replaces_known_marker(self) -> None:
        """已知 chunk_id 渲染为可读来源。"""
        rendered = format_sources("根因是电源老化。chunk_id:c1", self.SOURCES)
        assert rendered == "根因是电源老化。[来源：ops.md 第2段]"

    def test_keeps_unknown_marker(self) -> None:
        """未收录的标记保持原样，不破坏文本结构。"""
        text = "结论。chunk_id:unknown"
        assert format_sources(text, self.SOURCES) == text

    def test_supports_chinese_colon_and_spacing(self) -> None:
        """兼容中文冒号与空白。"""
        assert "[来源：ops.md 第2段]" in format_sources("结论 chunk_id ： c1", self.SOURCES)

    def test_empty_answer_and_empty_sources(self) -> None:
        """空答案原样返回；无来源时不改动文本。"""
        assert format_sources("", self.SOURCES) == ""
        assert format_sources("结论 chunk_id:c1", []) == "结论 chunk_id:c1"

    def test_missing_chunk_index_placeholder(self) -> None:
        """缺少 chunk_index 时使用占位段号。"""
        rendered = format_sources("结论 chunk_id:c9", [{"chunk_id": "c9", "filename": "a.md"}])
        assert rendered == "结论 [来源：a.md 第?段]"


# ═══════════ 零磁盘 ═══════════


class TestZeroDisk:
    """零磁盘静态扫描：源码禁出现任何落盘调用。"""

    @pytest.mark.parametrize("filename", ["state_machine.py", "tools.py"])
    def test_source_has_no_disk_api_calls(self, filename: str) -> None:
        """AST 扫描禁用 open / pickle / write / dump 等落盘 API。"""
        tree = ast.parse((MODULE_DIR / filename).read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRS:
                found.append(f"attr:{func.attr}")
            if isinstance(func, ast.Name) and func.id in BANNED_NAMES:
                found.append(f"name:{func.id}")
        assert found == []

    def test_checkpointer_is_memory_saver(self) -> None:
        """检查点必须使用进程内 MemorySaver。"""
        source = (MODULE_DIR / "state_machine.py").read_text(encoding="utf-8")
        assert "MemorySaver()" in source
        assert "checkpointer=" in source
