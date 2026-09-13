"""检索层：向量存储、融合原语与重排序基础设施。

提供内存向量索引（:class:`MemoryVectorStore`）、通用 RRF 融合原语
（:func:`reciprocal_rank_fusion`）与 Cross-Encoder 重排序器
（:class:`CrossEncoderReranker`）；三路召回的编排见 :mod:`core.retrieval`。

Note:
    索引、模型权重与缓存全部驻留内存，进程退出即释放。
"""

from __future__ import annotations
