# -*- coding: utf-8 -*-
"""构建脚本：同步版本号 → 关闭旧进程 → PyInstaller 打包 → 归档带版本号的 exe。

用法：
    python build.py            # 按 VERSION 文件中的版本号构建
    python build.py 1.3.0      # 指定新版本号（会写回 VERSION）
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src", "netmon.py")
DIST = os.path.join(ROOT, "dist")
NAME = "网络监控中心"


def read_version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def write_version(ver):
    with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8") as f:
        f.write(ver + "\n")


def sync_source_version(ver):
    with open(SRC, encoding="utf-8") as f:
        text = f.read()
    new = re.sub(r'APP_VER = "[^"]+"', f'APP_VER = "{ver}"', text, count=1)
    if new != text:
        with open(SRC, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"[1/4] 源码版本号已同步为 {ver}")
    else:
        print(f"[1/4] 源码版本号已是 {ver}")


def kill_running():
    try:
        import psutil
    except ImportError:
        return
    n = 0
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "") == f"{NAME}.exe":
                p.kill()
                n += 1
        except Exception:
            pass
    print(f"[2/4] 关闭运行中的旧程序：{n} 个")


def build(ver):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
           "--clean", "--name", NAME,
           "--icon", os.path.join(ROOT, "src", "icon.ico"),
           "--add-data", os.path.join(ROOT, "src", "icon.ico") + os.pathsep + ".",
           "--distpath", DIST,
           "--workpath", os.path.join(ROOT, "build"),
           "--specpath", os.path.join(ROOT, "build"),
           SRC]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise SystemExit("打包失败")
    exe = os.path.join(DIST, f"{NAME}.exe")
    size = os.path.getsize(exe) / 1048576
    print(f"[3/4] 打包完成：{exe}（{size:.1f} MB）")
    return exe


def archive(exe, ver):
    arch_dir = os.path.join(DIST, "archive", f"v{ver}")
    os.makedirs(arch_dir, exist_ok=True)
    target = os.path.join(arch_dir, f"{NAME}_v{ver}.exe")
    shutil.copy2(exe, target)
    print(f"[4/4] 已归档：{target}")


def main():
    if len(sys.argv) > 1:
        ver = sys.argv[1].lstrip("v")
        write_version(ver)
    else:
        ver = read_version()
    print(f"构建版本 v{ver}")
    sync_source_version(ver)
    kill_running()
    exe = build(ver)
    archive(exe, ver)
    print(f"完成：v{ver}")


if __name__ == "__main__":
    main()
