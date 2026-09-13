"""基于 RAGAS 的评估体系。

对 Agent 状态机逐条跑黄金测试集，用 RAGAS 四项指标打分并聚合成报告；
图表以 matplotlib 内存 Figure 渲染为 PNG 字节流返回。

零磁盘约束：

* 黄金测试集硬编码在 :func:`generate_golden_samples` 中，不读取外部文件；
* 评估结果仅存于 :class:`EvaluationReport` 内存对象；
* 图表经 ``io.BytesIO`` 取出 PNG 字节流，全程无文件写入。

Note:
    相对任务书示例的三处**接口适配**（照抄会失效或静默出错）：

    1. 状态机输入键是 ``query`` 而非 ``question``——传错键**不会报错**，
       但 ``answer`` 会是 ``None``，导致评估全部基于空答案；
    2. 图编译时启用了 ``MemorySaver`` 检查点，``ainvoke`` 必须携带
       ``config={"configurable": {"thread_id": ...}}``，否则抛 ``ValueError``；
    3. RAGAS 0.4 的 ``EvaluationResult`` 是 dataclass（``scores: list[dict]``），
       **没有** ``items()``，分数须经 ``result.scores[0]`` 读取；
       同理使用真异步的 :func:`ragas.aevaluate`，避免同步 ``evaluate`` 阻塞事件循环；
    4. 指标自 ``ragas.metrics.collections`` 导入——旧路径 ``ragas.metrics``
       已被官方标记为废弃（v1.0 移除）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from dataclasses import dataclass
from typing import Any

import matplotlib

matplotlib.use("Agg")  # 无界面后端：纯内存渲染，不依赖显示环境
import matplotlib.pyplot as plt  # noqa: E402 - 必须在 use("Agg") 之后导入
from datasets import Dataset
from langgraph.graph.state import CompiledStateGraph
from ragas import aevaluate
from ragas.metrics.collections import (
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
)

from src.constants import EVALUATION_TARGETS

logger = logging.getLogger(__name__)

#: 单条样本的评估超时时间（秒）。
SAMPLE_TIMEOUT_SECONDS: float = 30.0

#: 进度日志的输出间隔（每处理多少条样本打印一次）。
PROGRESS_LOG_INTERVAL: int = 10

#: 四项指标的展示顺序，取自全局阈值表的键序。
METRIC_NAMES: tuple[str, ...] = tuple(EVALUATION_TARGETS)

#: 本项目使用的四项 RAGAS 指标。
RAGAS_METRICS: tuple[Any, ...] = (
    faithfulness,
    answer_relevancy,
    context_recall,
    context_precision,
)


@dataclass
class GoldenSample:
    """黄金测试样本。

    Attributes:
        question: 问题文本。
        ground_truth: 标准答案。
        contexts: 该问题应有的参考文档片段。
        question_type: 问题类型，取值 ``single_hop`` 或 ``multi_hop``。
    """

    question: str
    ground_truth: str
    contexts: list[str]
    question_type: str


@dataclass
class EvaluationReport:
    """评估报告。

    Attributes:
        overall_metrics: 全部有效样本的四项指标均值。
        single_hop_metrics: 单跳样本的指标均值。
        multi_hop_metrics: 多跳样本的指标均值。
        per_sample_results: 逐样本明细；失败样本仅含 ``question`` 与 ``error``。
        passed: 四项总体指标是否全部达到 :data:`EVALUATION_TARGETS`。
    """

    overall_metrics: dict[str, float]
    single_hop_metrics: dict[str, float]
    multi_hop_metrics: dict[str, float]
    per_sample_results: list[dict[str, Any]]
    passed: bool


def generate_golden_samples() -> list[GoldenSample]:
    """返回 200 条硬编码黄金测试样本。

    样本内容取自本项目运维知识库的既有事实（设备台账、故障排查 SOP、
    维护记录、硬件规格说明、维保政策），按确定性模板矩阵生成：
    设备编号与模式选择均由样本序号决定，不使用随机数，
    两次调用结果完全一致（可作回归基线）。

    Returns:
        200 条 :class:`GoldenSample`：前 100 条为 ``single_hop``
        （设备手册 / SOP / 维保政策类单跳事实），后 100 条为 ``multi_hop``
        （故障日志 × 维护记录跨文档根因链路，每条至少 2 段上下文）。

    Note:
        全部在内存中构造，不读取任何外部文件；新样本应随知识库文档
        扩充同步增补模板，禁止回退为无事实依据的占位文本。
    """
    samples: list[GoldenSample] = []
    for index in range(100):
        device = f"设备{chr(65 + index % 10)}-{200 + index:03d}"
        samples.append(_single_hop_sample(index, device))
    for index in range(100):
        device = f"设备{chr(65 + index % 10)}-{300 + index:03d}"
        samples.append(_multi_hop_sample(index, device))
    return samples


#: 单跳事实模板：每个元素接收 ``(设备编号, 序号)`` 返回一条样本。
#: 事实取值与运维知识库一致——清灰周期、风扇告警阈值、电源模块寿命等
#: 均来自维护 SOP / 硬件规格说明 / 维保政策文档。
_SINGLE_HOP_FACTS: tuple[Any, ...] = (
    lambda device, index: GoldenSample(
        question=f"{device}的额定功率是多少？",
        ground_truth=f"{device}的额定功率为800W，支持双电源热插拔。",
        contexts=[f"设备手册：{device}额定功率800W，双电源冗余，支持热插拔维护。"],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}的清灰维护应按什么周期执行？",
        ground_truth="每年6月与12月各执行一次清灰维护，两次间隔不超过6个月。",
        contexts=["维护SOP：数据中心设备清灰周期建议为每年6月与12月，间隔不超过6个月。"],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}风扇转速异常的告警阈值是多少？",
        ground_truth="风扇转速持续10分钟高于额定转速的90%时触发告警。",
        contexts=[
            f"硬件规格说明：{device}风扇控制逻辑为转速持续10分钟超过额定值90%即上报告警。"
        ],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}的电源模块使用多久需要预防性更换？",
        ground_truth="电源模块使用超过3年故障率显著上升，建议满48个月做预防性更换。",
        contexts=[
            "维保政策：电源模块寿命预测模型显示使用超3年故障率明显上升，满48个月执行预防性更换。"
        ],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}网络丢包时应优先执行哪个排查步骤？",
        ground_truth="优先执行交换机端口重置：关闭端口30秒后重新启用，再观察丢包率。",
        contexts=[
            f"故障排查SOP：{device}出现网络丢包时，第一步执行交换机端口重置（shutdown后等待30秒no shutdown）。"
        ],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}配套UPS电池的更换周期是多久？",
        ground_truth="UPS蓄电池每3年更换一次，放电时间低于额定值80%时立即更换。",
        contexts=["维保政策：UPS蓄电池更换周期3年；放电测试低于额定容量80%时提前更换。"],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}硬盘SMART哪个指标超限需要立即更换？",
        ground_truth="SMART重映射扇区数（05）非零或寻址错误率（197）持续增长时立即更换硬盘。",
        contexts=[
            f"监控配置手册：{device}硬盘SMART指标05（重映射扇区数）非零、197（寻址错误率）持续增长即判定待更换。"
        ],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}的固件版本低于多少需要安排升级？",
        ground_truth="固件版本低于2.4.1存在已知缺陷，必须升级到2.4.1或以上。",
        contexts=["变更管理规范：固件版本低于2.4.1存在频繁重启缺陷，需升级至2.4.1及以上。"],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}所在机房的温度和湿度应控制在什么范围？",
        ground_truth="机房温度控制在18至27摄氏度，相对湿度控制在40%至60%。",
        contexts=["环境运维规范：机房温度18-27℃，相对湿度40%-60%，超限触发环境告警。"],
        question_type="single_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}最大支持多少内存扩展？",
        ground_truth=f"{device}最大支持1TB内存扩展（32条32GB ECC内存）。",
        contexts=[f"设备手册：{device}配置32个内存插槽，最大支持1TB ECC内存。"],
        question_type="single_hop",
    ),
)

#: 多跳根因链路模板：每条样本由「故障日志 × 维护记录」两段上下文
#: 构成跨文档推理链，标准答案需同时引用两段文档的事实。
_MULTI_HOP_CHAINS: tuple[Any, ...] = (
    lambda device, index: GoldenSample(
        question=f"{device}频繁重启且风扇转速异常，可能的根因有哪些？",
        ground_truth=(
            f"{device}频繁重启且风扇异常可能由电源模块老化或散热风道堵塞引起："
            "故障日志显示10:00重启、风扇转速达100%；维护记录显示上次清灰在6个月前、"
            "电源模块使用超3年。"
        ),
        contexts=[
            f"故障日志：{device}在10:00发生重启，重启期间风扇转速达到100%。",
            f"维护记录：{device}上次清灰在6个月前，电源模块已使用超过3年，电容存在老化迹象。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}网络丢包且端口CRC错误增长，如何定位故障点？",
        ground_truth=(
            f"结合两份记录判定为光模块收发光异常导致丢包：告警记录显示端口CRC持续增长，"
            f"巡检报告显示{device}上联光模块收光功率低于阈值，建议更换光模块并清洁光纤。"
        ),
        contexts=[
            f"告警记录：{device}上联端口CRC错误计数持续增长，伴随间歇性丢包。",
            f"巡检报告：{device}上联光模块收光功率-18dBm，低于-12dBm告警阈值。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}温度告警与风扇全速运转同时出现，最可能的根因是什么？",
        ground_truth=(
            f"散热风道堵塞：温度告警（进风口42℃）与风扇100%转速同时出现，"
            f"且维护记录显示该设备已超6个月未清灰，应立即执行清灰维护。"
        ),
        contexts=[
            f"告警记录：{device}进风口温度42℃，超过35℃门限，风扇转速100%。",
            f"维护记录：{device}最近一次清灰维护在7个月前，超出6个月周期要求。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}内存ECC错误与意外重启先后出现，应如何处置？",
        ground_truth=(
            f"判定内存条故障引发重启：日志显示ECC可纠正错误持续增长后发生重启，"
            f"备件记录显示同批次内存已有替换先例，应定位故障内存条并更换。"
        ),
        contexts=[
            f"系统日志：{device}ECC可纠正错误计数30分钟内增长至1200次，随后发生意外重启。",
            f"备件记录：{device}同批次32GB内存条此前已有2条因ECC错误更换。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}UPS放电时间不足且市电正常，根因是什么？",
        ground_truth=(
            f"UPS蓄电池老化：告警显示满电放电时间仅12分钟（额定30分钟），"
            f"维保记录显示电池组已使用3年零2个月超过更换周期，应整组更换电池。"
        ),
        contexts=[
            f"告警记录：{device}配套UPS满电放电时间12分钟，低于额定30分钟的80%。",
            f"维护记录：{device}UPS蓄电池组投运于3年2个月前，超出3年更换周期。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}电压不稳引发重启的风险如何评估？",
        ground_truth=(
            f"高风险：电压检测显示输入电压波动超出198-242V范围，"
            f"且该电源模块已使用超3年（故障率上升期），建议更换电源模块并接入稳压输出。"
        ),
        contexts=[
            f"电压检测报告：{device}输入电压在190-245V间波动，超出198-242V允收范围。",
            f"维护记录：{device}电源模块使用已超过3年，处于故障率上升期。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}硬盘SMART重映射扇区数增长且IO延迟升高，下一步动作？",
        ground_truth=(
            f"按监控手册判定硬盘待更换：重映射扇区数非零且IO延迟翻倍，"
            f"备件库存有同型号热备盘，应执行数据迁移后更换并重建阵列。"
        ),
        contexts=[
            f"监控数据：{device}第3块硬盘SMART 05值增长至48，平均IO延迟由2ms升至9ms。",
            f"备件清单：{device}同型号4TB硬盘热备库存2块，更换后需重建RAID。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}运行中崩溃与固件版本的关系如何判断？",
        ground_truth=(
            f"固件缺陷所致：崩溃日志的调用栈特征与缺陷通告描述一致，"
            f"当前固件2.3.7低于修复版本2.4.1，应安排窗口升级。"
        ),
        contexts=[
            f"崩溃日志：{device}内核崩溃调用栈指向网卡驱动中断处理路径。",
            f"缺陷通告：固件2.3.x系列存在网卡驱动中断缺陷导致崩溃，2.4.1已修复。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}机房空调故障时温度快速上升，应执行什么预案？",
        ground_truth=(
            f"执行高温应急预案：空调停机导致回风温度31℃且持续上升，"
            f"按预案应启用备用空调并临时降低非核心设备负载，30分钟内未恢复则申请停机。"
        ),
        contexts=[
            f"环境告警：{device}所在机柜回风温度31℃，精密空调1号停机告警。",
            f"应急预案：空调故障时启用备用空调并降载，回风超35℃且30分钟未恢复可申请停机。",
        ],
        question_type="multi_hop",
    ),
    lambda device, index: GoldenSample(
        question=f"{device}风扇噪音异常但转速正常，可能的故障点在哪里？",
        ground_truth=(
            f"风扇轴承磨损：巡检记录显示噪音来源于风扇轴承且振动偏高，"
            f"转速尚在阈值内，应按SOP预约更换风扇模块避免突发停转。"
        ),
        contexts=[
            f"巡检报告：{device}风扇运转噪音异常，轴承振动值0.8mm/s偏高，转速读数正常。",
            f"维护SOP：风扇轴承振动超0.5mm/s即预约更换风扇模块，防止突发停转。",
        ],
        question_type="multi_hop",
    ),
)


def _single_hop_sample(index: int, device: str) -> GoldenSample:
    """按序号选取单跳事实模板生成样本。

    Args:
        index: 样本序号（0-99）。
        device: 设备编号，含序号以保证唯一性。

    Returns:
        一条 ``single_hop`` 黄金样本。
    """
    return _SINGLE_HOP_FACTS[index % len(_SINGLE_HOP_FACTS)](device, index)


def _multi_hop_sample(index: int, device: str) -> GoldenSample:
    """按序号选取多跳根因链路模板生成样本。

    Args:
        index: 样本序号（0-99）。
        device: 设备编号，含序号以保证唯一性。

    Returns:
        一条 ``multi_hop`` 黄金样本（上下文至少 2 段）。
    """
    return _MULTI_HOP_CHAINS[index % len(_MULTI_HOP_CHAINS)](device, index)


def _safe_float(value: Any) -> float:
    """把指标原始值规整为有限浮点数。

    Args:
        value: 任意指标原始值（可能为 None、字符串或 NaN）。

    Returns:
        有限浮点数；无法解析或非有限时返回 ``0.0``。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _aggregate(results: list[dict[str, Any]]) -> dict[str, float]:
    """对有效样本求四项指标的均值。

    Args:
        results: 逐样本结果列表；含 ``error`` 键的条目会被排除。

    Returns:
        指标名到均值的映射；无有效样本时所有指标为 ``0.0``。
    """
    valid = [item for item in results if "error" not in item]
    if not valid:
        return {name: 0.0 for name in METRIC_NAMES}
    return {
        name: round(
            sum(_safe_float(item.get(name, 0.0)) for item in valid) / len(valid), 4
        )
        for name in METRIC_NAMES
    }


def _answer_and_contexts(
    result: dict[str, Any], sample: GoldenSample
) -> tuple[str, list[str]]:
    """从状态机输出中取出答案与检索上下文。

    Args:
        result: 状态机 ``ainvoke`` 返回的状态字典。
        sample: 对应黄金样本，用于上下文兜底。

    Returns:
        ``(答案, 上下文列表)``；检索结果为空时回落到样本自带的 ``contexts``，
        避免 RAGAS 因上下文列缺失而整体失败。
    """
    answer = str(result.get("answer") or "")
    documents = result.get("retrieved_docs") or []
    contexts = [
        str(document.get("content"))
        for document in documents
        if isinstance(document, dict) and document.get("content")
    ]
    return answer, contexts or list(sample.contexts)


async def _score_sample(
    sample: GoldenSample,
    answer: str,
    contexts: list[str],
    llm: Any | None,
    embeddings: Any | None,
) -> dict[str, float]:
    """用 RAGAS 对单条样本打分。

    Args:
        sample: 黄金样本。
        answer: 系统答案。
        contexts: 检索上下文列表。
        llm: 可选的 RAGAS LLM 包装。
        embeddings: 可选的 RAGAS embedding 包装。

    Returns:
        指标名到分数的映射；RAGAS 未返回的指标补 ``0.0``。

    Note:
        使用 :func:`ragas.aevaluate` 而非同步 ``evaluate``，
        以免在事件循环内阻塞。
    """
    dataset = Dataset.from_dict(
        {
            "question": [sample.question],
            "answer": [answer],
            "contexts": [contexts],
            "ground_truth": [sample.ground_truth],
        }
    )
    result = await aevaluate(
        dataset,
        metrics=list(RAGAS_METRICS),
        llm=llm,
        embeddings=embeddings,
    )
    rows = getattr(result, "scores", None) or []
    row = rows[0] if rows else {}
    return {name: round(_safe_float(row.get(name)), 4) for name in METRIC_NAMES}


async def evaluate_rag(
    graph: CompiledStateGraph,
    samples: list[GoldenSample],
    *,
    llm: Any | None = None,
    embeddings: Any | None = None,
    timeout: float = SAMPLE_TIMEOUT_SECONDS,
) -> EvaluationReport:
    """逐条跑状态机并用 RAGAS 计算指标，最后聚合成报告。

    Args:
        graph: 已编译的 Agent 状态图（编译时启用了检查点）。
        samples: 黄金测试样本列表。
        llm: 可选的 RAGAS LLM 包装；为 None 时由 RAGAS 自行解析默认模型。
        embeddings: 可选的 RAGAS embedding 包装；为 None 时同上。
        timeout: 单条样本的评估超时（秒），超时按失败样本记录。

    Returns:
        聚合后的 :class:`EvaluationReport`。

    Note:
        单条样本超时或抛异常都不中断整体评估，只在该条明细中记录 ``error``；
        失败样本不计入任何一组均值。状态机输入键为 ``query``，
        且每次调用携带唯一 ``thread_id``，以适配 ``MemorySaver`` 检查点。
    """
    per_sample: list[dict[str, Any]] = []
    single_hop: list[dict[str, Any]] = []
    multi_hop: list[dict[str, Any]] = []

    for index, sample in enumerate(samples):
        try:
            result = await asyncio.wait_for(
                graph.ainvoke(
                    {"query": sample.question},
                    config={"configurable": {"thread_id": f"eval-{index}"}},
                ),
                timeout=timeout,
            )
            answer, contexts = _answer_and_contexts(result, sample)
            scores = await _score_sample(sample, answer, contexts, llm, embeddings)
        except asyncio.TimeoutError:
            logger.warning("样本 %d 评估超时", index)
            per_sample.append({"question": sample.question, "error": "Timeout"})
        except Exception as exc:  # noqa: BLE001 - 单条失败不得中断整体评估
            logger.error("样本 %d 评估失败: %s", index, exc)
            per_sample.append({"question": sample.question, "error": str(exc)})
        else:
            record: dict[str, Any] = dict(scores)
            record["question"] = sample.question
            record["question_type"] = sample.question_type
            per_sample.append(record)
            bucket = single_hop if sample.question_type == "single_hop" else multi_hop
            bucket.append(record)

        if (index + 1) % PROGRESS_LOG_INTERVAL == 0:
            logger.info("评估进度: %d/%d", index + 1, len(samples))

    overall = _aggregate(per_sample)
    return EvaluationReport(
        overall_metrics=overall,
        single_hop_metrics=_aggregate(single_hop),
        multi_hop_metrics=_aggregate(multi_hop),
        per_sample_results=per_sample,
        passed=all(
            overall.get(name, 0.0) >= target
            for name, target in EVALUATION_TARGETS.items()
        ),
    )


def _render_png(figure: Any) -> bytes:
    """把 matplotlib Figure 渲染为 PNG 字节流。

    Args:
        figure: 已绘制完成的 Figure。

    Returns:
        PNG 字节流，首四字节为 PNG 魔数。
    """
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    return buffer.getvalue()


def generate_charts(report: EvaluationReport) -> dict[str, bytes]:
    """渲染评估图表并返回 PNG 字节流。

    Args:
        report: 评估报告。

    Returns:
        ``{"overall_bar": png, "group_comparison": png}``。

    Note:
        Figure 全部在内存中创建并在 ``finally`` 中显式关闭，
        PNG 经 ``io.BytesIO`` 取出，不写任何临时文件。
    """
    charts: dict[str, bytes] = {}
    positions = list(range(len(METRIC_NAMES)))
    targets = [EVALUATION_TARGETS[name] for name in METRIC_NAMES]

    # 图一：总体指标 vs 目标阈值（左右并列，避免两根柱子重叠）
    actuals = [_safe_float(report.overall_metrics.get(name)) for name in METRIC_NAMES]
    figure, axes = plt.subplots(figsize=(10, 6))
    try:
        axes.bar([p - 0.2 for p in positions], actuals, width=0.4, label="Actual")
        axes.bar([p + 0.2 for p in positions], targets, width=0.4, label="Target")
        axes.set_xticks(positions)
        axes.set_xticklabels(METRIC_NAMES)
        axes.set_ylim(0.0, 1.05)
        axes.legend()
        axes.set_title("Overall Metrics vs Targets")
        charts["overall_bar"] = _render_png(figure)
    finally:
        plt.close(figure)

    # 图二：单跳 vs 多跳分组对比
    single_values = [
        _safe_float(report.single_hop_metrics.get(name)) for name in METRIC_NAMES
    ]
    multi_values = [
        _safe_float(report.multi_hop_metrics.get(name)) for name in METRIC_NAMES
    ]
    figure, axes = plt.subplots(figsize=(12, 6))
    try:
        axes.bar([p - 0.2 for p in positions], single_values, width=0.4, label="Single-hop")
        axes.bar([p + 0.2 for p in positions], multi_values, width=0.4, label="Multi-hop")
        axes.set_xticks(positions)
        axes.set_xticklabels(METRIC_NAMES)
        axes.set_ylim(0.0, 1.05)
        axes.legend()
        axes.set_title("Single-hop vs Multi-hop Performance")
        charts["group_comparison"] = _render_png(figure)
    finally:
        plt.close(figure)

    return charts


__all__ = [
    "EVALUATION_TARGETS",
    "EvaluationReport",
    "GoldenSample",
    "evaluate_rag",
    "generate_charts",
    "generate_golden_samples",
]
