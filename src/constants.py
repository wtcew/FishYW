"""全局常量定义模块。

集中声明跨模块共享的枚举式常量，避免魔法值散落在业务代码中。

Note:
    本模块为纯常量声明，不含任何副作用与文件读写。
"""

from __future__ import annotations

FILE_TYPES_SUPPORTED: list[str] = ["pdf", "md", "docx"]

ENTITY_RELATION_TYPES: list[str] = [
    "设备-故障",
    "故障-解决方案",
    "组件-设备",
]

EVALUATION_TARGETS: dict[str, float] = {
    "faithfulness": 0.95,
    "answer_relevancy": 0.90,
    "context_recall": 0.85,
    "context_precision": 0.80,
}

PLATFORM_CONFIG: dict[str, str] = {
    "primary": "deepseek",
    "auxiliary": "glm",
}
