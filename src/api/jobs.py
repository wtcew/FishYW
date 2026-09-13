"""摄取任务状态机。

为什么需要它：原来的 POST /upload 是同步一次性的 —— 解析、分块、向量化、实体抽取
全部跑完才返回。大文件期间前端拿不到任何反馈，用户只看到「点了没反应」，
这正是同类产品被投诉最多的问题（文件长期卡在 Queuing）。

这里把摄取变成可观测的后台任务：

* 状态流转 ``queued → parsing(stage, progress) → success | failed | cancelled``；
* ``failed`` 可重试、``queued|parsing`` 可取消（协作式：任务在阶段回调里自查标志）；
* 注册表由锁保护，供 HTTP 请求与后台任务并发访问；
* 终态任务按上限淘汰，避免长跑进程里无限堆积。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

JobStatus = Literal["queued", "parsing", "success", "failed", "cancelled"]

#: 终态集合：进入其中之一后不再变化，前端可停止轮询。
TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})


# 取消信号的定义放在 core.preprocessing：摄取流程要按类型识别它并让它穿透进度
# 上报的容错，因此两边必须是同一个类，不能是同名的两个。
from core.preprocessing import IngestCancelled  # noqa: E402,F401 - 转出给路由层使用


@dataclass
class IngestJob:
    """一次摄取任务的可观测状态。

    Attributes:
        doc_id: 任务标识，同时也是本次上传生成的文档标识。
        filename: 原始文件名（多文件时为首个）。
        status: 当前状态。
        stage: 正在进行或最后完成的阶段名（parse/chunk/embed/entity/done）。
        progress: 0~1 的完成比例。
        error: 失败原因（仅 status=failed 时有意义）。
        chunk_count / entity_count: 成功后的产出统计。
    """

    doc_id: str
    filename: str
    status: JobStatus = "queued"
    stage: str = ""
    progress: float = 0.0
    error: str = ""
    chunk_count: int = 0
    entity_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def terminal(self) -> bool:
        """是否已进入终态。"""
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """转为可直接 JSON 序列化的字典。"""
        return {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "error": self.error,
            "chunk_count": self.chunk_count,
            "entity_count": self.entity_count,
            "terminal": self.terminal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobRegistry:
    """线程安全的摄取任务注册表。

    Note:
        取消是**协作式**的：``cancel`` 只打标记，真正的中断发生在任务下一次
        调用 :meth:`is_cancelled` 时。这样无需强杀线程，也不会留下半截写入。
    """

    def __init__(self, max_jobs: int = 200) -> None:
        """初始化注册表。

        Args:
            max_jobs: 保留的任务上限，超出时优先淘汰最旧的终态任务。
        """
        self._jobs: dict[str, IngestJob] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._max_jobs = max(1, max_jobs)

    def create(self, doc_id: str, filename: str) -> IngestJob:
        """登记一个新任务（状态 queued）。"""
        job = IngestJob(doc_id=doc_id, filename=filename)
        with self._lock:
            self._jobs[doc_id] = job
            self._prune_locked()
        return job

    def get(self, doc_id: str) -> IngestJob | None:
        """取单个任务。"""
        with self._lock:
            return self._jobs.get(doc_id)

    def list(self) -> list[IngestJob]:
        """按创建时间倒序列出全部任务。"""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)

    def update(self, doc_id: str, **fields: Any) -> None:
        """更新任务字段并刷新时间戳；任务不存在时静默忽略。"""
        with self._lock:
            job = self._jobs.get(doc_id)
            if job is None:
                return
            for key, value in fields.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            job.updated_at = time.time()

    def cancel(self, doc_id: str) -> bool:
        """请求取消：终态任务不可取消，返回 False。"""
        with self._lock:
            job = self._jobs.get(doc_id)
            if job is None or job.terminal:
                return False
            self._cancelled.add(doc_id)
        self.update(doc_id, status="cancelled", stage="cancelled")
        return True

    def is_cancelled(self, doc_id: str) -> bool:
        """任务是否已被请求取消（供进度回调轮询）。"""
        with self._lock:
            return doc_id in self._cancelled

    def clear_cancel(self, doc_id: str) -> None:
        """清掉取消标记，供重试复用同一 doc_id。"""
        with self._lock:
            self._cancelled.discard(doc_id)

    def _prune_locked(self) -> None:
        """超出上限时淘汰最旧的终态任务（调用方须已持锁）。"""
        if len(self._jobs) <= self._max_jobs:
            return
        finished = [j for j in self._jobs.values() if j.terminal]
        finished.sort(key=lambda item: item.updated_at)
        for job in finished[: max(0, len(self._jobs) - self._max_jobs)]:
            self._jobs.pop(job.doc_id, None)
            self._cancelled.discard(job.doc_id)
