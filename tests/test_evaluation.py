"""RAGAS 评估体系测试（pytest）。

覆盖三条验收检查点与关键契约：

1. **import 测试** —— 模块可导入，且 ``EVALUATION_TARGETS`` 与
   :mod:`src.constants` 保持同源（不重复定义）；
2. **generate_golden_samples** —— 返回 200 条（100 单跳 + 100 多跳），字段齐备；
3. **generate_charts** —— 返回有效 PNG 字节流（文件头为 PNG 魔数）。

另附评估主流程的聚合、超时/异常降级，以及三处**接口契约**验证
（状态机输入键 ``query``、``thread_id`` 配置、RAGAS 结果取值路径）。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/test_evaluation.py
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from src.constants import EVALUATION_TARGETS
from src.evaluation import evaluator
from src.evaluation.evaluator import (
    EvaluationReport,
    GoldenSample,
    evaluate_rag,
    generate_charts,
    generate_golden_samples,
)

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "evaluation" / "evaluator.py"
)

BANNED_ATTRS = {
    "write", "write_text", "write_bytes", "dump", "savetxt", "to_csv", "to_json",
    "savez", "shelve", "to_disk",
}
BANNED_NAMES = {"open", "pickle"}

PNG_MAGIC = b"\x89PNG"


class RecordingGraph:
    """记录调用参数的假图，用于验证输入契约。

    Attributes:
        calls: 每次 ``ainvoke`` 的 ``(state, config)`` 记录。
    """

    def __init__(
        self,
        answer: str = "诊断答案",
        docs: list[dict] | None = None,
    ) -> None:
        """初始化替身。

        Args:
            answer: 模拟的系统答案。
            docs: 模拟的检索文档记录；为 None 时用单条占位记录。
        """
        self.answer: str = answer
        self.docs: list[dict] = (
            docs if docs is not None else [{"content": "参考上下文"}]
        )
        self.calls: list[tuple[dict, dict]] = []

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        """记录入参并返回模拟状态。

        Args:
            state: 输入状态字典。
            config: 运行配置。

        Returns:
            含 answer 与 retrieved_docs 的状态字典。
        """
        self.calls.append((state, config or {}))
        return {**state, "answer": self.answer, "retrieved_docs": self.docs}


class FlakyGraph:
    """首次调用失败（超时或抛异常）、其余正常的假图。"""

    def __init__(self, delay: float = 0.0, boom: bool = False) -> None:
        """初始化替身。

        Args:
            delay: 首次调用的额外延迟（秒），用于触发超时。
            boom: 为 True 时首次调用直接抛异常。
        """
        self.delay: float = delay
        self.boom: bool = boom
        self.count: int = 0

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        """首次按配置失败，之后返回正常状态。

        Args:
            state: 输入状态字典。
            config: 运行配置。

        Returns:
            正常状态字典。

        Raises:
            RuntimeError: 当 ``boom`` 为 True 且为首次调用时。
        """
        self.count += 1
        if self.count == 1:
            if self.boom:
                raise RuntimeError("graph boom")
            await asyncio.sleep(self.delay)
        return {"answer": "诊断答案", "retrieved_docs": [{"content": "参考上下文"}]}


def make_aevaluate(scores: dict[str, float]) -> Any:
    """构造返回固定分数的 RAGAS aevaluate 替身。

    Args:
        scores: 要返回的指标字典。

    Returns:
        与 :func:`ragas.aevaluate` 签名兼容的异步函数。
    """

    async def _fake_aevaluate(
        dataset: Any,
        metrics: Any = None,  # noqa: ARG001
        llm: Any = None,  # noqa: ARG001
        embeddings: Any = None,  # noqa: ARG001
    ) -> Any:
        return SimpleNamespace(scores=[dict(scores)])

    return _fake_aevaluate


def make_report() -> EvaluationReport:
    """构造用于图表测试的报告。

    Returns:
        指标固定的 :class:`EvaluationReport`。
    """
    overall = {name: 0.9 for name in EVALUATION_TARGETS}
    return EvaluationReport(
        overall_metrics=overall,
        single_hop_metrics=dict(overall),
        multi_hop_metrics={name: 0.8 for name in EVALUATION_TARGETS},
        per_sample_results=[],
        passed=False,
    )


# ═══════════ 验收 1：import 与阈值同源 ═══════════


def test_import_and_targets_are_shared_with_constants() -> None:
    """模块可导入，且 EVALUATION_TARGETS 与 src.constants 同源。"""
    assert evaluator.EVALUATION_TARGETS is EVALUATION_TARGETS
    assert set(evaluator.METRIC_NAMES) == set(EVALUATION_TARGETS)


# ═══════════ 验收 2：黄金测试集 ═══════════


def test_generate_golden_samples_returns_200() -> None:
    """返回 200 条样本。"""
    assert len(generate_golden_samples()) == 200


def test_golden_samples_split_is_100_and_100() -> None:
    """单跳与多跳各 100 条。"""
    samples = generate_golden_samples()

    single = [s for s in samples if s.question_type == "single_hop"]
    multi = [s for s in samples if s.question_type == "multi_hop"]

    assert len(single) == 100
    assert len(multi) == 100


def test_golden_samples_fields_are_filled() -> None:
    """每条样本的问题、标准答案与上下文均非空。"""
    for sample in generate_golden_samples():
        assert sample.question.strip()
        assert sample.ground_truth.strip()
        assert sample.contexts
        assert all(context.strip() for context in sample.contexts)
        assert sample.question_type in {"single_hop", "multi_hop"}


def test_golden_samples_are_multi_hop_rich() -> None:
    """多跳样本至少含两条上下文（体现跨文档推理）。"""
    multi = [
        s for s in generate_golden_samples() if s.question_type == "multi_hop"
    ]

    assert all(len(sample.contexts) >= 2 for sample in multi)


# ═══════════ 验收 3：图表字节流 ═══════════


def test_generate_charts_returns_two_charts() -> None:
    """返回两张图：总体对比与分组对比。"""
    charts = generate_charts(make_report())

    assert set(charts) == {"overall_bar", "group_comparison"}


@pytest.mark.parametrize("name", ["overall_bar", "group_comparison"])
def test_generate_charts_are_valid_png(name: str) -> None:
    """每张图都是有效 PNG 字节流（文件头为 PNG 魔数）。"""
    charts = generate_charts(make_report())

    payload = charts[name]
    assert isinstance(payload, bytes)
    assert payload[:4] == PNG_MAGIC


@pytest.mark.parametrize("name", ["overall_bar", "group_comparison"])
def test_generate_charts_have_non_trivial_size(name: str) -> None:
    """PNG 体积非平凡（说明确有渲染内容而非空图）。"""
    charts = generate_charts(make_report())

    assert len(charts[name]) > 5000


# ═══════════ 评估主流程 ═══════════


def test_evaluate_rag_passes_when_metrics_meet_targets() -> None:
    """四项指标均达标时 passed 为 True，且聚合值取整到四位。"""
    samples = [GoldenSample("问题", "标准答案", ["上下文"], "single_hop")]
    perfect = dict(EVALUATION_TARGETS)

    with mock.patch.object(evaluator, "aevaluate", new=make_aevaluate(perfect)):
        report = asyncio.run(evaluate_rag(RecordingGraph(), samples))

    assert report.passed is True
    assert report.overall_metrics == {
        name: round(value, 4) for name, value in perfect.items()
    }


def test_evaluate_rag_fails_below_targets() -> None:
    """任一指标低于阈值即 passed 为 False。"""
    samples = [GoldenSample("问题", "标准答案", ["上下文"], "single_hop")]
    low = {name: 0.1 for name in EVALUATION_TARGETS}

    with mock.patch.object(evaluator, "aevaluate", new=make_aevaluate(low)):
        report = asyncio.run(evaluate_rag(RecordingGraph(), samples))

    assert report.passed is False


def test_evaluate_rag_splits_single_and_multi_hop() -> None:
    """单跳与多跳分别聚合到各自的指标字典。"""
    samples = [
        GoldenSample("单跳", "gt", ["ctx"], "single_hop"),
        GoldenSample("多跳", "gt", ["ctx1", "ctx2"], "multi_hop"),
    ]
    perfect = dict(EVALUATION_TARGETS)

    with mock.patch.object(evaluator, "aevaluate", new=make_aevaluate(perfect)):
        report = asyncio.run(evaluate_rag(RecordingGraph(), samples))

    assert report.single_hop_metrics == report.multi_hop_metrics
    assert len(report.per_sample_results) == 2


def test_evaluate_rag_records_timeout_without_aborting() -> None:
    """单条超时记入 error 且不影响其余样本的聚合。"""
    samples = [
        GoldenSample("慢问题", "gt", ["ctx"], "single_hop"),
        GoldenSample("快问题", "gt", ["ctx"], "single_hop"),
    ]
    perfect = dict(EVALUATION_TARGETS)

    with mock.patch.object(evaluator, "aevaluate", new=make_aevaluate(perfect)):
        report = asyncio.run(
            evaluate_rag(FlakyGraph(delay=0.3), samples, timeout=0.05)
        )

    assert report.per_sample_results[0]["error"] == "Timeout"
    assert "error" not in report.per_sample_results[1]
    assert report.overall_metrics["faithfulness"] == pytest.approx(
        EVALUATION_TARGETS["faithfulness"]
    )


def test_evaluate_rag_records_exception_without_aborting() -> None:
    """单条抛异常记入 error，其余样本照常评估。"""
    samples = [
        GoldenSample("出错问题", "gt", ["ctx"], "single_hop"),
        GoldenSample("正常问题", "gt", ["ctx"], "single_hop"),
    ]

    with mock.patch.object(
        evaluator, "aevaluate", new=make_aevaluate(dict(EVALUATION_TARGETS))
    ):
        report = asyncio.run(evaluate_rag(FlakyGraph(boom=True), samples))

    assert "graph boom" in report.per_sample_results[0]["error"]
    assert "error" not in report.per_sample_results[1]


def test_evaluate_rag_all_samples_failed_yields_zero_metrics() -> None:
    """全部样本失败时指标全为 0 且 passed 为 False（不抛异常）。"""
    samples = [GoldenSample(f"q{i}", "gt", ["ctx"], "single_hop") for i in range(3)]

    class AlwaysBoom:
        """恒定抛异常的假图。"""

        async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
            """始终抛异常。"""
            raise RuntimeError("always boom")

    report = asyncio.run(evaluate_rag(AlwaysBoom(), samples))

    assert report.passed is False
    assert all(value == 0.0 for value in report.overall_metrics.values())


# ═══════════ 接口契约（照抄任务示例会失效的三处） ═══════════


def test_evaluate_rag_uses_query_key_and_thread_id() -> None:
    """必须以 query 为输入键且携带 thread_id。

    状态机输入键是 query（传 question 不报错但 answer 会是 None）；
    图启用 MemorySaver 检查点，缺 thread_id 时 ainvoke 直接抛 ValueError。
    """
    samples = [GoldenSample("运维问题", "gt", ["ctx"], "single_hop")]
    graph = RecordingGraph()

    with mock.patch.object(
        evaluator, "aevaluate", new=make_aevaluate(dict(EVALUATION_TARGETS))
    ):
        asyncio.run(evaluate_rag(graph, samples))

    state, config = graph.calls[0]
    assert set(state) == {"query"}
    assert state["query"] == "运维问题"
    assert config["configurable"]["thread_id"]


def test_evaluate_rag_passes_answer_and_doc_content_to_ragas() -> None:
    """送往 RAGAS 的 answer 与 contexts 取自状态机的 answer 与 retrieved_docs.content。"""
    captured: list[Any] = []

    async def _capturing(dataset: Any, metrics: Any = None, llm: Any = None, embeddings: Any = None) -> Any:  # noqa: ARG001
        captured.append(dataset)
        return SimpleNamespace(scores=[dict(EVALUATION_TARGETS)])

    samples = [GoldenSample("问题", "gt", ["兜底上下文"], "single_hop")]
    graph = RecordingGraph(
        answer="系统答案", docs=[{"content": "检索正文一"}, {"content": "检索正文二"}]
    )

    with mock.patch.object(evaluator, "aevaluate", new=_capturing):
        asyncio.run(evaluate_rag(graph, samples))

    row = captured[0][0]
    assert row["answer"] == "系统答案"
    assert row["contexts"] == ["检索正文一", "检索正文二"]


def test_evaluate_rag_falls_back_to_sample_contexts() -> None:
    """检索为空时上下文回落到样本自带 contexts，避免 RAGAS 缺列。"""
    captured: list[Any] = []

    async def _capturing(dataset: Any, metrics: Any = None, llm: Any = None, embeddings: Any = None) -> Any:  # noqa: ARG001
        captured.append(dataset)
        return SimpleNamespace(scores=[dict(EVALUATION_TARGETS)])

    samples = [GoldenSample("问题", "gt", ["样本上下文"], "single_hop")]

    with mock.patch.object(evaluator, "aevaluate", new=_capturing):
        asyncio.run(evaluate_rag(RecordingGraph(docs=[]), samples))

    assert captured[0][0]["contexts"] == ["样本上下文"]


# ═══════════ 零磁盘 ═══════════


class TestZeroDisk:
    """零磁盘静态扫描：源码禁出现任何落盘调用。"""

    def test_source_has_no_disk_api_calls(self) -> None:
        """AST 扫描禁用 open / pickle / write / to_csv 等落盘 API。"""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
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
