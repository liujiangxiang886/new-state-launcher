"""用「打进 pck + 场景 load」的方式校验 GDScript 语法。

为什么不用 ``--check-only --script``：
- Godot 的**模板 exe 不支持 ``--path``**（编译时没开
  ``disable_path_overrides``），它会把 **exe 所在目录**当项目根 ——
  在那儿找不到 ``project.godot`` 就弹 ALERT「Couldn't load project data」。
- 而且 ``--check-only --script`` 在这个 exe 上会**挂住不退出**（超时被杀）。

可靠做法（与 tests/test_hook_inject.py 同一套）：
把被测脚本打进 pck，写一个场景 ``load()`` 它 —— 引擎真正去解析，
报不报 Parse Error 一看便知。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.pck.writer import PckWriter  # noqa: E402

WORK = ROOT / "builds" / "gd-syntax-check"


def check_scripts(scripts: dict[str, str], tag: str = "main") -> tuple[bool, str]:
    """把 ``{相对路径: 内容}`` 打进 pck，用场景 load 一遍。

    返回 ``(有没有解析错误, 错误详情)``。
    """
    case = WORK / tag
    if case.exists():
        shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True, exist_ok=True)

    # exe 与 pck **必须同名**：Godot 模板 exe 只认「与 exe 同名的 .pck」。
    # 异名时它不挂载 pck、还会弹模态 ALERT「Is the .pck file missing?」——
    # 弹窗会卡住进程，而且「没挂载」意味着脚本压根没被 load，
    # 检查结论是**假阳性**。
    writer = PckWriter(case / "godot.pck", line="4.6")
    writer.add_text(
        "project.godot",
        "config_version=5\n\n[application]\n\nconfig/name=\"syntax check\"\n"
        "run/main_scene=\"res://main.tscn\"\n"
        "config/features=PackedStringArray(\"4.6\", \"GL Compatibility\")\n\n"
        # [autoload] 段不能少：bootstrap.gd 引用 Compat、entry.gd 引用 mount_plan，
        # 缺了会报 "Identifier \"Compat\" not declared" —— 那是**依赖缺失**，
        # 不是语法错误，会掩盖真正的问题。
        "[autoload]\n\n"
        "Compat=\"*res://_compat/bootstrap.gd\"\n"
        "HookRuntime=\"*res://_compat/hook_runtime.gd\"\n"
        "ModHost=\"*res://_compat/mod_host.gd\"\n\n"
        "[rendering]\n\nrenderer/rendering_method=\"gl_compatibility\"\n",
    )
    writer.add_text(
        "main.tscn",
        '[gd_scene format=3]\n\n'
        '[ext_resource type="Script" path="res://main.gd" id="1"]\n\n'
        '[node name="Main" type="Node"]\n'
        'script = ExtResource("1")\n',
    )
    loads = "".join(f'\tload("res://{name}")\n' for name in scripts)
    # 哨兵：证明 pck **真的挂上了**。读不到哨兵就说明 pck 没挂 ——
    # 那时「无解析错误」是假阳性，必须直接判失败。
    loads += '\tvar _s: Script = load("res://_sentinel.gd")\n'
    loads += '\tif _s == null:\n\t\tprint("SENTINEL-MISSING")\n' 
    writer.add_text("main.gd", "extends Node\n\nfunc _ready() -> void:\n" + loads + "\tget_tree().quit(0)\n")
    for name, text in scripts.items():
        writer.add_text(name, text)
    writer.add_text("_sentinel.gd", "extends Node\n\nfunc ok() -> bool:\n\treturn true\n")
    writer.commit()

    # exe 必须与 project.godot **同目录** —— 模板 exe 只认「exe 同名的 .pck」
    # 或「exe 同目录的 project.godot」，不认 --path。
    exe = case / "godot.exe"
    # 用启动器自带的底座引擎当语法检查器（独立引擎 exe 已随版本线下线移除）
    shutil.copyfile(ROOT / "runtime" / "host46" / "jjxf46.exe", exe)
    env = {
        **dict(os.environ),
        "APPDATA": str(case / "appdata"),
        "LOCALAPPDATA": str(case / "appdata"),
    }
    (case / "appdata").mkdir(exist_ok=True)

    try:
        proc = subprocess.run(
            [str(exe), "--headless", "--quit-after", "600"],
            cwd=str(case),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "超时（引擎没退出）"

    err = (proc.stdout or "") + (proc.stderr or "")
    if "SENTINEL-MISSING" in err:
        return False, "哨兵没读到 —— pck 根本没挂上，检查结论是假阳性"
    bad = [
        ln
        for ln in err.splitlines()
        if "Parse Error" in ln or "Failed to load script" in ln or "SCRIPT ERROR" in ln
    ]
    return (not bad), ("\n".join(bad[:6]) or "无解析错误")


def main() -> int:
    compat_dir = ROOT / "runtime" / "_compat"
    scripts = {
        f"_compat/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted(compat_dir.glob("*.gd"))
    }
    print(f"检查 {len(scripts)} 个 GDScript：{', '.join(scripts)}")
    ok, detail = check_scripts(scripts)
    print()
    if ok:
        print("全部通过：引擎能解析")
        return 0
    print("有解析错误：")
    print(detail)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
