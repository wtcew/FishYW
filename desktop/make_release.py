"""FishCloud 发行包构建脚本（免安装版 + 便携 zip）。

用法::

    python desktop/make_release.py --all          # 同步 + 重建 exe + 打 zip
    python desktop/make_release.py --sync         # 只同步源码/前端到 App/
    python desktop/make_release.py --exe          # 只重建 FishCloud.exe（带品牌图标）
    python desktop/make_release.py --zip          # 只把免安装版打成 FishCloud-Portable-*.zip
    python desktop/make_release.py --verify       # 只做发布前检查（密钥/图标/产物）

产物（均在 ``release/`` 下，该目录已被 .gitignore 排除）：

* ``release/免安装版/FishCloud.exe`` —— 单文件启动器（含作业对象退出清场 + 品牌图标）
* ``release/免安装版/FishCloud-Portable-1.0.0.zip`` —— 根目录为 ``FishCloud/`` 的便携包

安全红线：``App/.env`` **永不进包**（含模型/JWT/Webhook 密钥）。同步与打包都会显式跳过，
``--verify`` 会二次确认。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
ICON = DESKTOP / "icon" / "fishcloud.ico"
PORTABLE = ROOT / "release" / "免安装版"
APP = PORTABLE / "App"
WORK = Path(r"D:\build_tmp\pyinstaller-fishcloud")
APP_NAME = "FishCloud"
APP_VERSION = "1.0.0"

#: 绝不进包的敏感文件（相对 App/ 的路径）。
FORBIDDEN = (".env", ".env.local", ".env.production")
#: 不进包的目录名：缓存、依赖与本地工具残留。
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".git", ".mimosa"}


def mirror(source: Path, target: Path) -> int:
    """把目录镜像到目标位置（多余文件删除，含缓存与工具残留），返回复制的文件数。"""
    if not source.is_dir():
        raise SystemExit(f"源目录不存在: {source}")
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    wanted: set[Path] = set()
    for item in source.rglob("*"):
        if any(part in SKIP_DIRS for part in item.parts):
            continue
        relative = item.relative_to(source)
        wanted.add(relative)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        copied += 1
    # 清掉目标里"源已没有"的文件，以及源里被跳过的缓存/残留目录
    for existing in sorted(target.rglob("*"), reverse=True):
        if existing.is_dir():
            if existing.name in SKIP_DIRS:
                shutil.rmtree(existing, ignore_errors=True)
            continue
        if any(part in SKIP_DIRS for part in existing.parts):
            continue
        if existing.relative_to(target) not in wanted:
            existing.unlink()
    for existing in sorted(target.rglob("*"), reverse=True):
        if existing.is_dir() and not any(existing.iterdir()):
            existing.rmdir()
    return copied


def write_stop_bat() -> Path:
    """写出「停止FishCloud.bat」双击入口（供免安装版与安装版共用）。"""
    bat = PORTABLE / "停止FishCloud.bat"
    bat.write_text(
        "@echo off\r\n"
        "rem 双击即可强制结束所有 FishCloud 进程（关闭窗口后仍有残留时使用）。\r\n"
        "setlocal\r\n"
        'cd /d "%~dp0"\r\n'
        'set "TMP=D:\\build_tmp"\r\n'
        'set "TEMP=D:\\build_tmp"\r\n'
        '"App\\python\\python.exe" "App\\desktop\\stop_fishcloud.py"\r\n'
        "echo.\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    return bat


def purge_junk() -> int:
    """清理 App/ 下的缓存与工具残留目录（__pycache__ / .mimosa 等），返回删除目录数。"""
    removed = 0
    for path in sorted(APP.rglob("*"), reverse=True):
        if path.is_dir() and path.name in SKIP_DIRS:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def sync() -> None:
    """把仓库源码与前端产物同步进免安装版 App 目录。"""
    APP.mkdir(parents=True, exist_ok=True)
    total = 0
    total += mirror(ROOT / "src", APP / "src")
    total += mirror(ROOT / "core", APP / "core")
    desktop_target = APP / "desktop"
    desktop_target.mkdir(parents=True, exist_ok=True)
    for name in ("fishcloud_desktop.py", "stop_fishcloud.py", "winproc.py"):
        shutil.copy2(DESKTOP / name, desktop_target / name)
        total += 1
    total += mirror(DESKTOP / "icon", desktop_target / "icon")
    total += mirror(ROOT / "frontend" / "dist", APP / "frontend_dist")
    shutil.copy2(ROOT / ".env.example", APP / ".env.example")
    shutil.copy2(ROOT / "requirements.txt", APP / "requirements.txt")
    total += 2
    purge_junk()
    write_stop_bat()
    print(f"同步完成：{total} 个文件 → {APP}（缓存与工具残留已清理）")


def build_exe() -> None:
    """用 PyInstaller 重建带品牌图标的单文件启动器。"""
    if not ICON.is_file():
        raise SystemExit(f"缺少图标，先运行 desktop/icon/make_icon.py：{ICON}")
    import PyInstaller.__main__

    WORK.mkdir(parents=True, exist_ok=True)
    PyInstaller.__main__.run(
        [
            "--onefile",
            "--console",
            "--name",
            APP_NAME,
            "--icon",
            str(ICON),
            "--distpath",
            str(PORTABLE),
            "--workpath",
            str(WORK),
            "--specpath",
            str(WORK),
            "--noconfirm",
            "--clean",
            str(DESKTOP / "launcher_exe.py"),
        ]
    )
    print(f"启动器已重建：{PORTABLE / (APP_NAME + '.exe')}")


def make_zip() -> Path:
    """把免安装版打成根目录为 ``FishCloud/`` 的便携包（存储模式，秒级完成）。

    打包前先清缓存：**运行过 App 的目录会生成 ``__pycache__``**，
    不清理会让包体虚胖（实测 1342 MB → 1469 MB）并把字节码带进发布包。
    """
    purged = purge_junk()
    if purged:
        print(f"已清理 {purged} 个缓存/残留目录后再打包")
    target = PORTABLE / f"{APP_NAME}-Portable-{APP_VERSION}.zip"
    if target.exists():
        target.unlink()
    count = 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for item in sorted(PORTABLE.rglob("*")):
            if item.is_dir():
                continue
            if item.suffix == ".zip" or item.name in FORBIDDEN:
                continue
            relative = item.relative_to(PORTABLE)
            archive.write(item, f"{APP_NAME}/{relative.as_posix()}")
            count += 1
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"便携包已生成：{target}（{count} 个文件，{size_mb:.1f} MB）")
    return target


def verify() -> int:
    """发布前检查：密钥文件、图标、产物完整性。返回问题数量。"""
    problems: list[str] = []
    for name in FORBIDDEN:
        if (APP / name).exists():
            problems.append(f"App/{name} 存在——发布包会泄露密钥，请删除后再打包")
    if not ICON.is_file():
        problems.append(f"缺少图标 {ICON}")
    if not (PORTABLE / f"{APP_NAME}.exe").is_file():
        problems.append("缺少 FishCloud.exe，请先 --exe")
    if not (APP / "desktop" / "stop_fishcloud.py").is_file():
        problems.append("缺少 stop_fishcloud.py，请先 --sync")
    if not (APP / ".env.example").is_file():
        problems.append("缺少 .env.example，请先 --sync")
    print("检查项：")
    print(f"  App/.env        : {'存在（需排除）' if (APP / '.env').exists() else '不存在 ✔'}")
    print(f"  图标            : {ICON.name if ICON.is_file() else '缺失'}")
    print(f"  启动器          : {APP_NAME}.exe {'就绪' if (PORTABLE / (APP_NAME + '.exe')).is_file() else '缺失'}")
    for problem in problems:
        print("  ✗", problem)
    if not problems:
        print("  全部通过，可发布。")
    return len(problems)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 FishCloud 发行包")
    parser.add_argument("--sync", action="store_true", help="同步源码与前端产物")
    parser.add_argument("--exe", action="store_true", help="重建单文件启动器")
    parser.add_argument("--zip", action="store_true", help="生成便携版 zip")
    parser.add_argument("--verify", action="store_true", help="只做发布前检查")
    parser.add_argument("--all", action="store_true", help="同步 + 重建 exe + 打 zip")
    args = parser.parse_args()

    if not any(vars(args).values()):
        parser.print_help()
        return 1
    if args.all or args.sync:
        sync()
    if args.all or args.exe:
        build_exe()
    if args.all or args.zip:
        make_zip()
    if args.all or args.verify:
        return 1 if verify() else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
