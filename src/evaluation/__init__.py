"""评估层：RAGAS 指标评估与内存图表。

对 Agent 状态机跑黄金测试集，用 RAGAS 四项指标打分并聚合成报告；
图表以 matplotlib 内存 Figure 渲染为 PNG 字节流返回。

Note:
    评估结果、黄金测试集与图表字节流全部驻留内存，不产生任何磁盘文件。
"""

from __future__ import annotations
