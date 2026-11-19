"""组装「机甲旋风兼容启动器」发布包（按《扩展打包规范》《剧情打包规范》）。

直接由来源打 zip，不落地中间目录、不做批量删除：
  - 程序本体与运行时来自 PyInstaller 产物（``build_dist_new/...`` 优先，可用
    环境变量 ``JJXF_BUILD_DIR`` 指向 ``build_dist2`` 之类的目录；都没有则退回 ``dist/...``）
  - 数据目录来自 dist/机甲旋风兼容启动器/ 的 config/baseline/engines/runtime/schemas
  - 文档与声明来自项目根 docs/ THIRD_PARTY_NOTICES.md 运行要求.md

排除项（严格不随包）：
  - 顶层目录 packs/ builds/ cache/ logs/ profiles/ app/ tools/ tests/ 等
  - 源码 *.py *.pyc（任何位置）
  - 游戏本体 mchGame*.exe *Demo*.pck（重要文件，指向包外）

发布态 config/extensions.json 置空；stories.json 用相对 overlay 路径。
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "机甲旋风兼容启动器"
OUT_ZIP = ROOT / "机甲旋风启动器1.1.zip"

DIST = ROOT / "dist" / RELEASE
#: exe 产物目录，默认 ``build_dist``。
#: 用环境变量 ``JJXF_BUILD_DIR`` 可指向别的构建目录——
#: 需要重新构建又不想让 PyInstaller 清空旧目录时用得上（本机安全钩子会拦批量删除）。
BUILD = Path(os.environ.get("JJXF_BUILD_DIR") or (ROOT / "build_dist")) / RELEASE
SRC_EXE_DIR = BUILD if BUILD.exists() else DIST

DATA_DIRS = ["config", "baseline", "engines", "runtime", "schemas"]
EXTRA_FILES = ["THIRD_PARTY_NOTICES.md", "运行要求.md"]

# 顶层目录排除（发布包顶层只应有 exe/_internal/config/.../docs）
_EXCLUDE_TOP = {"packs", "builds", "cache", "logs", "profiles",
                "app", "tools", "tests", ".git", ".pytest_cache",
                "_build_tools", "_safebox", "__pycache__"}

#: 运行时目录里可能残留的**本机运行报告**，不随包发给玩家
_RUNTIME_REPORTS = {"mount_report.json", "smoke_report.json",
                    "mod_report.json", "saves-diff.json"}


def _skip(rel: str, top_exclude: bool = True) -> bool:
    parts = Path(rel).parts
    if top_exclude and parts[0] in _EXCLUDE_TOP:
        return True
    # 开发/内部残留（任何层级都不随包）：配置备份、QA 证据脚本
    if any(p in {"backups", "evidence"} for p in parts):
        return True
    name = parts[-1].lower()
    if name.endswith(".py") or name.endswith(".pyc"):
        return True
    # 跑过冒烟/开窗验证之后，host 目录里会留下这几份报告。它们描述的是
    # **我们机器上的那次运行**，对玩家没有意义，还会把发布包的体积说明带偏。
    if name in _RUNTIME_REPORTS:
        return True
    if "mchgame" in name or name.endswith("demo.pck") or name.endswith("demo.exe"):
        return True
    return False


def _add_tree(z: zipfile.ZipFile, src: Path, prefix: str, skip=None, top_exclude: bool = True):
    """把 ``src`` 整棵树写进 zip。

    ``top_exclude=False`` 用于 ``_internal/``：那里面的 ``app/`` 是 PyInstaller
    打进去的**资源**（图标就在 ``_internal/app/launcher.ico``），不是项目根的
    源码目录。早先这里一视同仁地套用顶层排除表，结果把 launcher.ico 一起
    剔掉了——exe 内嵌图标照旧，窗口/任务栏却只能走兜底，三处图标又对不上。
    """
    skip = skip or set()
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if rel in skip:
            continue
        if _skip(rel, top_exclude=top_exclude):
            continue
        if p.is_file():
            z.write(p, f"{prefix}/{rel}")


def main() -> int:
    exe = SRC_EXE_DIR / f"{RELEASE}.exe"
    if not exe.exists():
        print(f"[错误] 找不到程序本体：{exe}")
        return 1

    added = 0
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        # 1) exe + _internal（保持 _internal/ 子目录）
        z.write(exe, f"{RELEASE}/{RELEASE}.exe"); added += 1
        _add_tree(z, SRC_EXE_DIR / "_internal", f"{RELEASE}/_internal", top_exclude=False)
        print(f"[1] exe + _internal  ← {SRC_EXE_DIR}")

        # 2) 数据目录（来自 dist，已是发布态相对路径；保持各自子目录）
        for d in DATA_DIRS:
            src = DIST / d
            if src.exists():
                skip = {"extensions.json"} if d == "config" else None
                _add_tree(z, src, f"{RELEASE}/{d}", skip=skip)
                print(f"[2] {d}/  ← dist")
            else:
                print(f"[!] 缺少数据目录 dist/{d}")

        # 3) 文档与声明（保持 docs/ 子目录）
        if (ROOT / "docs").exists():
            _add_tree(z, ROOT / "docs", f"{RELEASE}/docs")
            print("[3] docs/  ← 项目根")
        for f in EXTRA_FILES:
            p = ROOT / f
            if p.exists():
                z.write(p, f"{RELEASE}/{f}"); added += 1
                print(f"[3] {f}")
            else:
                print(f"[!] 缺少 {f}")

        # 4) 发布态 config 修正（内存变换，不改 dist）
        ext_src = DIST / "config" / "extensions.json"
        if ext_src.exists():
            data = json.loads(ext_src.read_text(encoding="utf-8"))
            n = len(data.get("items", []))
            data["items"] = []
            z.writestr(f"{RELEASE}/config/extensions.json",
                       json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            added += 1
            print(f"[4] extensions.json 置空（原 {n} 条）→ items=[]")

        sto_src = DIST / "config" / "stories.json"
        if sto_src.exists():
            sd = json.loads(sto_src.read_text(encoding="utf-8"))
            bad = [it.get("id") for it in sd.get("items", []) if ":" in it.get("overlay", "")]
            print(f"[4] stories.json overlay 校验："
                  f"{'全部相对路径 OK' if not bad else '绝对路径→' + str(bad)}")

    total = len(zipfile.ZipFile(OUT_ZIP).namelist())
    print(f"[5] 打包完成：{OUT_ZIP.name}  含 {total} 个文件")
    print(f"     体积约 {OUT_ZIP.stat().st_size / 1_000_000:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
