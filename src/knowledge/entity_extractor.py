"""运维领域实体与关系抽取。

从文档分块中抽取设备、故障、解决方案与组件等实体，
并按预定义关系类型建立三元组。

Note:
    当前为骨架实现，抽取逻辑将在知识层阶段填充。
"""

from __future__ import annotations

import logging

from src.constants import ENTITY_RELATION_TYPES

logger = logging.getLogger(__name__)


class EntityExtractor:
    """实体与关系抽取器。

    对文本分块执行命名实体识别与关系判定，输出结构化三元组。

    Attributes:
        relation_types: 允许的关系类型集合。
    """

    def __init__(self, relation_types: list[str] | None = None) -> None:
        """初始化抽取器。

        Args:
            relation_types: 关系类型白名单，缺省读取 ``ENTITY_RELATION_TYPES``。
        """
        self.relation_types: list[str] = (
            relation_types if relation_types is not None else ENTITY_RELATION_TYPES
        )

    def extract_entities(self, text: str) -> list[str]:
        """抽取文本中的实体。

        Args:
            text: 待抽取的文本分块。

        Returns:
            去重后的实体名称列表。

        Raises:
            ValueError: 输入文本为空。
        """
        raise NotImplementedError("实体抽取逻辑将在知识层阶段实现")

    def extract_relations(self, text: str) -> list[tuple[str, str, str]]:
        """抽取文本中的实体关系三元组。

        Args:
            text: 待抽取的文本分块。

        Returns:
            ``(头实体, 关系类型, 尾实体)`` 三元组列表。

        Raises:
            ValueError: 输入文本为空。
        """
        raise NotImplementedError("关系抽取逻辑将在知识层阶段实现")
