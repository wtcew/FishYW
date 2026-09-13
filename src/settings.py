"""全局配置模块。

基于 ``pydantic-settings`` 从环境变量与 ``.env`` 文件加载运行时配置，
并通过 :func:`get_settings` 的 ``lru_cache`` 保证进程内单例语义。

Note:
    本模块遵循零磁盘约束：仅读取配置，不写入任何文件。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """项目运行时配置。

    字段优先级（由高到低）：构造参数 > 环境变量 > ``.env`` 文件 > 类默认值。

    Attributes:
        DEEPSEEK_API_KEY: DeepSeek API Key，已在 DSH 平台预配置。
        DEEPSEEK_MODEL_NAME: DeepSeek 主模型名称。
        DEEPSEEK_BASE_URL: DeepSeek API 服务地址。
        GLM_API_KEY: GLM API Key，已在 ZCode 平台预配置。
        GLM_MODEL_NAME: GLM 辅助模型名称。
        GLM_BASE_URL: GLM API 服务地址。
        EMBEDDING_MODEL_NAME: 向量化模型名称。
        RERANKER_MODEL_NAME: 重排序模型名称。
        CHUNK_SIZE: 文本分块大小（字符数）。
        CHUNK_OVERLAP: 相邻分块重叠大小（字符数）。
        TOP_K_RETRIEVAL: 召回阶段的候选文档数量。
        TOP_K_RERANK: 重排序后保留的文档数量。
        MAX_AGENT_ITERATIONS: Agent 反思循环的最大迭代次数。
        HF_HOME: HuggingFace 模型缓存目录（默认指向 D 盘，保护 C 盘空间）。
    """

    # DeepSeek 配置
    DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
    # V4.1 Flash 的 API 模型 ID（2026-09-12 核实）；旧名 deepseek-chat 已于 2026-07-24 停用。
    DEEPSEEK_MODEL_NAME: str = "deepseek-flash"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # GLM 配置
    GLM_API_KEY: str = os.environ.get("GLM_API_KEY", "")
    # 2026-09-13 用户裁决：底层模型 = glm-4.7-flash（免费文本档）。
    # 注意：该模型偶发 429/1305「访问量过大」（平台侧容量）；免费备选 glm-4-flash（更快）。
    GLM_MODEL_NAME: str = "glm-4.7-flash"
    GLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"

    # RAG 配置
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-m3"
    # bge-m3 输出 1024 维；向量库按此维度校验写入，维度不符会直接报错而非静默错配。
    EMBEDDING_DIM: int = 1024
    # 知识库落盘目录。默认放在项目之外，避免污染项目树；
    # 置空字符串则退回纯内存模式（重启即清空）。
    RAG_DATA_DIR: str = os.environ.get("RAG_DATA_DIR", r"D:\rag-data")
    RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-v2-m3"
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50
    TOP_K_RETRIEVAL: int = 15
    TOP_K_RERANK: int = 5
    MAX_AGENT_ITERATIONS: int = 3

    # 模型缓存：bge-m3 + bge-reranker-v2-m3 合计约 4.4GB，
    # 用户决策（2026-09-12）将其引到 D 盘，避免写爆 C 盘。
    HF_HOME: str = os.environ.get("HF_HOME", r"D:\hf-cache")

    # 平台业务库（2026-09-12 决策：MySQL 优先，SQLite 兜底；均在 D 盘或本机 MySQL）
    PLATFORM_DB: str = os.environ.get("PLATFORM_DB", r"D:\xingzhi-platform\platform.db")
    MYSQL_HOST: str = os.environ.get("MYSQL_HOST", "")
    MYSQL_PORT: int = int(os.environ.get("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.environ.get("MYSQL_USER", "")
    MYSQL_PASSWORD: str = os.environ.get("MYSQL_PASSWORD", "")
    MYSQL_DB: str = os.environ.get("MYSQL_DB", "")

    # JWT：绝不硬编码密钥。未提供 JWT_SECRET 时每次进程启动生成临时密钥
    # （开发可用；代价是重启后所有 token 失效——显式的安全默认值，非弱默认值）。
    # 生产部署必须在 .env 提供 JWT_SECRET（长度 >= 32）。
    JWT_SECRET: str = os.environ.get("JWT_SECRET", "")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 720
    # 引导期管理员密码（2026-09-13 用户决策：固定 admin/123456，便于免安装版开箱即用）。
    # .env 或环境变量提供 PLATFORM_ADMIN_PASSWORD 时优先使用；生产部署务必修改，
    # 或删除已有 admin 后重建（种子仅在"无任何用户"时创建）。
    PLATFORM_ADMIN_PASSWORD: str = os.environ.get("PLATFORM_ADMIN_PASSWORD", "123456")
    WEBHOOK_TOKENS: str = os.environ.get("WEBHOOK_TOKENS", "")
    # 自助注册（2026-09-13 用户裁决 Q2：默认开启；新账号一律 viewer 只读角色，
    # 默认需管理员在用户管理里启用 = 审批流；AUTO_ACTIVE=True 可改为注册即可登录）。
    PLATFORM_ALLOW_REGISTRATION: bool = True
    PLATFORM_REGISTRATION_AUTO_ACTIVE: bool = False
    DIAGNOSIS_TIMEOUT_SECONDS: int = 120
    AUTO_DIAGNOSIS_ENABLED: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例。

    Returns:
        进程内缓存的 :class:`Settings` 实例；重复调用返回同一对象。
    """
    return Settings()


# HuggingFace 系列库在 import 时读取进程环境变量，而本项目的重依赖
# （sentence_transformers / transformers）都是懒加载——在 settings 之后才导入，
# 因此这里在模块加载期把缓存目录注入进程环境即可生效。
# setdefault 语义：用户在系统级显式设置的变量优先级更高。
_HF_HOME: str = get_settings().HF_HOME
os.environ.setdefault("HF_HOME", _HF_HOME)
# 应用级缓存目录统一迁到 D 盘（用户约束：运行期不得写 C 盘）。
# 覆盖 torch 权重、matplotlib 字体与 XDG 通用缓存。
_AI_CACHE = r"D:\ai-cache"
for _env_name, _dir_name in (
    ("TORCH_HOME", "torch"),
    ("MPLCONFIGDIR", "matplotlib"),
    ("XDG_CACHE_HOME", "xdg"),
):
    os.environ.setdefault(_env_name, os.path.join(_AI_CACHE, _dir_name))
# sentence-transformers 把该变量直接当作 cache_folder 传给 huggingface_hub 的
# cache_dir，其目录结构就是 HF_HUB_CACHE（models--*），因此必须与权重缓存同址；
# 指向独立空目录会让离线加载在别处找不到模型而装配失败。
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", os.path.join(_HF_HOME, "hub"))
# 权重已全量缓存后强制离线加载：否则每次启动都会联网探测 adapter/revision，
# 网络不通时最多重试 5 次、启动可被拖住数分钟。
# 仅当缓存目录已有内容时才离线——首次部署的新机器仍需允许联网下载。
if os.path.isdir(os.path.join(_HF_HOME, "hub")) and os.listdir(os.path.join(_HF_HOME, "hub")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
