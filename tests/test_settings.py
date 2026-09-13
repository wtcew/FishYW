"""配置与常量模块的单元测试。

使用标准库 ``unittest``，不引入额外依赖。推荐运行方式::

    python -B -m unittest discover -s tests

``-B`` 关闭字节码写入，保证测试过程不产生 ``__pycache__``。
"""

from __future__ import annotations

import unittest

from src.constants import (
    ENTITY_RELATION_TYPES,
    EVALUATION_TARGETS,
    FILE_TYPES_SUPPORTED,
    PLATFORM_CONFIG,
)
from src.settings import Settings, get_settings


class TestSettings(unittest.TestCase):
    """Settings 默认值与单例语义测试。"""

    def test_default_model_names(self) -> None:
        """代码内默认模型名应与各自 BASE_URL 指向的服务自洽。

        断言的是 ``Settings`` 的**类默认值**（``_env_file=None`` 关掉 .env），
        因此不受部署环境 .env 覆盖影响（GLM 档位切换频繁，2026-09-12 已两次变更）。

        2026-09-12 按官方文档核实：V4.1 Flash 的 API 模型 ID 为 ``deepseek-flash``，
        旧名 ``deepseek-chat`` 已于 2026-07-24 停用。
        """
        defaults = Settings(_env_file=None)
        self.assertEqual(defaults.DEEPSEEK_MODEL_NAME, "deepseek-flash")
        self.assertEqual(defaults.GLM_MODEL_NAME, "glm-4.7-flash")

    def test_effective_model_names_are_well_formed(self) -> None:
        """实际生效的模型名非空且属于对应服务族（防 .env 手改出拼写错）。"""
        settings = get_settings()
        self.assertTrue(settings.DEEPSEEK_MODEL_NAME.startswith("deepseek"))
        self.assertTrue(settings.GLM_MODEL_NAME.startswith("glm"))

    def test_default_base_urls(self) -> None:
        """默认服务地址应与官方文档一致。"""
        settings = get_settings()
        self.assertEqual(settings.DEEPSEEK_BASE_URL, "https://api.deepseek.com")
        self.assertEqual(settings.GLM_BASE_URL, "https://open.bigmodel.cn/api/paas/v4")

    def test_rag_defaults(self) -> None:
        """RAG 相关默认值应与设计一致。"""
        settings = get_settings()
        self.assertEqual(settings.EMBEDDING_MODEL_NAME, "BAAI/bge-m3")
        self.assertEqual(settings.RERANKER_MODEL_NAME, "BAAI/bge-reranker-v2-m3")
        self.assertEqual(settings.CHUNK_SIZE, 512)
        self.assertEqual(settings.CHUNK_OVERLAP, 50)
        self.assertEqual(settings.TOP_K_RETRIEVAL, 15)
        self.assertEqual(settings.TOP_K_RERANK, 5)
        self.assertEqual(settings.MAX_AGENT_ITERATIONS, 3)

    def test_get_settings_returns_cached_singleton(self) -> None:
        """get_settings 应返回同一实例，体现 lru_cache 语义。"""
        self.assertIs(get_settings(), get_settings())

    def test_settings_is_pydantic_model(self) -> None:
        """Settings 应为可实例化的配置模型。"""
        self.assertIsInstance(get_settings(), Settings)


class TestConstants(unittest.TestCase):
    """全局常量取值测试。"""

    def test_file_types_supported(self) -> None:
        """支持的文档格式应覆盖 pdf / md / docx。"""
        self.assertEqual(FILE_TYPES_SUPPORTED, ["pdf", "md", "docx"])

    def test_entity_relation_types(self) -> None:
        """实体关系类型应包含三类运维语义。"""
        self.assertEqual(len(ENTITY_RELATION_TYPES), 3)

    def test_evaluation_targets(self) -> None:
        """四项评估指标阈值应与设计一致。"""
        self.assertEqual(
            EVALUATION_TARGETS,
            {
                "faithfulness": 0.95,
                "answer_relevancy": 0.90,
                "context_recall": 0.85,
                "context_precision": 0.80,
            },
        )

    def test_platform_config(self) -> None:
        """平台角色配置应为主辅双平台。"""
        self.assertEqual(PLATFORM_CONFIG, {"primary": "deepseek", "auxiliary": "glm"})


if __name__ == "__main__":
    unittest.main()
