"""FishCloud 智能运维平台桌面客户端启动器。

单进程方案：在后台线程启动 FastAPI（含全部 /api/v1 接口与 SSE），
前端静态资源由同一进程托管，pywebview 以原生窗口（EdgeChromium/WebView2）
打开界面——用户双击 exe 即得完整桌面应用，无需安装任何依赖。

零磁盘约定：运行期数据仅在内存；知识库快照与模型缓存位于项目之外的
``D:\\rag-data`` / ``HF_HOME``（见 src/settings.py），不写入安装目录。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

# 自举仓库根目录：直接运行本脚本时 sys.path[0] 是 desktop/，
# 打包时 PyInstaller 的分析也需要该路径才能找到 src 包。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("fishcloud.desktop")

#: 应用窗口与服务参数。
APP_TITLE = "FishCloud · 智能运维平台"
HOST = "127.0.0.1"
BASE_PORT = 8780


def resource_dir() -> Path:
    """返回随包资源目录（兼容 PyInstaller 打包与源码运行）。

    Returns:
        打包后为 ``sys._MEIPASS``；源码运行为仓库根目录。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent.parent


def frontend_dist() -> Path:
    """定位前端构建产物目录。

    Returns:
        ``frontend_dist``（打包内嵌）或 ``frontend/dist``（源码）；
        两者都不存在时返回不存在的路径，由调用方降级为纯接口模式。
    """
    bundled = resource_dir() / "frontend_dist"
    if bundled.is_dir():
        return bundled
    return resource_dir() / "frontend" / "dist"


def pick_port() -> int:
    """从 ``BASE_PORT`` 起探测可用端口。

    Returns:
        第一个未被占用的端口号。
    """
    import socket

    for offset in range(10):
        port = BASE_PORT + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((HOST, port)) != 0:
                return port
    return BASE_PORT


def start_server(port: int, error_holder: dict) -> None:
    """在当前进程内启动 uvicorn 服务线程。

    Args:
        port: 监听端口。
        error_holder: 线程内异常写入此字典，供主线程捕获后写入崩溃日志。
    """
    try:
        import uvicorn

        from src.api.routes import app

        dist = frontend_dist()
        if dist.is_dir():
            # 静态托管必须挂在 API 路由之后：前缀 "/" 只兜底未被 /api 命中的请求。
            from fastapi.staticfiles import StaticFiles

            app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
            logger.info("前端静态资源已挂载: %s", dist)

        uvicorn.run(app, host=HOST, port=port, log_level="warning", access_log=False)
    except BaseException as exc:  # noqa: BLE001 - 线程异常必须透传到主线程留痕
        error_holder["error"] = exc
        raise


def wait_ready(port: int, error_holder: dict, timeout: float = 120.0) -> bool:
    """轮询健康检查直至服务就绪。

    Args:
        port: 监听端口。
        error_holder: 服务线程的异常容器。
        timeout: 最长等待秒数（首次启动需加载嵌入与重排模型）。

    Returns:
        是否就绪。

    Raises:
        BaseException: 服务线程抛出的异常原样上抛，保证崩溃留痕。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "error" in error_holder:
            raise error_holder["error"]
        try:
            with urllib.request.urlopen(
                f"http://{HOST}:{port}/api/v1/health", timeout=2
            ) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 未就绪时静默重试
            time.sleep(0.5)
    return False


def main() -> None:
    """桌面客户端主入口。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    port = pick_port()
    error_holder: dict = {}
    server = threading.Thread(target=start_server, args=(port, error_holder), daemon=True)
    server.start()

    if not wait_ready(port, error_holder):
        logger.error("服务在 120 秒内未就绪，退出（详见日志）")
        sys.exit(1)

    import webview

    window = webview.create_window(
        APP_TITLE,
        f"http://{HOST}:{port}/",
        width=1600,
        height=900,
        min_size=(1024, 640),
    )
    # 窗口关闭即进程退出，daemon 服务线程随之结束，全部内存数据就地释放。
    webview.start()
    logger.info("窗口已关闭，进程退出")


def _crash_logged() -> None:
    """窗口模式下 stderr 不可见：任何未捕获异常都写入 exe 同目录 crash.log。"""
    try:
        main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 顶层兜底，崩溃必须留痕
        import traceback

        crash_path = Path(sys.executable).with_name("crash.log")
        if getattr(sys, "frozen", False) is not True:
            crash_path = _ROOT / "desktop" / "crash.log"
        crash_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    _crash_logged()
