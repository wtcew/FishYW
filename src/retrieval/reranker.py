"""Cross-Encoder 重排序。

对 RRF 融合后的候选文档执行精排，保留最相关的若干条注入生成上下文。

本模块提供可复用的重排序**基础设施**：只管模型权重的加载与「查询-文档」对
打分，不关心候选来自哪一路召回；三路召回与融合的编排见 :mod:`core.retrieval`。

Note:
    模型权重经 :class:`sentence_transformers.CrossEncoder` 直接加载到内存，
    不产生任何持久化文件（权重缓存位于项目之外的 HF 缓存目录）。
"""

from __future__ import annotations

import logging

from sentence_transformers import CrossEncoder

from src.settings import get_settings

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """基于 Cross-Encoder 的重排序器。

    加载 ``BAAI/bge-reranker-v2-m3`` 对「查询-文档」对逐条打分。

    Attributes:
        model_name: 重排序模型名称。
        top_k: 精排后保留的文档数量。
    """

    def __init__(
        self,
        model_name: str | None = None,
        top_k: int | None = None,
        model: CrossEncoder | None = None,
    ) -> None:
        """初始化重排序器。

        Args:
            model_name: 模型名称，缺省读取全局配置 ``RERANKER_MODEL_NAME``。
            top_k: 保留数量，缺省读取全局配置 ``TOP_K_RERANK``。
            model: 可选的已加载模型实例（依赖注入，便于测试与复用）；
                为 None 时须自行调用 :meth:`load_model`。

        Note:
            构造阶段不做任何模型加载，因此本类可安全地在导入期实例化。
        """
        settings = get_settings()
        self.model_name: str = (
            model_name if model_name is not None else settings.RERANKER_MODEL_NAME
        )
        self.top_k: int = top_k if top_k is not None else settings.TOP_K_RERANK
        self._model: CrossEncoder | None = model

    @property
    def model(self) -> CrossEncoder | None:
        """返回底层模型实例；尚未加载时为 None。"""
        return self._model

    @property
    def available(self) -> bool:
        """模型是否已就绪、可供打分。"""
        return self._model is not None

    def load_model(self) -> None:
        """加载重排序模型权重到内存。

        Raises:
            RuntimeError: 模型文件缺失、下载失败或加载异常。

        Note:
            已注入模型实例时本方法为空操作，便于测试替换；
            权重缓存由 ``HF_HOME`` 环境变量决定，不落在项目根目录内。
        """
        if self._model is not None:
            return
        try:
            self._model = CrossEncoder(self.model_name)
        except Exception as exc:  # noqa: BLE001 - 统一包装为 RuntimeError
            logger.error("重排序模型加载失败: model=%s", self.model_name)
            raise RuntimeError(
                f"Failed to load reranker {self.model_name}: {exc}"
            ) from exc
        logger.info("重排序模型已加载: model=%s", self.model_name)

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """对「查询-文档」对批量打分。

        Args:
            pairs: ``(查询, 文档正文)`` 二元组列表。

        Returns:
            与 ``pairs`` 等长且顺序一致的相关性分数列表。

        Raises:
            RuntimeError: 模型尚未加载。

        Note:
            推理过程中的底层异常不在此处吞掉，由调用方决定降级策略。
        """
        if self._model is None:
            raise RuntimeError("Reranker model is not loaded; call load_model() first")
        return [float(score) for score in self._model.predict(pairs)]

    def rerank(self, query: str, documents: list[str]) -> list[tuple[str, float]]:
        """对候选文档重新排序。

        Args:
            query: 用户查询文本。
            documents: 待排序的候选文档列表。

        Returns:
            按相关性降序排列的 ``(文档, 分数)`` 列表，长度不超过 :attr:`top_k`；
            候选为空时返回空列表。

        Raises:
            RuntimeError: 模型不可用或推理失败。
        """
        if not documents:
            return []
        if self._model is None:
            raise RuntimeError("Reranker model is not loaded; call load_model() first")
        try:
            scores = self.predict([(query, document) for document in documents])
        except Exception as exc:  # noqa: BLE001 - 统一包装为 RuntimeError
            logger.error("重排序推理失败: model=%s", self.model_name)
            raise RuntimeError(f"Rerank failed: {exc}") from exc
        ranked = sorted(zip(documents, scores), key=lambda item: item[1], reverse=True)
        return ranked[: self.top_k]
