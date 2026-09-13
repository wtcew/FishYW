"""取消语义：请求取消之后，摄取不得再把文件写进知识库。

这条路径曾经是坏的：进度回调的容错吞掉了一切异常，包括取消信号，于是任务
带着「已取消」的标签把文件真正跑完并入库——界面说取消了，库里却多一份数据。
因此这里断言的是「异常必须穿透」，而不是某个状态字段。
"""

from __future__ import annotations

import pytest

from core.preprocessing import IngestCancelled, ingest_documents_with_progress


def test_cancellation_survives_progress_callback_tolerance() -> None:
    """进度回调抛出的取消必须穿透 report，立刻中止摄取。"""
    calls: list[str] = []

    def _cancel(stage: str, ratio: float) -> None:
        calls.append(stage)
        raise IngestCancelled("用户取消")

    with pytest.raises(IngestCancelled):
        ingest_documents_with_progress(
            [(b"# title\n\nsome content", "md", "tiny.md")], _cancel
        )
    # 只在最早阶段就中止：说明没有继续往下跑分块与向量化。
    assert calls == ["parse"]
