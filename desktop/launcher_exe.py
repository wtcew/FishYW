# -*- coding: utf-8 -*-
"""FishCloud 免安装版启动器（编译为 .exe）。

只负责四件事：定位便携环境 → 注入 D 盘缓存环境变量 → 在 Windows 作业对象里
拉起 ``App\\python\\python.exe App\\desktop\\fishcloud_desktop.py`` → 退出前清场。
真正的应用（FastAPI + 模型 + pywebview）跑在便携 Python 里，
本 exe 不含任何重依赖，因此不存在 PyInstaller 冻结 torch 的问题。

进程回收（2026-09-13 加固）：子进程被放进一个 ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
的作业对象。无论用户是关窗口、关控制台还是从任务管理器结束本 exe，
整棵子进程树（便携 Python、WebView2 渲染进程、模型加载线程）都会被系统一并终止，
不会留下占着内存的后台进程。

双击即可用；崩溃时在 exe 同目录写 FishCloud-crash.log 并停住窗口显示原因。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import traceback
from ctypes import wintypes
from pathlib import Path

# ── Windows 作业对象常量 ────────────────────────────────────────────────
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class IO_COUNTERS(ctypes.Structure):
    """IO_COUNTERS：作业对象扩展信息的一部分（此处仅占位）。"""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION。"""

    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION。"""

    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _base_dir() -> Path:
    """exe 所在目录（源码运行时为脚本所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _build_env(app_dir: Path) -> dict[str, str]:
    """在当前环境上叠加便携运行所需的 D 盘缓存与临时目录。"""
    env = os.environ.copy()
    build_tmp = r"D:\build_tmp"
    env.setdefault("TMP", build_tmp)
    env.setdefault("TEMP", build_tmp)
    env.setdefault("HF_HOME", r"D:\hf-cache")
    # sentence-transformers 的 cache_folder 必须与权重缓存同址（见 docs/HANDOFF.md 踩坑 #3）
    env.setdefault("SENTENCE_TRANSFORMERS_HOME", r"D:\hf-cache\hub")
    env.setdefault("TORCH_HOME", r"D:\ai-cache\torch")
    env.setdefault("MPLCONFIGDIR", r"D:\ai-cache\matplotlib")
    env.setdefault("XDG_CACHE_HOME", r"D:\ai-cache\xdg")
    env["APP_DIR"] = str(app_dir)
    return env


def _create_kill_on_close_job() -> int | None:
    """创建"句柄关闭即杀光成员"的作业对象，失败返回 None（降级为普通父子关系）。"""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        wintypes.HANDLE(job),
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(wintypes.HANDLE(job))
        return None
    return job


def _assign_to_job(job: int, pid: int) -> bool:
    """把子进程加入作业对象；权限不足或系统不支持时返回 False。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(
            kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle))
        )
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _terminate_job(job: int) -> None:
    """杀掉作业对象内仍存活的所有进程，并关闭句柄。"""
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject(wintypes.HANDLE(job), 0)
    kernel32.CloseHandle(wintypes.HANDLE(job))


def main() -> int:
    """定位并拉起便携应用；返回进程退出码。"""
    base = _base_dir()
    app_dir = base / "App"
    python_exe = app_dir / "python" / "python.exe"
    entry = app_dir / "desktop" / "fishcloud_desktop.py"

    missing = [str(p) for p in (python_exe, entry) if not p.is_file()]
    if missing:
        print("缺少便携环境文件：")
        for item in missing:
            print("  -", item)
        print("请确认本 exe 与 App/ 目录位于同一层。")
        if getattr(sys, "frozen", False):
            input("按回车退出…")
        return 2

    env = _build_env(app_dir)
    job = _create_kill_on_close_job()
    process = None
    try:
        process = subprocess.Popen(
            [str(python_exe), str(entry)],
            cwd=str(app_dir),
            env=env,
            shell=False,
        )
        if job is not None and not _assign_to_job(job, process.pid):
            # 作业对象不可用时保持普通等待语义：子进程仍受进程树约束。
            print("提示：无法加入作业对象，退出清场将退化为常规父子等待。")
        return process.wait()
    except Exception:
        log = base / "FishCloud-crash.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        print(f"启动器异常，详情见 {log}")
        if getattr(sys, "frozen", False):
            input("按回车退出…")
        return 1
    finally:
        if job is not None:
            # 无论走哪条分支，退出前把子进程树清干净（KILL_ON_JOB_CLOSE 双保险）。
            _terminate_job(job)


if __name__ == "__main__":
    sys.exit(main())
