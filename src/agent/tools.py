"""Agent 可调用工具集。

聚合状态机节点依赖的外部能力：幻觉校验与答案引用格式化。
所有工具均以纯内存方式工作，不产生任何磁盘副作用。

Note:
    模型凭据仅从环境变量读取（经 :mod:`src.settings` 注入），
    源码与测试中不出现任何明文密钥。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI

from src.settings import get_settings

logger = logging.getLogger(__name__)

#: 幻觉校验提示词模板。
#:
#: Note:
#:     模板中 JSON 示例的花括号均已转义为双花括号，以便 :meth:`str.format`
#:     填充 ``documents`` / ``answer`` 两个占位符；渲染结果与模板原文一致。
#:     若沿用单花括号，``format`` 会把 JSON 字段名当作占位符并抛 ``KeyError``，
#:     使校验恒走降级分支。
HALLUCINATION_CHECK_PROMPT: str = """你是一名严谨的资深运维工程师（SRE），负责对运维诊断答案做事实核查。请根据提供的参考文档，判断给定的答案是否存在幻觉（即包含文档中未提及或相悖的信息）。
请以 JSON 格式返回：
{{
  "has_hallucination": true/false,
  "hallucinated_sentences": ["幻觉句子1", "幻觉句子2"],
  "corrected_answer": "修正后的完整答案（若无幻觉则为空字符串）"
}}
参考文档：{documents}
待核查答案：{answer}
"""

#: 答案中的引用标记模式，匹配 ``chunk_id:xxx``（兼容中文冒号与前后空白）。
SOURCE_REFERENCE_PATTERN: re.Pattern[str] = re.compile(r"chunk_id\s*[:：]\s*([A-Za-z0-9_.\-]+)")

#: 幻觉校验属于评估分析类任务，按项目约定取最高推理档位。
HALLUCINATION_REASONING_EFFORT: str = "max"


@dataclass
class HallucinationResult:
    """幻觉校验结果。

    Attributes:
        has_hallucination: 是否检出幻觉。
        hallucinated_sentences: 检出幻觉的句子列表。
        corrected_answer: 修正后的完整答案；无幻觉时为空字符串。
    """

    has_hallucination: bool
    hallucinated_sentences: list[str]
    corrected_answer: str


def message_text(message: object) -> str:
    """把 LLM 响应内容规整为纯文本。

    Args:
        message: ``ChatOpenAI`` 的响应对象，或其 ``content`` 字段。

    Returns:
        拼接后的纯文本；内容块列表会按 ``text`` 字段拼接。
    """
    content = getattr(message, "content", message)
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


def _to_result(payload: dict[str, Any]) -> HallucinationResult:
    """把 LLM 返回的 JSON 载荷规整为校验结果。

    Args:
        payload: 已解析的 JSON 字典。

    Returns:
        字段缺失或类型异常时按安全默认值填充的校验结果。
    """
    sentences = payload.get("hallucinated_sentences") or []
    if isinstance(sentences, str):
        sentences = [sentences]
    return HallucinationResult(
        has_hallucination=bool(payload.get("has_hallucination", False)),
        hallucinated_sentences=[str(sentence) for sentence in sentences],
        corrected_answer=str(payload.get("corrected_answer") or ""),
    )


def check_hallucination(answer: str, docs: list[dict]) -> HallucinationResult:
    """调用 LLM 进行幻觉检测。

    以 GLM（``GLM_MODEL_NAME`` / ``GLM_BASE_URL`` / ``GLM_API_KEY``）开启 JSON
    输出与思考模式执行核查，推理档位取 ``reasoning_effort="max"``。

    Args:
        answer: 待核查的生成答案。
        docs: 答案依据的参考文档记录列表，序列化后填入提示词。

    Returns:
        校验结果；调用或解析失败时降级为
        ``has_hallucination=False``、空句子列表与空修正答案，
        保证检索链路不因校验失败而中断。

    Note:
        文档序列化使用 ``default=str`` 兜底，非 JSON 原生类型不会中断核查。
    """
    try:
        settings = get_settings()
        # 与状态机选择保持一致：配置了 DeepSeek 凭据就用主模型。
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
                reasoning_effort=HALLUCINATION_REASONING_EFFORT,
                # 不传 thinking：GLM 思考模式默认开启，且该非标准字段会被透传给
                # 结构化输出的 Completions.parse()，触发 unexpected keyword argument
                # 使幻觉校验永久降级（与 state_machine._build_llm 同一坑）。
                model_kwargs={"response_format": {"type": "json_object"}},
            )
        prompt = HALLUCINATION_CHECK_PROMPT.format(
            documents=json.dumps(docs, ensure_ascii=False, default=str),
            answer=answer,
        )
        response = llm.invoke(prompt)
        payload = json.loads(message_text(response))
        return _to_result(payload)
    except Exception as exc:  # noqa: BLE001 - 校验失败须降级而非中断
        logger.error("幻觉校验失败，降级为未检出幻觉: %s", exc)
        return HallucinationResult(
            has_hallucination=False,
            hallucinated_sentences=[],
            corrected_answer="",
        )


def format_sources(answer: str, sources: list[dict]) -> str:
    """把答案中的 ``chunk_id:xxx`` 标记替换为可读的引用来源。

    将 ``chunk_id:xxx`` 渲染为 ``[来源：{filename} 第{chunk_index}段]``；
    未收录在 ``sources`` 中的标记保持原样，避免破坏原有文本结构。

    Args:
        answer: 含 ``chunk_id:xxx`` 引用标记的答案文本。
        sources: 来源记录列表，每条含 ``chunk_id``、``filename`` 与 ``chunk_index``。

    Returns:
        替换后的答案文本；``answer`` 为空时原样返回。
    """
    if not answer:
        return answer

    index: dict[str, dict] = {}
    for source in sources or []:
        chunk_id = str(source.get("chunk_id") or "")
        if chunk_id:
            index[chunk_id] = source

    def _render(match: re.Match[str]) -> str:
        source = index.get(match.group(1))
        if source is None:
            return match.group(0)
        filename = source.get("filename") or "未知文档"
        chunk_index = source.get("chunk_index")
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            try:
                chunk_index = int(str(chunk_index))
            except (TypeError, ValueError):
                chunk_index = None
        position = f"第{chunk_index}段" if chunk_index is not None else "第?段"
        return f"[来源：{filename} {position}]"

    return SOURCE_REFERENCE_PATTERN.sub(_render, answer)
