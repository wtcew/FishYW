"""FishCloud 进程清场脚本（关闭窗口后仍有残留时使用）。

用法（双击 ``停止FishCloud.bat`` 等价于第一种）：:

    App\\python\\python.exe App\\desktop\\stop_fishcloud.py     # 自动定位本包
    python stop_fishcloud.py --root "D:\\FishCloud"            # 指定安装目录

行为：找出所有"镜像路径位于 FishCloud 目录内"的进程（便携 Python 主进程、
FastAPI 服务、启动器 FishCloud.exe、以及它们派生出的子进程），先杀叶子再杀根，
最后打印一份留/杀清单。找不到进程时返回 0（幂等，可重复执行）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from winproc import (  # noqa: E402
    ProcessInfo,
    descendants,
    fishcloud_processes,
    iter_processes,
    terminate,
)


def default_root() -> Path:
    """默认根目录：本脚本位于 ``<root>/App/desktop/`` 时向上两级。"""
    here = Path(__file__).resolve()
    app_dir = here.parent.parent  # <root>/App
    if app_dir.name.lower() == "app":
        return app_dir.parent
    return here.parent


def collect(root: Path) -> list[ProcessInfo]:
    """收集与 FishCloud 相关的进程（含它们派生出的全部子进程）。"""
    pool = iter_processes()
    by_pid = {item.pid: item for item in pool}
    matched = {item.pid: item for item in fishcloud_processes(root)}
    child_ids: list[int] = []
    for pid in list(matched):
        child_ids.extend(descendants(pid, pool))
    for pid in child_ids:
        if pid in by_pid:
            matched.setdefault(pid, by_pid[pid])
    return sorted(matched.values(), key=lambda item: item.pid)


def main() -> int:
    parser = argparse.ArgumentParser(description="终止 FishCloud 相关进程")
    parser.add_argument("--root", default=None, help="FishCloud 根目录（默认自动定位）")
    parser.add_argument(
        "--wait", type=float, default=0.0, help="执行前等待秒数（给正常退出留时间）"
    )
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else default_root()
    print(f"FishCloud 目录: {root}")

    if args.wait > 0:
        print(f"等待 {args.wait:.0f} 秒，让应用自行退出…")
        time.sleep(args.wait)

    targets = collect(root)
    if not targets:
        print("未发现运行中的 FishCloud 进程，无需处理。")
        return 0

    print(f"发现 {len(targets)} 个进程：")
    for item in targets:
        print(f"  pid={item.pid:<7} {item.name:<16} {item.exe}")

    killed = 0
    for item in reversed(targets):  # 先叶子后根，避免父进程先死导致子树被托管
        if terminate(item.pid):
            killed += 1
    time.sleep(0.5)

    remaining = collect(root)
    print(f"\n已终止 {killed} 个进程；复查剩余 {len(remaining)} 个。")
    for item in remaining:
        print(f"  仍在运行: pid={item.pid} {item.name} {item.exe}")
    if remaining:
        print("提示：仍有进程未退出（可能权限不足），请用管理员身份重跑本脚本。")
        return 1
    print("FishCloud 已完全退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
