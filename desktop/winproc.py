"""Windows 进程工具（纯标准库 ctypes 实现，便携 Python 可直接用）。

只做三件事：枚举进程（含父进程与镜像全路径）、终止进程、按进程树清场。
之所以不用 ``taskkill``/``wmic``：桌面版要能在任意机器上零依赖运行，
且不引入任何 shell 拼接（避免命令行注入面）。

所有句柄都在 ``try/finally`` 中关闭；权限不足的进程会被跳过而不是抛异常。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    """Toolhelp32 进程快照条目（对应 PROCESSENTRY32W）。"""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


@dataclass(frozen=True)
class ProcessInfo:
    """一条进程记录。"""

    pid: int
    ppid: int
    name: str
    exe: str


def iter_processes() -> list[ProcessInfo]:
    """枚举当前所有进程（含父进程 id 与镜像全路径，取不到路径时为空串）。"""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []

    processes: list[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pid = int(entry.th32ProcessID)
            processes.append(
                ProcessInfo(
                    pid=pid,
                    ppid=int(entry.th32ParentProcessID),
                    name=entry.szExeFile,
                    exe=image_path(pid),
                )
            )
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


def image_path(pid: int) -> str:
    """返回指定进程的镜像全路径；无权限或已退出时返回空串。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(MAX_PATH * 4)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def terminate(pid: int) -> bool:
    """强杀指定进程，返回是否成功。"""
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def descendants(pid: int, processes: list[ProcessInfo] | None = None) -> list[int]:
    """返回指定进程的全部后代进程 id（广度优先，含多层子进程）。"""
    pool = processes if processes is not None else iter_processes()
    children: dict[int, list[int]] = {}
    for item in pool:
        children.setdefault(item.ppid, []).append(item.pid)

    found: list[int] = []
    queue = list(children.get(pid, []))
    while queue:
        child = queue.pop(0)
        if child in found:
            continue
        found.append(child)
        queue.extend(children.get(child, []))
    return found


def kill_tree(pid: int, exclude: set[int] | None = None) -> int:
    """终止进程及其所有后代，返回被终止的进程数。"""
    skip = exclude or set()
    pool = iter_processes()
    targets = descendants(pid, pool)
    targets.append(pid)
    killed = 0
    for target in reversed(targets):  # 先叶子后根，避免父进程先死导致子进程被托管
        if target in skip or target == os.getpid():
            continue
        if terminate(target):
            killed += 1
    return killed


def fishcloud_processes(root: Path) -> list[ProcessInfo]:
    """找出属于指定安装根目录的全部 FishCloud 进程。

    判定规则（二者其一）：
    * 镜像路径位于 ``root`` 之内（便携 Python / 应用线程）；
    * 进程名为 ``FishCloud.exe`` 且镜像路径位于 ``root`` 之内（启动器本体）。

    Args:
        root: FishCloud 根目录（免安装版目录或安装目录）。

    Returns:
        匹配到的进程列表（不含当前进程）。
    """
    root_text = str(root).rstrip("\\/").lower()
    matched: list[ProcessInfo] = []
    for item in iter_processes():
        if item.pid == os.getpid():
            continue
        exe = (item.exe or "").lower()
        if not exe:
            continue
        if exe.startswith(root_text + "\\") or exe.startswith(root_text + "/"):
            matched.append(item)
    return matched
