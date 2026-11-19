#!/usr/bin/env python
"""构建兼容底座（base46 / base47）。

做四件事：

1. 从反解工程的 ``project.godot`` 派生底座配置——只改三处：
   工程名、主场景指向 ``res://_compat/entry.tscn``、Autoload 首位插入 ``Compat``；
   同时移除 ``gui/theme/custom``（挂载前路径还不存在，改由 entry.gd 在挂载后应用）。
2. 把 ``runtime/_compat`` 的三个引导文件装进底座。
3. 打成 ``runtime/host<line>/jjxf<line>.pck``。
4. 准备运行时 exe：

   - 4.6 从 1.45 exe 截断出纯 PE 桩（去掉内嵌 pck 与尾部标记）。

示例::

    python tools/build_base.py 47
    python tools/build_base.py 46 47
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod, paths  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402

COMPAT_SRC = ROOT / "runtime" / "_compat"

#: 要装进底座的引导文件
COMPAT_FILES = (
    "bootstrap.gd",
    "mount_plan.gd",
    "entry.gd",
    "entry.tscn",
    "hook_runtime.gd",
    "mod_host.gd",
    "frame_probe.gd",
    "audio_guard.gd",
    "mem_slim.gd",
)

#: 底座工程名后缀，决定 user:// 目录，避免与真实存档混淆
BASE_NAME = "机甲旋风 · 兼容底座 {line}"

OLD_MAIN = 'run/main_scene="res://scenes/start_page.tscn"'
NEW_MAIN = 'run/main_scene="res://_compat/entry.tscn"'
AUTOLOAD_ANCHOR = '[autoload]\n\n'
COMPAT_AUTOLOAD = (
    'Compat="*res://_compat/bootstrap.gd"\n'
    'HookRuntime="*res://_compat/hook_runtime.gd"\n'
    'ModHost="*res://_compat/mod_host.gd"\n'
    'FrameProbe="*res://_compat/frame_probe.gd"\n'
    'AudioGuard="*res://_compat/audio_guard.gd"\n'
)
GUI_THEME_LINE = 'theme/custom="res://assets/ui/offline_theme.tres"'

#: 低配机器用得上的渲染档位。
#:
#: 为什么加这些（2026-11-14）
#: ------------------------
#: 原版 ``project.godot`` 的 ``[rendering]`` 段只有 3 行（driver / method），
#: **没有任何限帧或 vsync 设置**。在 8 GB 机器上这意味着：
#:
#: - 引擎按显示器刷新率无上限地跑。高刷屏（144/165 Hz）上，帧率越高，
#:   贴图与节点的换代越频繁，LRU 缓存与 GPU 侧资源分配翻转越快，
#:   页面文件上的抖动越明显。
#: - 无限帧还会让 CPU 一直「忙等下一帧」，与系统其它程序抢内存带宽。
#:
#: 这游戏是 2D 逐帧序列图，60 帧与动画帧率一致，再高只是空转。
#:
#: 只加这两项，不加 ``rendering/scaling_3d/scale``：那条只对 3D 生效，
#: 这款游戏全程 2D（``CanvasItem`` + 序列图），写进去是误导。
#:
#: **键名的段位置很重要**（是踩过的坑）：Godot 4 里
#:
#: - ``application/run/max_fps`` 属于 ``[application]`` 段 → 写 ``run/max_fps``
#: - ``display/window/vsync/vsync_mode`` 属于 ``[display]`` 段 → 写
#:   ``window/vsync/vsync_mode``
#:
#: 把后者写进 ``[rendering]`` 段不会报错，但**也不会生效** —— ``project.godot``
#: 的 section 会被拼成完整键名，写错段等于写了一个无关的新键。
#: （Godot 3 是 ``display/window/vsync/use_vsync`` 布尔值，4 改成了枚举，别混。）

#: 插进 ``[application]`` 段末尾的限帧行。
LOW_SPEC_APPLICATION = """
; ---- 启动器底座注入（低配适配）：限帧 ----
; 无上限时高刷屏会把 GPU 与内存带宽吃满，8 GB 机器上体现为换页抖动。
; 60 与这游戏的动画帧率一致，再高只是空转。
run/max_fps=60
"""

#: 插进 ``[display]`` 段末尾的 vsync 行。
LOW_SPEC_DISPLAY = """
; ---- 启动器底座注入（低配适配）：关闭垂直同步 ----
; 限帧已经兜住了画面撕裂，再开 vsync 会额外占一帧缓冲。
window/vsync/vsync_mode=0
"""


def _insert_into_section(text: str, section: str, block: str, marker: str) -> str:
    """把 ``block`` 追加到 ``section`` 段末尾；段不存在则在文件末尾新建。

    ``marker`` 用来判断是否已经注入过（幂等，重复跑不会叠两份）。
    """
    if marker in text:
        return text
    head = f"\n[{section}]\n"
    if head not in text:
        return text.rstrip() + f"\n\n[{section}]\n" + block
    start = text.index(head)
    tail = text.find("\n[", start + 1)
    body = text[start:tail] if tail > 0 else text[start:]
    rest = text[tail:] if tail > 0 else ""
    return text[:start] + body.rstrip() + "\n" + block + rest


def transform_project(text: str, line: str) -> str:
    """把游戏原版 project.godot 改造成底座配置。"""
    # 1) 工程名
    m = re.search(r'^config/name=".*?"$', text, flags=re.M)
    if not m:
        raise SystemExit("源 project.godot 里找不到 config/name")
    text = text[: m.start()] + f'config/name="{BASE_NAME.format(line=line)}"' + text[m.end() :]

    # 2) 主场景
    if OLD_MAIN not in text:
        raise SystemExit(f"源 project.godot 里找不到主场景行：{OLD_MAIN}")
    text = text.replace(OLD_MAIN, NEW_MAIN, 1)

    # 3) Autoload 首位插入 Compat
    if AUTOLOAD_ANCHOR not in text:
        raise SystemExit("源 project.godot 里找不到 [autoload] 段")
    if "Compat=" not in text:
        text = text.replace(AUTOLOAD_ANCHOR, AUTOLOAD_ANCHOR + COMPAT_AUTOLOAD, 1)

    # 4) 移除启动期无法解析的主题引用
    if GUI_THEME_LINE in text:
        text = re.sub(r"\n\[gui\]\n\n" + re.escape(GUI_THEME_LINE) + r"\n", "\n", text, count=1)

    # 5) 帧去重必须排在**所有游戏 autoload 之后**：Game 在 _ready 里解析 data/*.json，
    #    早于它执行就什么都拿不到。所以单独追加到 [autoload] 段末尾，不能塞进
    #    上面那批「挂载前必须就位」的 Compat 组里。
    if "MemSlim=" not in text:
        start = text.index("[autoload]")
        nxt = text.find("\n[", start + 1)
        if nxt < 0:
            raise SystemExit("project.godot 的 [autoload] 段没有结束标记")
        text = text[:nxt] + '\nMemSlim="*res://_compat/mem_slim.gd"' + text[nxt:]

    # 6) 低配渲染档位：限帧 + 关 vsync。
    #
    #    注意**段位置**：max_fps 属于 [application]，vsync 属于 [display]。
    #    写错段不会报错，但那个键根本不生效（project.godot 的段名会拼进
    #    完整键名）。这是 Godot 4 与 3 的差异点，也是很容易踩的坑。
    #
    #    原版这一段一行都没有，所以高刷屏上引擎会跑满刷新率，白吃内存带宽
    #    与页面文件配额 —— 8 GB 机器上这就是换页抖动的来源之一。
    text = _insert_into_section(
        text, "application", LOW_SPEC_APPLICATION, "run/max_fps=60"
    )
    text = _insert_into_section(
        text, "display", LOW_SPEC_DISPLAY, "window/vsync/vsync_mode=0"
    )

    return text


def build(line: str, cfg: dict) -> dict:
    src_meta = cfg["runtime_sources"][line]
    src = Path(src_meta["project_godot"])
    if not src.exists():
        raise SystemExit(f"缺少反解工程配置：{src}")

    base_dir = ROOT / "runtime" / f"base{line.replace('.', '')}"
    host_dir = ROOT / "runtime" / f"host{line.replace('.', '')}"
    base_dir.mkdir(parents=True, exist_ok=True)
    host_dir.mkdir(parents=True, exist_ok=True)

    text = transform_project(src.read_text(encoding="utf-8"), line)
    (base_dir / "project.godot").write_text(text, encoding="utf-8")

    compat_dir = base_dir / "_compat"
    compat_dir.mkdir(exist_ok=True)
    for name in COMPAT_FILES:
        shutil.copyfile(COMPAT_SRC / name, compat_dir / name)

    pck_path = host_dir / f"jjxf{line.replace('.', '')}.pck"
    w = PckWriter(pck_path, line=line)
    w.add_file("project.godot", base_dir / "project.godot")
    for name in COMPAT_FILES:
        w.add_file(f"_compat/{name}", compat_dir / name)
    res = w.commit()

    exe_path = host_dir / (f"jjxf{line.replace('.', '')}.exe")
    # 用 cfg_mod.resolve_path 而非 Path(...)：打包分发后配置里可能是相对路径，
    # Path 会按当前工作目录解析，从别的目录调用就找不到引擎。
    engine_src = cfg_mod.resolve_path((cfg["engines"][line] or {}).get("path"))
    if engine_src is None:
        raise SystemExit(f"{line} 引擎未配置路径")
    if not engine_src.exists():
        raise SystemExit(f"缺少 {line} 引擎二进制：{engine_src}")
    if not exe_path.exists() or exe_path.stat().st_size != engine_src.stat().st_size:
        shutil.copyfile(engine_src, exe_path)
    exe_note = f"复制自 {engine_src.name}（{line} 引擎）"

    print(f"[{line}] 底座构建完成")
    print(f"   project : {base_dir / 'project.godot'}")
    print(f"   pck     : {pck_path}  ({res.size:,} 字节, {res.entry_count} 条目)")
    print(f"   exe     : {exe_path}  ({exe_path.stat().st_size:,} 字节, {exe_note})")
    print(f"   启动    : {exe_path.name} --headless --main-pack {pck_path.name} -- <参数>")
    return {
        "line": line,
        "pck": str(pck_path),
        "pck_sha256": res.sha256,
        "exe": str(exe_path),
        "entry_count": res.entry_count,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="构建兼容底座")
    ap.add_argument(
        "lines", nargs="*", default=["46"], help="引擎线编号（如 46）；默认只建内置线"
    )
    args = ap.parse_args(argv)

    cfg = cfg_mod.load()
    paths.ensure_dirs()
    wanted = args.lines or ["46"]
    for token in wanted:
        line = f"{token[0]}.{token[1:]}" if len(token) >= 2 else token
        build(line, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
