# -*- coding: utf-8 -*-
"""FishCloud 免安装版启动器（编译为 .exe）。

只负责三件事：定位便携环境 → 注入 D 盘缓存环境变量 → 拉起
``App\\python\\python.exe App\\desktop\\fishcloud_desktop.py``。
真正的应用（FastAPI + 模型 + pywebview）跑在便携 Python 里，
本 exe 不含任何重依赖，因此不存在 PyInstaller 冻结 torch 的问题。

双击即可用；崩溃时在 exe 同目录写 FishCloud-crash.log 并停住窗口显示原因。
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path


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
    try:
        completed = subprocess.run(
            [str(python_exe), str(entry)],
            cwd=str(app_dir),
            env=env,
            check=False,
        )
        return completed.returncode
    except Exception:
        log = base / "FishCloud-crash.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        print(f"启动器异常，详情见 {log}")
        if getattr(sys, "frozen", False):
            input("按回车退出…")
        return 1


if __name__ == "__main__":
    sys.exit(main())
