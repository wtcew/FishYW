"""数据层占位包。

为什么这里是空的：
    本包曾并行存在 loader.py / preprocessor.py 一套骨架（全部为 NotImplementedError），
    与 core/preprocessing.py 构成双轨；实测前者零引用、零测试，改动极易落到空壳那一支。
    已删除，文档加载 / 清洗 / 分块 / 实体抽取的唯一实现统一在 core/preprocessing.py。

    未来若要支持 PDF / DOCX 等多格式，请在 core/preprocessing.py 内扩展，不要再新开一条链路。
"""

from __future__ import annotations
