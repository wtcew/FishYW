"""内存知识图谱存储。

以 ``networkx.DiGraph`` 承载运维领域实体与关系，为混合检索提供图谱召回。
图结构与实体倒排索引均常驻内存，不做 GraphML 序列化（零磁盘约束）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


class MemoryGraphStore:
    """线程安全的内存有向图存储。

    节点为运维实体（``type`` 属性记录实体类型，如 ``device`` / ``fault_type``），
    边为实体间关系（``type`` 属性记录关系类型，如 ``device_fault``）。
    另维护 ``entity_name -> [source_chunk_ids]`` 倒排索引，用于把实体回溯到来源分块。

    Note:
        全部写操作与读操作都在同一把 ``threading.Lock`` 内完成。
    """

    def __init__(self) -> None:
        """初始化空图存储。

        Note:
            状态由 ``_graph`` / ``_entity_index`` / ``_lock`` 三个私有属性构成，
            均驻留内存。
        """
        self._graph: nx.DiGraph = nx.DiGraph()
        self._entity_index: dict[str, list[str]] = {}
        self._lock: threading.Lock = threading.Lock()

    def add_entities(self, entities: list[dict]) -> None:
        """批量写入实体与关系。

        Args:
            entities: 记录列表。实体记录含 ``name`` / ``type`` / ``source_chunk_id``；
                关系记录含 ``source`` / ``target`` / ``type`` / ``source_chunk_id``。
                记录可带 ``kind`` 字段（``entity`` 或 ``relation``）以显式区分；
                缺省时按字段推断——含 ``name`` 视为实体，否则视为关系。

        Note:
            同一实体多次写入时，``source_chunk_ids`` 按首次出现顺序**去重合并**；
            节点/边走 ``add_node`` / ``add_edge``，重复写入是幂等的。
            缺 ``name``（实体）或缺 ``source`` / ``target``（关系）的记录会被跳过。
        """
        with self._lock:
            for record in entities:
                kind = record.get("kind")
                if kind is None:
                    kind = "entity" if "name" in record else "relation"

                if kind == "entity":
                    name = record.get("name")
                    if not name:
                        continue
                    self._graph.add_node(name, type=record.get("type", "unknown"))
                    chunk_ids = self._entity_index.setdefault(name, [])
                    source = record.get("source_chunk_id")
                    if source and source not in chunk_ids:
                        chunk_ids.append(source)
                else:
                    source = record.get("source")
                    target = record.get("target")
                    if not source or not target:
                        continue
                    self._graph.add_edge(
                        source, target, type=record.get("type", "unknown")
                    )
        logger.info("图谱写入: records=%d nodes=%d", len(entities), self._graph.number_of_nodes())

    def search_by_keyword(self, keyword: str, max_results: int = 5) -> list[dict]:
        """按关键词子串匹配中心实体，返回其邻域子图。

        Args:
            keyword: 查询关键词，匹配时不区分大小写。
            max_results: 最多返回的中心实体数量，缺省 5。

        Returns:
            每个命中实体的字典列表，含 ``center_entity``（中心实体）、
            ``neighbors``（后继 + 前驱节点）与 ``subgraph_text``
            （形如 ``头实体 -> 尾实体 (关系类型)`` 的多行文本）；
            无命中时返回空列表。
        """
        with self._lock:
            matched = [
                node
                for node in self._graph.nodes
                if keyword.lower() in str(node).lower()
            ][:max_results]

            results: list[dict] = []
            for node in matched:
                successors = list(self._graph.successors(node))
                predecessors = list(self._graph.predecessors(node))
                subgraph = self._graph.subgraph([node] + successors + predecessors)
                subgraph_text = "\n".join(
                    f"{u} -> {v} ({data.get('type', 'unknown')})"
                    for u, v, data in subgraph.edges(data=True)
                )
                results.append(
                    {
                        "center_entity": node,
                        "neighbors": successors + predecessors,
                        "subgraph_text": subgraph_text,
                    }
                )
        return results

    def get_neighbors(self, entity_name: str, depth: int = 1) -> list[dict]:
        """按跳数返回实体的邻居。

        Args:
            entity_name: 中心实体名称。
            depth: 最大跳数，缺省 1。

        Returns:
            ``{"entity": 节点名, "distance": 跳数}`` 列表，不含中心实体自身。

        Raises:
            KeyError: ``entity_name`` 不在图中。
        """
        with self._lock:
            if entity_name not in self._graph:
                raise KeyError(entity_name)
            lengths = nx.single_source_shortest_path_length(
                self._graph, entity_name, cutoff=depth
            )
        return [
            {"entity": node, "distance": distance}
            for node, distance in lengths.items()
            if node != entity_name
        ]

    def entity_sources(self, entity_name: str) -> list[str]:
        """返回实体对应的来源分块 ID 列表。

        Args:
            entity_name: 实体名称。

        Returns:
            去重后的 ``source_chunk_id`` 列表副本；实体不存在时返回空列表。
        """
        with self._lock:
            return list(self._entity_index.get(entity_name, []))

    def count(self) -> tuple[int, int]:
        """返回图的规模。

        Returns:
            ``(节点数, 边数)`` 二元组。

        Note:
            在锁内读取，保证节点数与边数是同一写操作后的一致快照。
        """
        with self._lock:
            return (self._graph.number_of_nodes(), self._graph.number_of_edges())

    def clear(self) -> None:
        """清空图与实体倒排索引。

        Note:
            仅释放内存中的对象引用，不做任何磁盘落盘。
        """
        with self._lock:
            self._graph.clear()
            self._entity_index.clear()
        logger.info("图谱已清空")

    def node_types(self) -> dict[str, Any]:
        """返回 ``实体名 -> 实体类型`` 的映射副本。

        Returns:
            节点类型映射；图为空时返回空字典。
        """
        with self._lock:
            return {
                node: data.get("type", "unknown")
                for node, data in self._graph.nodes(data=True)
            }
