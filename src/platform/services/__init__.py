"""平台服务包子包与 API 路由（auth/assets/events/admin）。"""

from src.platform.services import events as event_service  # noqa: F401

__all__ = ["event_service"]
