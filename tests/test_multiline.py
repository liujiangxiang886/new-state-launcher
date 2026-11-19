"""多线安装与跨版本兼容性的单测。

1.4 起内置线只剩一条，但「覆盖层按线分开存储」这套机制仍然要成立：
自定义线与老玩家升级后的历史数据都会用到它。所以这里用 ``OTHER`` 作为
第二条线的替身，验证两条线互不干扰。

覆盖：
1. 覆盖层按线分开存储（``overlay-46.pck`` / ``overlay-45.pck``）
2. 注册表的 ``lines`` 映射与旧格式兼容
3. ``enabled_mods`` 按目标线裁剪
4. 兼容性探测的结论判定
5. ``allow_partial`` 下跳过失效补丁
6. 只靠补丁的扩展全部失效时报错（而不是「空包」）
7. ``resolve_source`` 能从合集包里取回成员
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext import compat, patcher, symcheck  # noqa: E402
from app.core.ext.service import (  # noqa: E402
    detect_bundle,
    install_bundle,
    resolve_source,
)

results: list[tuple[str, bool, str]] = []


#: 「另一条线」的替身：验证多线机制仍然成立，同时不引入已下线的线号。
OTHER = "4.5"


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ------------------------------------------------------------ 1 覆盖层按线分开
def test_overlay_per_line(tmp: Path) -> None:
    from app.core import config as cfg_mod
    from app.core.ext.service import ExtensionService

    cfg = cfg_mod.load()
    svc = ExtensionService(cfg)

    # 造两个最小扩展包（只带一个新增文件，无补丁）
    def make(tag: str, target: str) -> Path:
        root = tmp / f"src-{tag}"
        (root / "payload").mkdir(parents=True, exist_ok=True)
        (root / "payload" / "x.txt").write_text(tag, encoding="utf-8")
        manifest = {
            "schema": 1,
            "id": f"test.perline.{tag}",
            "name": f"按线测试 {tag}",
            "version": "1.0.0",
            "baseline": "jjxf.v1.45.original",
            "files": [{"source": "payload/x.txt", "target": target}],
        }
        (root / "extension.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        out = tmp / f"{tag}.zip"
        with zipfile.ZipFile(out, "w") as z:
            for f in root.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(root).as_posix())
        return out

    pkg = make("multi", "mods/test_perline_multi/x.txt")
    res = svc.install(pkg, None, verify_smoke=False, target_line=OTHER)
    check("能装到指定线", res.ok, res.message[:80])
    check("engine_line 记录正确", res.engine_line == OTHER, res.engine_line)

    entry = svc.get("test.perline.multi") or {}
    lines = entry.get("lines") or {}
    check("注册表有 lines 映射", OTHER in lines, str(list(lines)))
    overlay = Path(str(lines.get(OTHER, {}).get("overlay", "")))
    check("覆盖层文件名带线", overlay.name == "overlay-47.pck", overlay.name)
    check("覆盖层文件存在", overlay.exists())

    # 再装一次到 4.6 → 两个覆盖层并存
    res2 = svc.install(pkg, None, verify_smoke=False, target_line="4.6")
    check("能再装到另一条线", res2.ok, res2.message[:60])
    entry = svc.get("test.perline.multi") or {}
    lines = entry.get("lines") or {}
    check("两条线都在", set(lines) == {"4.6", OTHER}, str(list(lines)))
    o46 = Path(str(lines.get("4.6", {}).get("overlay", "")))
    check("4.6 覆盖层独立存在", o46.exists() and o46.name == "overlay-46.pck", o46.name)

    svc.uninstall("test.perline.multi")


# ------------------------------------------------------------ 2 旧格式兼容
def test_legacy_registry(tmp: Path) -> None:
    from app.core.ext.enable import enabled_mods

    cfg = {"profiles": {"default": "default", "redirect_appdata": False}}
    # 直接构造一个旧格式条目
    old = {
        "id": "legacy.one",
        "name": "旧格式",
        "version": "1.0.0",
        "engine_line": "4.6",
        "overlay": "",
        "enabled": True,
        "runtime_hooks": [],
    }
    from app.core.ext.service import ExtensionService

    svc = ExtensionService(cfg)
    items = svc.installed()
    svc._save_registry(items + [old])
    try:
        got = enabled_mods(cfg, "4.6")
        check("旧格式条目能被识别", any(i["id"] == "legacy.one" for i in got))
        got_other = enabled_mods(cfg, OTHER)
        check("旧格式条目不串到别的线", not any(i["id"] == "legacy.one" for i in got_other))
    finally:
        svc._save_registry(items)


# ------------------------------------------------------------ 3 enabled_mods 裁剪
def test_enabled_mods_slice(tmp: Path) -> None:
    from app.core.ext.enable import enabled_mods
    from app.core.ext.service import ExtensionService

    cfg = {"profiles": {"default": "default", "redirect_appdata": False}}
    svc = ExtensionService(cfg)
    items = svc.installed()
    entry = {
        "id": "slice.one",
        "name": "裁剪测试",
        "version": "1.0.0",
        "engine_line": OTHER,
        "enabled": True,
        "runtime_hooks": [],
        "lines": {
            "4.6": {"line": "4.6", "overlay": "a.pck", "runtime_hooks": [{"script": "s.gd", "method": "m", "phase": "before"}]},
            OTHER: {"line": OTHER, "overlay": "b.pck", "runtime_hooks": [{"script": "t.gd", "method": "n", "phase": "after"}]},
        },
    }
    svc._save_registry(items + [entry])
    try:
        got46 = enabled_mods(cfg, "4.6")
        one = next((i for i in got46 if i["id"] == "slice.one"), None)
        check("按线取到条目", one is not None)
        if one:
            check("覆盖层取的是该线的", one.get("overlay") == "a.pck", str(one.get("overlay")))
            check("钩子取的是该线的", one["runtime_hooks"][0]["script"] == "s.gd")
            check("engine_line 被改写为该线", one.get("engine_line") == "4.6")
        got_other = enabled_mods(cfg, OTHER)
        two = next((i for i in got_other if i["id"] == "slice.one"), None)
        check("另一条线取到自己的", two and two.get("overlay") == "b.pck")
    finally:
        svc._save_registry(items)


# ------------------------------------------------------------ 4 兼容性判定
def test_compat_verdict(tmp: Path) -> None:
    r = compat.ProbeResult()
    r.patch_total = 0
    check("无补丁 → 兼容", r.verdict == compat.COMPATIBLE)

    r = compat.ProbeResult()
    r.patch_total = 3
    r.patch_ok = 3
    check("补丁全中 → 兼容", r.verdict == compat.COMPATIBLE)

    r = compat.ProbeResult()
    r.patch_total = 3
    r.patch_ok = 1
    r.failures = ["a", "b"]
    check("部分命中 → 部分兼容", r.verdict == compat.PARTIAL)

    r = compat.ProbeResult()
    r.patch_total = 3
    r.patch_ok = 0
    r.failures = ["a"]
    check("全不中 → 不兼容", r.verdict == compat.INCOMPATIBLE)

    r = compat.ProbeResult()
    r.error = "x"
    check("出错 → 待查", r.verdict == compat.UNKNOWN)


# ------------------------------------------------------------ 5 allow_partial
def test_allow_partial(tmp: Path) -> None:
    src = 'hp=100\nhp2=120\n'
    # find 指向原文里**不存在**的内容 → 命中 0 处，期望 1 处 → 严格模式应抛
    patch = [{"find": "hp3=999", "replace": "hp3=1", "expect_count": 1}]

    # 严格：命中数不符 → 抛
    try:
        patcher.patch_bytes("t.gd", src.encode(), patch, [], allow_partial=False)
        check("严格模式命中数不符时抛错", False, "没抛")
    except patcher.PatchError:
        check("严格模式命中数不符时抛错", True)

    # 宽松：跳过，继续
    out, reports = patcher.patch_bytes("t.gd", src.encode(), patch, [], allow_partial=True)
    check("宽松模式不抛错", True)
    check("宽松模式未改动内容", out.decode() == src, repr(out.decode()))
    check("宽松模式记录了问题", bool(reports[0].issues), str(reports[0].issues))


# ------------------------------------------------------------ 6 只靠补丁的扩展
def test_patch_only_all_fail(tmp: Path) -> None:
    from app.core import config as cfg_mod
    from app.core.ext.service import ExtensionService

    cfg = cfg_mod.load()
    svc = ExtensionService(cfg)

    root = tmp / "patchonly"
    (root / "payload").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1,
        "id": "test.patchonly",
        "name": "只靠补丁",
        "version": "1.0.0",
        "baseline": "jjxf.v1.45.original",
        "text_patches": [
            {
                "target": "scripts/game.gd",
                "find": "这段文字在原版里绝对不存在_zzz",
                "replace": "x",
                "expect_count": 1,
            }
        ],
    }
    (root / "extension.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    pkg = tmp / "patchonly.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        for f in root.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())

    res = svc.install(pkg, None, verify_smoke=False, target_line=OTHER, allow_partial=True)
    check("补丁全失效时报错", not res.ok, res.message[:60])
    check("错误信息说明了原因", "补丁" in res.message and "没命中" in res.message, res.message[:100])
    svc.uninstall("test.patchonly")


# ------------------------------------------------------------ 7 resolve_source
def test_resolve_source(tmp: Path) -> None:
    inner = tmp / "inner"
    (inner / "payload").mkdir(parents=True, exist_ok=True)
    (inner / "payload" / "a.txt").write_text("x", encoding="utf-8")
    (inner / "extension.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": "test.member",
                "name": "成员",
                "version": "1.0.0",
                "files": [{"source": "payload/a.txt", "target": "mods/test_member/a.txt"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    member = tmp / "member.zip"
    with zipfile.ZipFile(member, "w") as z:
        for f in inner.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(inner).as_posix())

    bundle = tmp / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.write(member, "mod/member.zip")

    check("能识别合集包", len(detect_bundle(bundle)) == 1, str(detect_bundle(bundle)))

    out = tmp / "out"
    out.mkdir(exist_ok=True)
    res = install_bundle({"profiles": {"default": "default", "redirect_appdata": False}}, bundle, verify_smoke=False)
    check("合集安装成功", res.ok, res.message)

    entry = (res.items and {}) or {}
    from app.core.ext.service import ExtensionService

    # 用注册表里的 source_ref 找回成员
    svc = ExtensionService({"profiles": {"default": "default", "redirect_appdata": False}})
    got = svc.get("test.member") or {}
    ref = got.get("source_ref") or {}
    check("记录了 source_ref", bool(ref.get("member")), str(ref))

    found = resolve_source(got, out)
    check("能取回成员包", found is not None and found.is_file(), str(found))
    if found:
        with zipfile.ZipFile(found) as z:
            check("取回的包内容正确", "extension.json" in z.namelist())


# ------------------------------------------------------------ 8 符号检查
def test_symcheck() -> None:
    original = 'extends Node\n\nconst MY_CONST = 1\n'
    patched = 'extends Node\n\nconst MY_CONST = 1\n\nfunc f():\n\tprint(MY_CONST)\n'
    chk = symcheck.check_patched_script("t.gd", original, patched, [])
    check("已声明的常量不报", chk.ok, str(chk.broken))

    patched2 = 'extends Node\n\nfunc f():\n\tprint(MISSING_ONE)\n'
    chk2 = symcheck.check_patched_script("t.gd", original, patched2, [])
    check("未声明的常量被报出", not chk2.ok and "MISSING_ONE" in chk2.broken, str(chk2.broken))

    # 枚举成员不算
    patched3 = 'extends Node\n\nfunc f():\n\tprint(Color.WHITE)\n'
    chk3 = symcheck.check_patched_script("t.gd", original, patched3, [])
    check("枚举成员不误报", chk3.ok, str(chk3.broken))

    # 字符串内容不算
    patched4 = 'extends Node\n\nfunc f():\n\tprint("SOME_TEXT")\n'
    chk4 = symcheck.check_patched_script("t.gd", original, patched4, [])
    check("字符串内容不误报", chk4.ok, str(chk4.broken))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="multiline-test-"))
    try:
        print("=== 覆盖层按线分开 ===")
        test_overlay_per_line(tmp)
        print("\n=== 旧格式兼容 ===")
        test_legacy_registry(tmp)
        print("\n=== enabled_mods 裁剪 ===")
        test_enabled_mods_slice(tmp)
        print("\n=== 兼容性判定 ===")
        test_compat_verdict(tmp)
        print("\n=== allow_partial ===")
        test_allow_partial(tmp)
        print("\n=== 只靠补丁的扩展 ===")
        test_patch_only_all_fail(tmp)
        print("\n=== resolve_source ===")
        test_resolve_source(tmp)
        print("\n=== 符号检查 ===")
        test_symcheck()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n通过 {passed}，失败 {len(results) - passed}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
