#!/usr/bin/env python3
"""检查某条引擎线上每个已装扩展的「验证标志」状态。

**为什么需要这个工具**

4.7 固定完全校验（``strict``）。第一关是「有没有有效验证记录」：
包里没有 ``verification.json``，或指纹对不上，就**整包剔除** ——
连挂载都不会尝试。但界面上扩展照样显示「已启用」，日志里也只有一行
``verify.absent``，很难联想到根因。

这个脚本把状态直接打出来，是排障的第一步。

**自包含**：只用 Python 标准库，不依赖启动器源码，可以单独发给作者。

用法::

    python tools/check_verify.py 4.7
    python tools/check_verify.py 4.7 --packs "D:\\新起点启动器\\packs"

退出码：全部 verified 为 0，存在 stale/absent 为 1。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

#: 验证记录文件名（与启动器 `verify_mark.RECORD_NAME` 保持一致）
RECORD_NAME = "verification.json"

#: 已安装扩展的注册表候选
REGISTRY_CANDIDATES = ("extensions.json", "launcher.json")


# ---------------------------------------------------------------- 指纹计算


def code_digest_for(baseline_dir: Path, line: str) -> str:
    """从 ``baseline/identity-<line>.json`` 读代码层指纹。

    4.6 的 identity 没有 ``code_layer`` 字段，返回空串 —— 那种情况下
    ``strict`` 的验证判定会整体跳过（全部放行），不是错误。
    """
    for name in (f"identity-{line}.json", f"identity-v{line}.json"):
        path = baseline_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        layer = data.get("code_layer")
        if isinstance(layer, dict):
            return str(layer.get("digest") or "")
        return ""
    return ""


def content_digest(root: Path) -> str:
    """算扩展目录的内容指纹。

    规则与启动器保持一致：排序遍历所有文件，喂「相对路径 + 内容哈希」，
    **跳过 verification.json 自身**（否则写入记录会改变自己的指纹）。
    """
    if not root.is_dir():
        return ""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == RECORD_NAME:
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------- 判定


def read_record(root: Path) -> dict | None:
    path = root / RECORD_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("code_digest") or "") or not str(data.get("content_digest") or ""):
        return None
    return data


def judge(root: Path | None, want_code: str) -> tuple[str, str]:
    """返回 ``(状态, 说明)``。状态与启动器一致：verified / stale / absent。"""
    if not want_code:
        return ("verified", "该线没有代码层指纹，验证判定整体跳过（全部放行）")
    if root is None or not root.is_dir():
        return ("absent", f"找不到扩展目录：{root}")

    record = read_record(root)
    if record is None:
        return ("absent", "没有验证记录（没跑过 seal）")

    rec_code = str(record.get("code_digest") or "")

    # 包内记录只校 code_digest —— 与 ``verify_mark.judge`` 保持同一口径。
    #
    # 为什么不算 content_digest：seal 在**源 zip** 上算它（载荷是
    # ``mods/**`` 的脚本与数据），而这里读的是**安装后的**
    # ``packs/<id>/``（载荷已进 ``overlay-*.pck``，只剩清单和覆盖层）。
    # 文件集合不同，比了必然不相符，会把每个正常的包都误报成 stale。
    if rec_code == want_code:
        return ("verified", "验证记录与当前版本一致")

    return ("stale", "游戏代码层已更新，这条记录是按旧版本代码做的")


# ---------------------------------------------------------------- 主流程


def load_registry(cfg_dir: Path) -> list[dict]:
    """读已安装扩展列表。

    优先 ``extensions.json``（独立注册表），退回 ``launcher.json`` 里的
    ``extensions.installed``。
    """
    for name in REGISTRY_CANDIDATES:
        path = cfg_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if name == "extensions.json":
            # 启动器实际写的键是 ``items``（不是 installed）
            if isinstance(data, dict):
                items = data.get("items")
                if isinstance(items, list):
                    return [x for x in items if isinstance(x, dict)]
            elif isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        else:
            ext = data.get("extensions") if isinstance(data, dict) else None
            if isinstance(ext, dict):
                items = ext.get("installed")
                if isinstance(items, list):
                    return [x for x in items if isinstance(x, dict)]
    return []


def installed_for_line(items: list[dict], line: str) -> list[dict]:
    """挑出在该线上装过的条目。

    注册表有两种形态：新形态有 ``lines`` 字典（按线分别登记），
    老形态只有顶层 ``engine_line``。
    """
    out: list[dict] = []
    for item in items:
        if not item.get("enabled", True):
            continue
        lines = item.get("lines")
        if isinstance(lines, dict) and lines:
            picked = lines.get(line)
            if isinstance(picked, dict):
                merged = dict(item)
                merged.update(picked)
                merged["engine_line"] = line
                out.append(merged)
            continue
        if str(item.get("engine_line") or "") == line:
            out.append(item)
    return out


MARK = {"verified": "OK   ", "stale": "STALE", "absent": "MISS "}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="查扩展的验证标志状态")
    ap.add_argument("line", nargs="?", default="4.7", help="引擎线（默认 4.7）")
    ap.add_argument("--packs", default=None, help="packs 目录（默认 ./packs）")
    ap.add_argument("--config", default=None, help="config 目录（默认 ./config）")
    args = ap.parse_args(argv)

    base = Path(__file__).resolve().parent.parent
    packs_dir = Path(args.packs) if args.packs else base / "packs"
    cfg_dir = Path(args.config) if args.config else base / "config"
    baseline_dir = base / "baseline"

    line = args.line
    want_code = code_digest_for(baseline_dir, line)

    print(f"引擎线 {line} · packs={packs_dir}")
    if want_code:
        print(f"code_digest = {want_code[:32]}…")
    else:
        print("没有代码层指纹 -> 该线不做验证判定（全部放行）")
    print()

    if not packs_dir.is_dir():
        print(f"× packs 目录不存在：{packs_dir}")
        print("  用 --packs 指定启动器的 packs 目录，例如：")
        print(r'  python tools/check_verify.py 4.7 --packs "D:\某启动器\packs"')
        return 1

    items = load_registry(cfg_dir)
    if not items:
        print("（没读到注册表，改为扫描 packs 目录下的扩展）\n")
        entries = [{"id": p.name} for p in sorted(packs_dir.iterdir()) if p.is_dir()]
    else:
        entries = installed_for_line(items, line)

    if not entries:
        print(f"{line} 线没有已启用的扩展")
        return 0

    bad = 0
    for item in entries:
        ext_id = str(item.get("id") or "")
        root = packs_dir / ext_id
        state, reason = judge(root, want_code)
        if state != "verified":
            bad += 1
        print(f"  [{MARK.get(state, state)}] {ext_id:34} {reason}")

    print()
    if bad:
        print(f"{bad} 个包在 {line} 线上会被剔除。处理：")
        print("  让作者重出包 -> python tools/jjm_pack.py seal <包> --line " + line)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())