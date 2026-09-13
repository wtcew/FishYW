"""接口层测试（pytest）。

覆盖全部九个业务端点、SSE 事件契约、错误降级与零磁盘约束：

1. **import 与路由** —— 应用可导入，OpenAPI 列全端点；
2. **健康检查** —— 状态与三类存储规模；
3. **诊断** —— 非流式查询成功/失败，SSE 事件序列与错误事件；
4. **上传** —— 内存摄取、空文件拒绝；
5. **调参与评估** —— 检索、触发评估、取结果；
6. **链路与反馈** —— trace 查找、反馈校验；
7. **零磁盘** —— AST 扫描禁用落盘 API。

运行方式::

    python -B -m pytest -p no:cacheprovider -q tests/test_api.py
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any, AsyncIterator, Iterator
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from src.api import routes

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "src" / "api" / "routes.py"

BANNED_ATTRS = {
    "write", "write_text", "write_bytes", "dump", "savetxt", "to_csv", "to_json",
    "savez", "shelve", "to_disk", "NamedTemporaryFile", "mkstemp", "mkdtemp",
}
BANNED_NAMES = {"open", "pickle"}


class StubVectorStore:
    """内存向量存储替身。"""

    def __init__(self) -> None:
        """初始化空存储。"""
        self.items: int = 0
        self.added: list[Any] = []
        self.deleted: list[str] = []

    def count(self) -> int:
        """返回向量条数。"""
        return self.items

    def delete_document(self, doc_id: str) -> int:
        """删除该 doc_id 的全部分块并记录调用，返回删除条数。

        重试端点依赖它清掉上一次入库的残留：没有这一步，同一 doc_id 重跑就会
        把同样的内容再插一遍，表现为检索结果里同段话出现两次。
        """
        self.deleted.append(doc_id)
        removed = 0
        kept: list[Any] = []
        for _embeddings, chunks in self.added:
            for chunk in chunks:
                if str(getattr(chunk, "metadata", {}).get("doc_id")) == doc_id:
                    removed += 1
                else:
                    kept.append(chunk)
        self.added = [("kept", kept)] if kept else []
        self.items -= removed
        return removed

    def list_documents(self) -> list[Any]:
        """按 doc_id 聚合列出文档，供知识库列表端点使用。"""
        grouped: dict[str, Any] = {}
        for chunk in self.all_documents():
            key = str(getattr(chunk, "metadata", {}).get("doc_id") or "unknown")
            entry = grouped.setdefault(key, {"doc_id": key, "chunks": 0})
            entry["chunks"] += 1
        return list(grouped.values())

    def add(self, embeddings: Any, chunks: list[Any]) -> None:
        """记录入库调用。"""
        self.added.append((embeddings, chunks))
        self.items += len(chunks)

    def all_documents(self) -> list[Any]:
        """返回全部已入库分块。

        上传端点会用全量文档重建 BM25 索引（只用本次 chunks 会把此前入库的
        知识挤出词法索引），因此替身必须提供这一读取能力。
        """
        return [chunk for _embeddings, chunks in self.added for chunk in chunks]


class StubGraphStore:
    """内存图谱替身。"""

    def __init__(self) -> None:
        """初始化空图。"""
        self.records: list[dict] = []

    def count(self) -> tuple[int, int]:
        """返回（节点数, 边数）。"""
        return (len(self.records), 0)

    def add_entities(self, entities: list[dict]) -> None:
        """记录写入调用。"""
        self.records.extend(entities)


class StubRetriever:
    """混合检索器替身。"""

    def __init__(self) -> None:
        """初始化。"""
        self.indexed: int = 0

    def retrieve(self, query: str) -> list[tuple[Any, float]]:
        """返回固定候选。"""

        class _Doc:
            """占位文档。"""

            page_content = "正文"
            metadata = {"chunk_id": "c1"}

        return [(_Doc(), 0.87)]

    def build_bm25_index(self, chunks: list[Any]) -> None:
        """记录索引构建。"""
        self.indexed = len(chunks)


class StubGraph:
    """状态图替身：按节点顺序产出状态分片。"""

    def __init__(self, fail: bool = False) -> None:
        """初始化。

        Args:
            fail: 为 True 时在首个分片后抛异常。
        """
        self.fail: bool = fail

    async def astream(self, state: dict, config: dict | None = None) -> AsyncIterator[dict]:
        """产出五个节点的状态分片。"""
        yield {"decompose": {"sub_queries": ["子问题一", "子问题二"]}}
        if self.fail:
            raise RuntimeError("graph broken")
        yield {"retrieve": {"retrieved_docs": [{"score": 0.9}, {"score": 0.5}]}}
        yield {"reflect": {"reflection": {"sufficient": True, "missing": []}}}
        yield {"generate": {"answer": "诊断答案"}}
        yield {"hallucination_check": {"has_hallucination": False, "hallucination_sentences": []}}


def make_state(graph: StubGraph | None = None) -> Any:
    """构造已就绪的内存状态容器。

    Args:
        graph: 可选的状态图替身。

    Returns:
        ready 为 True 的 AppState。
    """
    state = routes.AppState()
    state.vector_store = StubVectorStore()
    state.graph_store = StubGraphStore()
    state.retriever = StubRetriever()
    state.agent_graph = graph if graph is not None else StubGraph()
    state.ready = True
    return state


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """构造注入了替身组件的测试客户端。"""

    async def fake_init(state: Any) -> None:
        """用替身组件替代真实初始化。"""
        state.vector_store = StubVectorStore()
        state.graph_store = StubGraphStore()
        state.retriever = StubRetriever()
        state.agent_graph = StubGraph()
        state.ready = True

    with mock.patch.object(routes, "_init_components", new=fake_init):
        with TestClient(routes.app) as test_client:
            yield test_client


# ═══════════ import 与路由 ═══════════


def test_import_and_openapi_lists_endpoints() -> None:
    """应用可导入，且 OpenAPI 列出全部业务端点。"""
    paths = routes.app.openapi()["paths"]
    expected = {
        "/api/v1/health",
        "/api/v1/diagnose",
        "/api/v1/query",
        "/api/v1/upload",
        "/api/v1/retrieve",
        "/api/v1/evaluate",
        "/api/v1/evaluate/results",
        "/api/v1/trace/{trace_id}",
        "/api/v1/feedback",
    }
    assert expected <= set(paths)


def test_openapi_has_no_download_link() -> None:
    """OpenAPI 契约中不含任何文件下载响应。"""
    schema = routes.app.openapi()
    assert "FileResponse" not in str(schema)
    # octet-stream 只允许出现在 multipart 请求体（上传输入），
    # 响应定义中不得出现二进制下载流
    upload_responses = schema["paths"]["/api/v1/upload"]["post"]["responses"]
    assert "application/octet-stream" not in str(upload_responses)
    assert "text/event-stream" not in str(upload_responses)


# ═══════════ 健康检查 ═══════════


def test_health_ok(client: TestClient) -> None:
    """健康检查返回 200 与状态字段。"""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "vector_count", "graph_nodes", "graph_edges"}


def test_health_reports_degraded_when_not_ready(client: TestClient) -> None:
    """组件未就绪时状态为 degraded 而非报错。"""
    client.app.state.rag.ready = False
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


# ═══════════ 诊断：非流式 ═══════════


def test_query_returns_answer_and_trace(client: TestClient) -> None:
    """非流式查询返回答案、来源与追踪号。"""
    async def fake_run_agent(query: str, graph: Any) -> dict:
        """返回固定状态。"""
        return {"answer": "答案", "sources": [{"chunk_id": "c1"}], "iterations": 1}

    with mock.patch("src.agent.state_machine.run_agent", new=fake_run_agent):
        response = client.post("/api/v1/query", json={"query": "设备重启"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "答案"
    assert body["sources"] == [{"chunk_id": "c1"}]
    assert body["trace_id"]


def test_query_returns_500_on_failure(client: TestClient) -> None:
    """诊断失败时返回 500 与错误详情。"""

    async def boom(query: str, graph: Any) -> dict:
        """抛出异常。"""
        raise RuntimeError("llm down")

    with mock.patch("src.agent.state_machine.run_agent", new=boom):
        response = client.post("/api/v1/query", json={"query": "x"})

    assert response.status_code == 500
    assert "llm down" in str(response.json()["detail"])


def test_query_rejects_empty_text(client: TestClient) -> None:
    """空问题被 pydantic 校验拦截。"""
    assert client.post("/api/v1/query", json={"query": ""}).status_code == 422


def test_query_returns_503_when_not_ready(client: TestClient) -> None:
    """组件未就绪时返回 503 而非静默空结果。"""
    client.app.state.rag.ready = False
    response = client.post("/api/v1/query", json={"query": "x"})
    assert response.status_code == 503


# ═══════════ SSE 流式诊断 ═══════════


def test_diagnose_emits_full_event_sequence(client: TestClient) -> None:
    """SSE 事件序列与状态机五节点一一对应。"""
    with client.stream("POST", "/api/v1/diagnose", json={"query": "设备重启"}) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())

    for event in ("decompose", "retrieve", "reflect", "generate", "hallucination", "done"):
        assert f"event: {event}" in body
    assert "subQuestions" in body
    assert "docCount" in body
    assert "topScore" in body
    assert "traceId" in body


def test_diagnose_content_type_is_event_stream(client: TestClient) -> None:
    """流式响应声明的媒体类型为 text/event-stream。"""
    with client.stream("POST", "/api/v1/diagnose", json={"query": "x"}) as res:
        assert res.headers["content-type"].startswith("text/event-stream")


def test_diagnose_emits_error_event_on_failure() -> None:
    """图执行失败时以 error 事件收尾，而非断流。"""

    async def fake_init(state: Any) -> None:
        """注入会失败的图。"""
        state.vector_store = StubVectorStore()
        state.graph_store = StubGraphStore()
        state.retriever = StubRetriever()
        state.agent_graph = StubGraph(fail=True)
        state.ready = True

    with mock.patch.object(routes, "_init_components", new=fake_init):
        with TestClient(routes.app) as test_client:
            with test_client.stream(
                "POST", "/api/v1/diagnose", json={"query": "x"}
            ) as res:
                body = "".join(res.iter_text())

    assert "event: error" in body
    assert "graph broken" in body
    assert "event: decompose" in body


# ═══════════ 上传 ═══════════


def _await_job(client: TestClient, doc_id: str, timeout: float = 20.0) -> dict:
    """轮询摄取任务直到进入终态，返回最后一次快照。

    上传改成 202 受理后任务在后台跑，断言前必须等终态；用轮询而非固定 sleep，
    才能既不等太久、又不在慢机器上偶发失败。
    """
    import time as _time

    deadline = _time.time() + timeout
    snapshot: dict = {}
    while _time.time() < deadline:
        snapshot = client.get(f"/api/v1/knowledge/jobs/{doc_id}").json()
        if snapshot.get("status") in {"success", "failed", "cancelled"}:
            return snapshot
        _time.sleep(0.05)
    return snapshot


def test_upload_is_accepted_immediately_then_completes(client: TestClient) -> None:
    """上传立即返回 202 受理态；后台摄取完成后任务转 success 且分块入库。"""
    from types import SimpleNamespace

    class _Chunk:
        """占位分块。"""

        page_content = "正文"
        metadata = {"chunk_id": "c1"}

    fake = SimpleNamespace(
        chunks=[_Chunk()],
        embeddings="EMB",
        entities=[{"name": "设备A", "type": "device"}],
    )
    observed: list = []

    def _fake(files, on_stage=None, doc_id=None):
        observed.append((files, doc_id))
        if on_stage is not None:
            on_stage("embed", 0.7)
        return fake

    with mock.patch("core.preprocessing.ingest_documents_with_progress", side_effect=_fake):
        response = client.post(
            "/api/v1/upload",
            files={"files": ("ops.md", b"content", "text/markdown")},
        )
        # 关键契约：请求不等摄取，立即拿到 doc_id 去轮询进度。
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert len(body["jobs"]) == 1
        doc_id = body["jobs"][0]["doc_id"]
        assert doc_id
        snapshot = _await_job(client, doc_id)

    assert snapshot["status"] == "success", snapshot
    assert snapshot["progress"] == 1.0
    assert snapshot["stage"] == "done"
    assert snapshot["chunk_count"] == 1
    assert snapshot["entity_count"] == 1
    # 入库用的必须是受理时派发的 doc_id，而不是摄取环节另起的标识。
    assert observed and observed[0][1] == doc_id


def test_upload_response_has_no_download_link(client: TestClient) -> None:
    """上传响应不含磁盘路径或下载链接（回显的文件名不算路径）。"""
    from types import SimpleNamespace

    fake = SimpleNamespace(chunks=[], embeddings="E", entities=[])
    with mock.patch("core.preprocessing.ingest_documents_with_progress", return_value=fake):
        response = client.post(
            "/api/v1/upload",
            files={"files": ("a.md", b"x", "text/markdown")},
        )
        _await_job(client, response.json()["jobs"][0]["doc_id"])

    blob = response.text
    for token in ("http://", "https://", "download", ":\\", "/tmp", "/var", "/home", "file://"):
        assert token not in blob


def test_upload_failure_lands_in_job_table(client: TestClient) -> None:
    """摄取抛错时任务转 failed 并带原因，界面不再无声空等。"""
    with mock.patch(
        "core.preprocessing.ingest_documents_with_progress",
        side_effect=RuntimeError("unsupported pdf"),
    ):
        response = client.post(
            "/api/v1/upload",
            files={"files": ("bad.pdf", b"%PDF", "application/pdf")},
        )
        doc_id = response.json()["jobs"][0]["doc_id"]
        snapshot = _await_job(client, doc_id)

    assert snapshot["status"] == "failed"
    assert "unsupported pdf" in (snapshot["error"] or "")


def test_cancel_wins_over_in_flight_stage(client: TestClient) -> None:
    """阶段中途取消：后台线程跑完后不得把终态改写回 success。"""
    import threading
    import time as _time
    from types import SimpleNamespace

    release = threading.Event()
    fake = SimpleNamespace(chunks=[], embeddings="E", entities=[])

    def _slow_stage(files, on_stage=None, doc_id=None):
        # 模拟「正在解析、尚未回调」的窗口：此处取消只能靠阶段返回后复查。
        release.wait(15)
        return fake

    with mock.patch(
        "core.preprocessing.ingest_documents_with_progress", side_effect=_slow_stage
    ):
        response = client.post(
            "/api/v1/upload",
            files={"files": ("slow.md", b"x", "text/markdown")},
        )
        doc_id = response.json()["jobs"][0]["doc_id"]
        cancelled = client.post(f"/api/v1/knowledge/jobs/{doc_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        # 已进入终态的任务不可重复取消
        assert client.post(f"/api/v1/knowledge/jobs/{doc_id}/cancel").status_code == 409

        release.set()
        _time.sleep(0.5)
        final = client.get(f"/api/v1/knowledge/jobs/{doc_id}").json()

    assert final["status"] == "cancelled"


def test_job_list_and_retry_roundtrip(client: TestClient) -> None:
    """任务列表按 doc_id 可查；失败任务重试后能再跑成。"""
    from types import SimpleNamespace

    fake = SimpleNamespace(chunks=[], embeddings="E", entities=[])
    attempts: list = []

    def _flaky(files, on_stage=None, doc_id=None):
        attempts.append(str(doc_id))
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return fake

    with mock.patch("core.preprocessing.ingest_documents_with_progress", side_effect=_flaky):
        response = client.post(
            "/api/v1/upload",
            files={"files": ("flaky.md", b"x", "text/markdown")},
        )
        doc_id = response.json()["jobs"][0]["doc_id"]
        assert _await_job(client, doc_id)["status"] == "failed"

        listed = client.get("/api/v1/knowledge/jobs").json()
        assert listed["total"] >= 1
        assert any(item["doc_id"] == doc_id for item in listed["jobs"])

        retried = client.post(f"/api/v1/knowledge/jobs/{doc_id}/retry")
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"
        snapshot = _await_job(client, doc_id)

    assert snapshot["status"] == "success", snapshot
    assert len(attempts) == 2
    # 重试必须先清掉该 doc_id 的旧分块，否则同一份内容会被入库两次。
    assert client.app.state.rag.vector_store.deleted == [doc_id]
    # 上一次的失败原因要清掉，否则界面会一直挂着过期报错。
    assert not snapshot["error"]


# ═══════════ 检索调参 / 评估 ═══════════


def test_retrieve_returns_documents(client: TestClient) -> None:
    """调参端点返回候选文档与分数。"""
    response = client.post("/api/v1/retrieve", json={"query": "重启", "topK": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["documents"][0]["chunk_id"] == "c1"
    assert body["documents"][0]["score"] == 0.87


def test_evaluate_trigger_and_results(client: TestClient) -> None:
    """触发评估后结果端点从 no_result 变为可用。"""
    assert client.get("/api/v1/evaluate/results").json()["status"] == "no_result"

    with mock.patch.object(routes, "asyncio") as fake_asyncio:
        fake_asyncio.create_task = lambda coro: coro.close()
        assert client.post("/api/v1/evaluate").json()["status"] == "running"

    client.app.state.rag.evaluation_result = {
        "overall_metrics": {"faithfulness": 0.96},
        "passed": True,
    }
    body = client.get("/api/v1/evaluate/results").json()
    assert body["status"] == "ok"
    assert body["passed"] is True


# ═══════════ 链路与反馈 ═══════════


def test_trace_reports_not_found(client: TestClient) -> None:
    """未知追踪号返回 found=False。"""
    body = client.get("/api/v1/trace/nope").json()
    assert body["found"] is False


def test_trace_found_after_query(client: TestClient) -> None:
    """查询后可按追踪号取回明细。"""

    async def fake_run_agent(query: str, graph: Any) -> dict:
        """返回固定状态。"""
        return {"answer": "答案", "sources": [], "iterations": 0}

    with mock.patch("src.agent.state_machine.run_agent", new=fake_run_agent):
        trace_id = client.post("/api/v1/query", json={"query": "q"}).json()["trace_id"]

    body = client.get(f"/api/v1/trace/{trace_id}").json()
    assert body["found"] is True
    assert body["answer"] == "答案"


def test_feedback_accepted(client: TestClient) -> None:
    """反馈被接受并计数。"""
    response = client.post(
        "/api/v1/feedback",
        json={"trace_id": "t1", "rating": "up", "comment": "有用"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["feedback_count"] == 1


def test_feedback_rejects_unknown_rating(client: TestClient) -> None:
    """非法评分被校验拦截。"""
    response = client.post(
        "/api/v1/feedback", json={"trace_id": "t1", "rating": "maybe"}
    )
    assert response.status_code == 422


# ═══════════ CORS ═══════════


def test_cors_preflight_allows_any_origin(client: TestClient) -> None:
    """预检请求放行任意来源（前端开发服务器跨域）。"""
    response = client.options(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert response.headers.get("access-control-allow-origin") in {"*", "http://localhost:5173"}


# ═══════════ 零磁盘 ═══════════


class TestZeroDisk:
    """零磁盘静态扫描：源码禁出现任何落盘调用。"""

    def test_source_has_no_disk_api_calls(self) -> None:
        """AST 扫描禁用 open / pickle / write / mkstemp 等落盘 API。"""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRS:
                found.append(f"attr:{func.attr}")
            if isinstance(func, ast.Name) and func.id in BANNED_NAMES:
                found.append(f"name:{func.id}")
        assert found == []

    def test_upload_reads_bytes_directly(self) -> None:
        """上传端点必须直接 await read()，不经临时文件中转。"""
        source = MODULE_PATH.read_text(encoding="utf-8")
        assert "await upload.read()" in source
