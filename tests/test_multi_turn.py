"""多轮上下文：历史必须真的进入提示词，且缓存不跨历史复用。

这里检查的是「发出去的东西」（提示词文本、图被执行的次数），而不是函数自己的
返回值——自洽断言发现不了「函数收下了历史但没写进提示词」这类静默失效。
"""

from __future__ import annotations

import asyncio
import uuid

from src.agent.state_machine import decompose_node, generate_node, run_agent


class _CaptureLLM:
    """记录提示词的假大模型客户端。"""

    def __init__(self, reply: str) -> None:
        """保存固定回复。"""
        self.reply = reply
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        """记录提示词并返回固定回复。"""
        self.prompts.append(prompt)
        return self.reply


HISTORY = [
    {"role": "user", "content": "三号主泵昨天振动超标报警"},
    {"role": "assistant", "content": "先检查地脚螺栓，再查联轴器对中。"},
]


def test_history_reaches_decompose_prompt() -> None:
    """拆解提示词必须携带历史原话，否则追问里的指代无法补全。"""
    llm = _CaptureLLM('{"sub_queries": ["主泵振动超标", "地脚螺栓"]}')
    decompose_node({"query": "那这个怎么处理?", "history": HISTORY}, llm)  # type: ignore[arg-type]

    assert llm.prompts, "拆解节点没有调用大模型"
    prompt = llm.prompts[0]
    assert "三号主泵昨天振动超标报警" in prompt
    assert "先检查地脚螺栓" in prompt


def test_history_reaches_generate_prompt() -> None:
    """生成提示词同样必须携带历史原话。"""
    llm = _CaptureLLM('{"answer": "拧紧地脚螺栓 chunk_id:c1"}')
    state = {
        "query": "那这个怎么处理?",
        "history": HISTORY,
        "retrieved_docs": [
            {
                "chunk_id": "c1",
                "filename": "pump.md",
                "chunk_index": 0,
                "content": "地脚螺栓松动会引起振动。",
            }
        ],
    }
    generate_node(state, llm)  # type: ignore[arg-type]

    assert llm.prompts, "生成节点没有调用大模型"
    assert "三号主泵昨天振动超标报警" in llm.prompts[0]


def test_absent_history_adds_no_history_block() -> None:
    """单轮问答时不得凭空出现「对话历史」段落。"""
    llm = _CaptureLLM('{"sub_queries": ["单轮问题"]}')
    decompose_node({"query": "单轮问题", "history": []}, llm)  # type: ignore[arg-type]

    assert "对话历史" not in llm.prompts[0]


class _CountingGraph:
    """只统计执行次数的假图。"""

    def __init__(self) -> None:
        """初始化计数。"""
        self.calls = 0

    async def ainvoke(self, state: dict, config: dict) -> dict:
        """记录一次执行并原样返回状态。"""
        self.calls += 1
        return {**state, "answer": "ok"}


def test_semantic_cache_not_shared_across_histories() -> None:
    """同一问题在不同历史下不得互相命中语义缓存。

    缓存若只看 query，「第二轮追问」会直接拿到「第一轮」的答案——用户看到的是
    答非所问，而链路全程没有任何报错。
    """
    graph = _CountingGraph()
    query = "cache-probe-" + uuid.uuid4().hex

    asyncio.run(run_agent(query, graph))
    asyncio.run(run_agent(query, graph, HISTORY))
    assert graph.calls == 2, "带历史与不带历史互相命中了缓存"

    asyncio.run(run_agent(query, graph))
    assert graph.calls == 2, "同一问题重复提问应命中缓存，不必重跑"
