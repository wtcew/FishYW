"""平台测试包共享常量（不走夹具，便于跨测试模块 import）。

密码与令牌均为测试专用值，仅存在于 tests/ 内，与运行环境无关。
"""

from __future__ import annotations

ADMIN_USERNAME = "admin"
OPERATOR_USERNAME = "operator1"
VIEWER_USERNAME = "viewer1"

ADMIN_PASSWORD = "Admin-Test-Pw-01"
OPERATOR_PASSWORD = "Operator-Test-Pw-02"
VIEWER_PASSWORD = "Viewer-Test-Pw-03"

#: 测试用 JWT 密钥（>= 32 字符，避免走进程临时密钥带来的跨用例不一致）。
JWT_TEST_SECRET = "platform-test-secret-0123456789abcdef"

#: ``source:token`` 形式的 webhook 令牌表（settings.WEBHOOK_TOKENS）。
WEBHOOK_TOKENS = "zabbix:tok-zabbix-1,grafana:tok-grafana-1"

#: 设计文档 §3.3 的状态机矩阵（测试内硬编码，另外单独断言与 TRANSITIONS 一致）。
TRANSITION_MATRIX: dict[str, set[str]] = {
    "open": {"acknowledged", "closed"},
    "acknowledged": {"diagnosing", "resolved", "closed"},
    "diagnosing": {"acknowledged", "resolved", "closed"},
    "resolved": {"closed"},
    "closed": set(),
}

#: HTTP 端点 → 目标状态（状态机经这些端点驱动）。
TRANSITION_ENDPOINTS: dict[str, str] = {
    "acknowledge": "acknowledged",
    "resolve": "resolved",
    "close": "closed",
}

ALL_STATUSES = ("open", "acknowledged", "diagnosing", "resolved", "closed")
