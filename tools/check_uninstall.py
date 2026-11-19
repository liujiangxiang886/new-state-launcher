"""自检：卸载模块的范围统计、路径越界保护、批处理生成。

最关键的一条是**越界保护**：绝不能删到安装目录之外。这里用假根目录验证。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import uninstall as um


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    # 用临时目录当「安装根」，造出各范围的真实结构
    from app import edition as _ed

    # 用当前产品名，而不是写死旧名 —— 改名后这里若不同步，
    # 自检会报「程序文件没识别到 exe」，实际是测试造的文件名不对。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fake_root"
        (root / "config").mkdir(parents=True)
        (root / "config" / "launcher.json").write_text("{}", encoding="utf-8")
        (root / "cache" / "instrumented").mkdir(parents=True)
        (root / "cache" / "instrumented" / "a.pck").write_bytes(b"x" * 1024)
        (root / "packs" / "mod1").mkdir(parents=True)
        (root / "packs" / "mod1" / "overlay.pck").write_bytes(b"y" * 2048)
        (root / "profiles" / "default").mkdir(parents=True)
        (root / _ed.exe_name()).write_bytes(b"e" * 512)

        stats = {s.key: s for s in um.scan(root)}
        results.append(("程序文件能识别 exe", stats["program"].bytes >= 512,
                        str(stats["program"].bytes)))
        results.append(("配置体积正确", stats["config"].bytes == 2, str(stats["config"].bytes)))
        results.append(("残留含缓存", stats["residue"].bytes == 1024, str(stats["residue"].bytes)))
        results.append(("扩展包体积正确", stats["packs"].bytes == 2048, str(stats["packs"].bytes)))
        results.append(("存档目录存在", stats["profiles"].exists, str(stats["profiles"].exists)))

        # 默认范围不应包含用户资产
        results.append(("默认不含扩展包", "packs" not in um.DEFAULT_SCOPES, ""))
        results.append(("默认不含存档", "profiles" not in um.DEFAULT_SCOPES, ""))

        # 越界保护：把一个安装目录外的路径混进 targets，remove_now 必须拒绝
        outside = Path(tmp) / "outside.txt"
        outside.write_text("keep me", encoding="utf-8")
        targets = um.plan(root, ["config"])
        targets.append(outside)
        ok, failures = um.remove_now(targets, root=root)
        results.append(("越界路径被拒绝", outside.exists() and any("跳过" in f for f in failures),
                        str(failures)))
        results.append(("界内路径被删除", not (root / "config").exists(), ""))

        # 批处理生成：只包含安装目录内的项
        (root / "runtime").mkdir()
        script = um.write_uninstall_script(root, um.plan(root, ["program"]))
        text = script.read_text(encoding="utf-8")
        results.append(("批处理含 rmdir", "rmdir /s /q" in text, ""))
        results.append(("批处理自删除", 'del /f /q "%~f0"' in text, ""))
        results.append(("批处理无目录外路径", "outside" not in text, ""))
        script.unlink()

        # human_size 基本正确
        results.append(("体积格式化", um.human_size(1024) == "1.0 KB", um.human_size(1024)))

    failed = 0
    for name, ok, detail in results:
        print(("[PASS] " if ok else "[FAIL] ") + name)
        if not ok:
            print("       ", detail)
            failed += 1
    print()
    print("全部通过" if not failed else f"失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())