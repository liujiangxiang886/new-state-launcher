"""S1：pack_version 的兼容边界与写侧约束。

**保留下来的结论**（实测，用真实引擎跑出来的）：

    引擎 4.6（唯一内置线）× 包 v3 → 可以加载
    引擎 4.6（唯一内置线）× 包 v4 → 拒绝加载（Pack version unsupported: 4）

所以真实规则是：**引擎能读 `<=` 自身的版本**。v4 是更高版本引擎才有的目录
布局，内置线读不了；反过来把老包塞给新引擎没问题。

1.4 起只剩内置线一条，所以：

- **写侧**只产出 v3（``PACK_VERSION_BY_ENGINE`` 只有 4.6）；
- **读侧**仍认 v3 / v4（玩家自定义改版可能就是 v4 的包，读不了等于导入即失败）。

本文件固定这三件事，防止未来有人「优化」成严格等值，或顺手把 v4 读取也删掉。

验证方式刻意**不经过我们的底座**——底座里有 8 个游戏 Autoload，会把探针包的
工程配置顶掉，测出来的是 Autoload 缺失而不是版本问题。用编辑器构建显式
传 ``--main-pack`` 直接喂探针包。

    python tests/test_pack_version.py
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod, runner  # noqa: E402
from app.core.pck import PckReader  # noqa: E402
from app.core.pck import format as pfmt  # noqa: E402
from app.core.pck.writer import PckWriter  # noqa: E402

PASS = 0
FAIL = 0

PROJECT = """config_version=5

[application]

config/name="s1 probe"
run/main_scene="res://main.tscn"
config/features=PackedStringArray("{line}", "GL Compatibility")

[rendering]

renderer/rendering_method="gl_compatibility"
"""

SCENE = """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://main.gd" id="1"]

[node name="Probe" type="Node"]
script = ExtResource("1")
"""

SCRIPT = (
    "extends Node\n"
    "\n"
    "func _ready() -> void:\n"
    '\tprint("PROBE_OK " + str(Engine.get_version_info()["string"]))\n'
    "\tget_tree().quit(0)\n"
)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def make_pack(line: str, out: Path) -> tuple[Path, int]:
    writer = PckWriter(out, line=line)
    writer.add_text("project.godot", PROJECT.format(line=line))
    writer.add_text("main.tscn", SCENE)
    writer.add_text("main.gd", SCRIPT)
    result = writer.commit()
    version = int.from_bytes(out.read_bytes()[4:8], "little")
    print(f"    生成 {out.name}：line={line} pack_version={version} {result.entry_count} 条目")
    return out, version


def retag_as_v4(src: Path, dest: Path) -> Path:
    """把 v3 包的头部版本号改成 4，得到一个「声称是 v4」的包。

    为什么不直接用 ``PckWriter`` 写 v4：写侧已经不允许非内置线了。
    这里只改头部 4 个字节——引擎判断「接不接受这个包」看的就是头部版本号，
    目录区布局在版本号对不上时会直接被拒，走不到解析那一步。
    """
    data = bytearray(src.read_bytes())
    struct.pack_into("<I", data, 4, 4)
    dest.write_bytes(bytes(data))
    return dest


def run_engine(engine_line: str, pack: Path, work: Path, tag: str) -> tuple[bool, str]:
    case = work / tag
    case.mkdir(parents=True, exist_ok=True)
    exe_src, _ = runner.host_paths(engine_line)

    exe = case / "Godot.exe"
    shutil.copyfile(exe_src, exe)
    target = case / "probe.pck"
    shutil.copyfile(pack, target)
    cmd = [str(exe), "--headless", "--main-pack", str(target), "--quit-after", "600"]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(case),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "超时（120s）"

    out = proc.stdout or ""
    err = proc.stderr or ""
    if "PROBE_OK" in out:
        return True, out.strip().splitlines()[-1]
    detail = ""
    for line in (err or out).splitlines():
        low = line.lower()
        if "pack version" in low or "corrupt" in low or "not found" in low:
            detail = line.strip()
            break
    return False, detail or f"rc={proc.returncode} 无 PROBE_OK"


def _write_packset(dest: Path, line: str, pack: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "schema": 1,
                "line": line,
                "packs": [{"path": pack.as_posix(), "offset": 0, "pack_version": 4}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return dest


def main() -> int:
    work = ROOT / "builds" / "s1-version"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    line = cfg_mod.BUILTIN_LINE

    print("=== 写侧：只产出内置线的 pack_version ===")
    print(f"  PACK_VERSION_BY_ENGINE = {pfmt.PACK_VERSION_BY_ENGINE}")
    check(
        "写侧只登记内置线",
        set(pfmt.PACK_VERSION_BY_ENGINE) == {line},
        f"实际 {sorted(pfmt.PACK_VERSION_BY_ENGINE)}",
    )
    check(
        "读侧仍接受 v3 / v4",
        set(pfmt.SUPPORTED_PACK_VERSIONS) == {3, 4},
        f"实际 {pfmt.SUPPORTED_PACK_VERSIONS}",
    )
    check(
        "内置线写 v3",
        pfmt.pack_version_for(line) == 3,
        f"实际 {pfmt.pack_version_for(line)}",
    )

    print()
    print("=== 生成探针包 ===")
    v3_pack, v3_version = make_pack(line, work / "probe-v3.pck")
    check("产出的包是 v3", v3_version == 3, f"实际 v{v3_version}")

    v4_pack = retag_as_v4(v3_pack, work / "probe-v4.pck")

    print()
    print("=== 读侧：v3 / v4 都能被 PckReader 打开 ===")
    for tag, pack in (("v3", v3_pack), ("v4", v4_pack)):
        try:
            reader = PckReader.open(pack)
            got = int(reader.header.pack_version)
            check(f"{tag} 包可读取", got == int(tag[1:]), f"读到的版本 {got}")
        except Exception as exc:  # noqa: BLE001 - 读不出来就是失败
            check(f"{tag} 包可读取", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 引擎实地验证：内置线能读 v3，但拒绝 v4 ===")
    matrix: list[dict] = []

    ok3, detail3 = run_engine(line, v3_pack, work, f"e{line.replace('.', '')}-p3")
    print(f"  引擎 {line} × 包 v3：{'可以加载' if ok3 else '拒绝加载'} | {detail3}")
    check(f"引擎 {line} × 包 v3 -> 可以加载", ok3, detail3)
    matrix.append(
        {"engine": line, "pack_line": line, "pack_version": 3,
         "loaded": ok3, "detail": detail3}
    )

    ok4, detail4 = run_engine(line, v4_pack, work, f"e{line.replace('.', '')}-p4")
    print(f"  引擎 {line} × 包 v4：{'可以加载' if ok4 else '拒绝加载'} | {detail4}")
    check(f"引擎 {line} × 包 v4 -> 拒绝加载", not ok4, detail4)
    matrix.append(
        {"engine": line, "pack_line": line, "pack_version": 4,
         "loaded": ok4, "detail": detail4}
    )

    conflict = runner.check_packset_versions(
        cfg_mod.load(),
        line,
        _write_packset(work / "ps-conflict.json", line, v4_pack),
    )
    check("启动前拦下 v4 分片（不等玩家看到「游戏起不来」）", bool(conflict), str(conflict))

    print()
    print("=== 结论 ===")
    if ok3 and not ok4:
        print(f"  向下兼容成立：{line} 能读 v3，v4 会被拒绝（Pack version unsupported: 4）")
    else:
        print("  与当前记录不符，请检查引擎是否换了构建")

    (ROOT / "logs" / "s1-report.json").write_text(
        json.dumps(
            {
                "matrix": matrix,
                "rule": "backwards_compatible" if (ok3 and not ok4) else "unknown",
                "note": f"{line} 读 v3；拒绝 v4（Pack version unsupported: 4）",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("=== packset 记录的 pack_version ===")
    rd = work / "packset-probe"
    packset_path = runner.make_packset(
        rd / "packset.json",
        packset_id="s1-packset",
        line=line,
        packs=[
            {"path": v3_pack.as_posix(), "offset": 0},
            {"path": v4_pack.as_posix(), "offset": 0},
        ],
        baseline_id="s1",
    )
    data = json.loads(packset_path.read_text(encoding="utf-8"))
    versions = [p.get("pack_version") for p in data.get("packs", [])]
    check("逐片记录 pack_version", versions == [3, 4], f"实际 {versions}")
    check("整体取最大值", data.get("pack_version") == 4, f"实际 {data.get('pack_version')}")

    print()
    print(f"=== 合计：{PASS} 通过 / {FAIL} 失败 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())