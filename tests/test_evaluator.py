"""评估器补充验收测试（pytest）。

按 Goal 指定的四个用例名编写，与主平台的 ``test_evaluation.py`` 互补：

1. **test_golden_samples_count** —— 不止数量：确定性（两次调用逐字段相等）、
   问题唯一性、占位文本清零（``XX`` 已替换为知识库事实）、上下文来源可溯；
2. **test_evaluation_report_structure** —— 用替身图与替身 RAGAS 跑**全量 200 条**，
   校验报告对象结构：总体/分组指标键、逐样本明细字段、分组数量、passed 判定；
3. **test_generate_charts_png** —— 完整 PNG 校验：8 字节文件签名、IHDR 块、
   IEND 结尾、非平凡体积、Figure 不泄漏；
4. **test_timeout_handling** —— 慢样本被 ``wait_for`` 取消记为 Timeout、
   快样本照常聚合、总耗时受超时参数约束而非慢样本时长。

全部使用替身（不触发真实 LLM / embedding / 权重下载）。

运行方式::

    python -m pytest tests/test_evaluator.py -p no:cacheprovider -q
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from types import SimpleNamespace
from typing import Any
from unittest import mock

import matplotlib.pyplot as plt
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

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: 上下文片段允许的文档来源前缀（对应知识库真实文档类型）。
CONTEXT_SOURCES = ("故障日志", "维护记录", "维护SOP", "设备手册", "硬件规格说明",
                   "维保政策", "告警记录", "巡检报告", "系统日志", "备件",
                   "电压检测报告", "监控", "变更管理规范", "环境运维规范", "缺陷通告", "应急预案",
                   "故障排查SOP", "环境告警", "崩溃日志")


def make_aevaluate(scores: dict[str, float]) -> Any:
    """构造返回固定分数的 RAGAS ``aevaluate`` 替身。"""

    async def _fake_aevaluate(
        dataset: Any,
        metrics: Any = None,  # noqa: ARG001
        llm: Any = None,  # noqa: ARG001
        embeddings: Any = None,  # noqa: ARG001
    ) -> Any:
        return SimpleNamespace(scores=[dict(scores)])

    return _fake_aevaluate


class StubGraph:
    """替身状态图：返回固定答案与上下文。"""

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:  # noqa: ARG002
        return {
            **state,
            "answer": "诊断答案",
            "retrieved_docs": [{"content": "检索上下文"}],
        }


class SlowFirstGraph:
    """对含「慢」标记的查询延迟应答的替身图（用于超时测试）。"""

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay
        self.cancelled = 0

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        try:
            if "慢" in str(state.get("query")) and self.delay > 0:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return {
            **state,
            "answer": "慢样本答案",
            "retrieved_docs": [{"content": "检索上下文"}],
        }


# ═══════════ 验收 1：test_golden_samples_count ═══════════


def test_golden_samples_count() -> None:
    """200 条（100 单跳 + 100 多跳），确定性生成、问题唯一、无占位文本。"""
    samples = generate_golden_samples()
    again = generate_golden_samples()

    assert len(samples) == 200
    assert len([s for s in samples if s.question_type == "single_hop"]) == 100
    assert len([s for s in samples if s.question_type == "multi_hop"]) == 100
    # 确定性：两次调用逐字段相等（黄金集可作回归基线）
    assert samples == again
    # 唯一性：问题不重复
    questions = [s.question for s in samples]
    assert len(questions) == len(set(questions))
    # 任务1回归：占位文本已替换为知识库事实内容
    for sample in samples:
        assert "XX" not in sample.question
        assert "XX" not in sample.ground_truth
        assert all("XX" not in context for context in sample.contexts)


def test_golden_samples_content_is_grounded() -> None:
    """样本内容与知识库一致：上下文带真实文档来源、事实数值正确。"""
    samples = generate_golden_samples()

    for sample in samples:
        for context in sample.contexts:
            assert context.startswith(CONTEXT_SOURCES), context
    # 抽查既有知识库事实（清灰周期 / 风扇阈值 / 电源寿命 / 端口重置）
    joined = "\n".join(s.ground_truth for s in samples)
    assert "6月与12月" in joined
    assert "90%" in joined
    assert "48个月" in joined
    assert "30秒" in joined
    # 多跳样本上下文 ≥ 2 段（跨文档推理）
    multi = [s for s in samples if s.question_type == "multi_hop"]
    assert all(len(s.contexts) >= 2 for s in multi)
    # 单跳在前、多跳在后
    assert all(s.question_type == "single_hop" for s in samples[:100])
    assert all(s.question_type == "multi_hop" for s in samples[100:])


# ═══════════ 验收 2：test_evaluation_report_structure ═══════════


def test_evaluation_report_structure() -> None:
    """全量 200 条跑出的报告结构完整：指标键、明细字段、分组数量、判定。"""
    samples = generate_golden_samples()

    with mock.patch.object(
        evaluator, "aevaluate", new=make_aevaluate(dict(EVALUATION_TARGETS))
    ):
        report = asyncio.run(evaluate_rag(StubGraph(), samples))

    assert isinstance(report, EvaluationReport)
    # 三组指标均为四项、键与阈值表同源
    for metrics in (report.overall_metrics, report.single_hop_metrics, report.multi_hop_metrics):
        assert set(metrics) == set(EVALUATION_TARGETS)
    # 逐样本明细：200 条，无失败，字段齐备
    assert len(report.per_sample_results) == 200
    assert sum(1 for item in report.per_sample_results if "error" in item) == 0
    for item in report.per_sample_results:
        assert set(EVALUATION_TARGETS) <= set(item)
        assert item["question"]
        assert item["question_type"] in {"single_hop", "multi_hop"}
    # 分组数量与顺序（前 100 单跳、后 100 多跳）
    types = [item["question_type"] for item in report.per_sample_results]
    assert types.count("single_hop") == 100
    assert types.count("multi_hop") == 100
    # 全部达标 → passed 为 True
    assert report.passed is True
    # 分组均值等于替身分数
    assert report.single_hop_metrics == {
        name: round(value, 4) for name, value in EVALUATION_TARGETS.items()
    }


# ═══════════ 验收 3：test_generate_charts_png ═══════════


def _make_report() -> EvaluationReport:
    """构造图表测试用的固定报告。"""
    overall = {name: 0.9 for name in EVALUATION_TARGETS}
    return EvaluationReport(
        overall_metrics=overall,
        single_hop_metrics=dict(overall),
        multi_hop_metrics={name: 0.8 for name in EVALUATION_TARGETS},
        per_sample_results=[],
        passed=True,
    )


def test_generate_charts_png() -> None:
    """两张图均为完整有效 PNG：签名、IHDR、IEND、非平凡体积、Figure 不泄漏。"""
    charts = generate_charts(_make_report())

    assert set(charts) == {"overall_bar", "group_comparison"}
    for name, payload in charts.items():
        assert isinstance(payload, bytes)
        assert payload[:8] == PNG_SIGNATURE
        assert b"IHDR" in payload[:32]
        # PNG 以 IEND 块收尾：[-8:-4] 为类型字段，末 4 字节为其 CRC
        assert payload[-8:-4] == b"IEND"
        assert len(payload) > 5000
    # 渲染完成后不留存活的 Figure（无内存泄漏）
    assert plt.get_fignums() == []


# ═══════════ 验收 4：test_timeout_handling ═══════════


def test_timeout_handling() -> None:
    """慢样本被按超时取消并记为 Timeout，快样本照常聚合，总耗时不被慢样本拖住。"""
    samples = [
        GoldenSample("快问题1", "gt", ["ctx"], "single_hop"),
        GoldenSample("慢问题", "gt", ["ctx"], "single_hop"),
        GoldenSample("快问题2", "gt", ["ctx"], "single_hop"),
    ]
    graph = SlowFirstGraph(delay=5.0)

    start = time.monotonic()
    with mock.patch.object(
        evaluator, "aevaluate", new=make_aevaluate(dict(EVALUATION_TARGETS))
    ):
        report = asyncio.run(evaluate_rag(graph, samples, timeout=0.1))
    elapsed = time.monotonic() - start

    # 超时样本：记录 Timeout 错误，不产生分数
    assert report.per_sample_results[1]["error"] == "Timeout"
    assert set(EVALUATION_TARGETS).isdisjoint(report.per_sample_results[1])
    # 快样本照常计分
    for position in (0, 2):
        assert "error" not in report.per_sample_results[position]
        assert report.per_sample_results[position]["faithfulness"] == pytest.approx(
            EVALUATION_TARGETS["faithfulness"]
        )
    # 聚合只取有效样本（2 条），均值等于替身分数
    assert report.overall_metrics["faithfulness"] == pytest.approx(
        EVALUATION_TARGETS["faithfulness"]
    )
    # 总耗时受 timeout 参数约束（远小于慢样本的 5 秒 delay）
    assert elapsed < 2.0
    assert graph.cancelled == 1


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
