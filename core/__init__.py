"""核心业务层。

承载 RAG 流水线的核心算法实现。与 ``src`` 平级：``src`` 提供配置与基础设施，
``core`` 提供领域能力。

Note:
    本包遵循零磁盘约束：所有处理均在内存中完成。
"""

from __future__ import annotations
