"""FishCloud 智能运维平台桌面客户端启动器。

单进程方案：在后台线程启动 FastAPI（含全部 /api/v1 接口与 SSE），
前端静态资源由同一进程托管，pywebview 以原生窗口（EdgeChromium/WebView2）
打开界面——用户双击 exe 即得完整桌面应用，无需安装任何依赖。

零磁盘约定：运行期数据仅在内存；知识库快照与模型缓存位于项目之外的
``D:\\rag-data`` / ``HF_HOME``（见 src/settings.py），不写入安装目录。

退出语义（2026-09-13 加固）：窗口关闭后按"停 HTTP 服务 → 释放数据库连接池 →
清理子进程 → ``os._exit``"顺序收尾。最后一步是刻意为之：torch/transformers 会
留下非守护线程与 ``multiprocess`` 资源跟踪进程，正常解释器退出可能被拖住十几秒
甚至挂死，桌面应用不能把用户机器上的后台进程留着。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

# 自举仓库根目录：直接运行本脚本时 sys.path[0] 是 desktop/，
# 打包时 PyInstaller 的分析也需要该路径才能找到 src 包。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("fishcloud.desktop")

#: 应用窗口与服务参数。
APP_TITLE = "FishCloud · 智能运维平台"
HOST = "127.0.0.1"
BASE_PORT = 8780
#: 退出时等待 HTTP 服务收尾的秒数。
SHUTDOWN_GRACE = 5.0


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


def icon_path() -> Path:
    """品牌图标路径（.ico，用于窗口与任务栏）。"""
    return resource_dir() / "desktop" / "icon" / "fishcloud.ico"


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


def start_server(port: int, error_holder: dict, handle: dict) -> None:
    """在当前进程内启动 uvicorn 服务线程。

    Args:
        port: 监听端口。
        error_holder: 线程内异常写入此字典，供主线程捕获后写入崩溃日志。
        handle: 就绪后写入 ``{"server": uvicorn.Server}``，供主线程优雅停机。
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

        config = uvicorn.Config(
            app, host=HOST, port=port, log_level="warning", access_log=False
        )
        server = uvicorn.Server(config)
        handle["server"] = server
        server.run()
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


def apply_window_icon() -> None:
    """把品牌图标贴到窗口上（Windows ``WM_SETICON``，尽力而为）。

    进程本体是便携 ``python.exe``，任务栏与 Alt-Tab 默认显示 Python 图标；
    这里在窗口出现后主动改图标。任何失败都只是少个图标，不影响启动。
    """
    icon = icon_path()
    if not icon.is_file():
        logger.info("未找到窗口图标文件，跳过: %s", icon)
        return

    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    WM_SETICON = 0x0080
    ICON_SMALL, ICON_BIG = 0, 1
    handle = user32.LoadImageW(
        None, str(icon), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
    )
    small = user32.LoadImageW(None, str(icon), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    if not handle:
        logger.info("窗口图标加载失败，跳过")
        return

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    for _ in range(60):  # 最多等 30 秒，窗口出现即贴图标
        found: list[int] = []

        def _callback(hwnd, _lparam, _found=found):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if APP_TITLE in buffer.value:
                _found.append(hwnd)
            return True

        user32.EnumWindows(EnumProc(_callback), 0)
        if found:
            user32.SendMessageW(found[0], WM_SETICON, ICON_BIG, handle)
            if small:
                user32.SendMessageW(found[0], WM_SETICON, ICON_SMALL, small)
            logger.info("窗口图标已应用: %s", icon.name)
            return
        time.sleep(0.5)


def shutdown(handle: dict, thread: threading.Thread | None) -> None:
    """窗口关闭后的收尾：停服务 → 释放数据库 → 结束全部子进程。

    Args:
        handle: 含 ``server`` 的字典（可能为空，例如服务由别的入口托管）。
        thread: 服务线程对象（可为 None）。
    """
    server = handle.get("server")
    if server is not None:
        server.should_exit = True
        logger.info("已请求 HTTP 服务停机")
    if thread is not None:
        thread.join(timeout=SHUTDOWN_GRACE)
        logger.info("HTTP 服务线程%s", "已退出" if not thread.is_alive() else "未在限时内退出")

    try:
        from src.platform import db as platform_db

        platform_db.dispose_engine()
    except Exception:  # noqa: BLE001 - 退出清理绝不影响进程终止
        logger.debug("释放平台数据库失败（忽略）", exc_info=True)

    _kill_children()


def _kill_children() -> None:
    """终止本进程派生的所有子进程（WebView2 渲染进程、资源跟踪进程等）。"""
    try:
        from winproc import kill_tree

        killed = kill_tree(os.getpid())
        if killed:
            logger.info("已清理 %d 个残留子进程", killed)
    except Exception:  # noqa: BLE001 - 同上，失败也要继续退出
        logger.debug("子进程清理失败（忽略）", exc_info=True)


def main() -> None:
    """桌面客户端主入口。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    port = pick_port()
    error_holder: dict = {}
    handle: dict = {}
    server = threading.Thread(
        target=start_server, args=(port, error_holder, handle), daemon=True
    )
    server.start()

    if not wait_ready(port, error_holder):
        logger.error("服务在 120 秒内未就绪，退出（详见日志）")
        sys.exit(1)

    import webview

    webview.create_window(
        APP_TITLE,
        f"http://{HOST}:{port}/",
        width=1600,
        height=900,
        min_size=(1024, 640),
    )
    webview.start(apply_window_icon)

    logger.info("窗口已关闭，开始收尾…")
    shutdown(handle, server)
    logger.info("进程退出")
    # 解释器正常退出会被 torch/HF 的非守护线程拖住，这里直接终止：
    # 收尾动作已完成，剩余线程与子进程不再有任何需要保存的状态。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


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
