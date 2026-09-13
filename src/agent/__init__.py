"""Agent 层：状态机编排与工具集。

实现「意图拆解 → 混合检索 → 充分性反思 → 答案生成 → 幻觉校验」
五节点状态机及其可调用工具。
"""

from __future__ import annotations
