"""命令行入口。UI 未就绪时也能完整驱动内核。

用法::

    python -m app.cli stat                      # 环境与基线体检
    python -m app.cli base 46 47                # 构建兼容底座
    python -m app.cli smoke 4.6                 # 冒烟：挂载基线并实例化主场景
    python -m app.cli smoke 4.6                 # 同上，走 1.45
    python -m app.cli run 4.6                   # 正常带窗口启动
    python -m app.cli packset 4.6 -o out.json   # 只生成 packset
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod, paths, runner  # noqa: E402
from app.core.errors import LauncherError  # noqa: E402
from app.core.hashing import sha256_file  # noqa: E402
from app.core.pck import PckReader  # noqa: E402

DEFAULT_BASELINE = dict(cfg_mod.BASELINE_OF)


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_stat(args: argparse.Namespace) -> int:
    cfg = cfg_mod.load()
    info = {
        "root": str(paths.ROOT),
        "config": str(cfg_mod.CONFIG_PATH),
        "engines": {},
        "baselines": {},
    }
    for line, meta in cfg["engines"].items():
        # 配置里可能是包内相对路径（打包分发时按 paths.ROOT 解析），
        # 直接用 Path(raw) 会按当前工作目录解析，换个目录启动就误报「不存在」。
        p = cfg_mod.resolve_path(meta.get("path") or "") or Path("")
        info["engines"][line] = {
            "path": str(p),
            "exists": p.exists(),
            "size": p.stat().st_size if p.exists() else None,
        }
    for bid, meta in cfg["baselines"].items():
        p = cfg_mod.resolve_path(meta["path"]) or Path("")
        exists = p.exists()
        size = p.stat().st_size if exists else None
        suffix = bid.split(".v")[-1]
        idx = paths.BASELINE / f"index-{suffix}.json.gz"
        ident = paths.BASELINE / f"identity-{suffix}.json"
        info["baselines"][bid] = {
            "path": str(p),
            "exists": exists,
            "size": size,
            "size_matches": (size == meta.get("size")) if exists else None,
            "index": idx.name if idx.exists() else None,
            "identity": ident.name if ident.exists() else None,
        }
    _print(info)
    return 0


def cmd_memtrim(args: argparse.Namespace) -> int:
    """单独试跑「启动前释放系统内存」，不用启动游戏。

    为什么要有这个命令：这个功能会动**全系统**进程的工作集，直接在游戏
    启动流程里试错代价太大（一次卡顿玩家就以为启动器坏了）。先在这里
    空跑一遍看清单，再真跑一遍看数字，最后才接进界面。
    """
    from .core import memtrim

    if not args.apply:
        report = memtrim.trim_working_sets(dry_run=True)
        print(f"[memtrim] 试运行：将处理 {report.processes} 个进程，"
              f"跳过 {report.skipped} 个")
        for name, size in report.tops:
            print(f"  - {name:<28} {memtrim.human(size)}")
        print("[memtrim] 加 --apply 才会真的执行")
        return 0

    report = memtrim.trim_working_sets(exclude_pids=set(args.exclude_pid or ()))
    print(f"[memtrim] {report.summary()}")
    for name, size in report.tops:
        print(f"  - {name:<28} 释放 {memtrim.human(size)}")
    return 0 if report.ok else 1


def cmd_base(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(paths.TOOLS / "build_base.py"), *args.lines]
    return subprocess.call(cmd)


def cmd_index(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(paths.TOOLS / "make_index.py"), *args.ids]
    return subprocess.call(cmd)


def _packset_for(
    cfg: dict, line: str, baseline_id: str | None, out: Path, with_mods: bool = False
) -> tuple[Path, str, Path | None]:
    """生成 packset；``with_mods`` 时把已启用扩展的插桩层与覆盖层一起排进去。"""
    bid = baseline_id or DEFAULT_BASELINE[line]
    packs = runner.baseline_packs(cfg, bid)
    mods_json: Path | None = None
    if with_mods:
        from app.core.ext import enable as ext_enable

        mods = ext_enable.build_for_launch(cfg, line, out.parent)
        packs.extend(mods.packs)
        mods_json = mods.mods_json
        for warning in mods.warnings:
            print(f"[扩展警告] {warning}")
        order = [e.get("id") for e in (mods.lock.get("resolution_order") or [])]
        if order:
            print(f"[扩展] 加载顺序：{' → '.join(str(x) for x in order)}（lock.json 已写）")
        if mods.mods:
            print(f"[扩展] 启用 {len(mods.mods)} 个：{', '.join(str(m.get('id')) for m in mods.mods)}，"
                  f"钩子 {len(mods.hooks)} 个")
    packset = runner.make_packset(
        out,
        packset_id=f"{bid}-{line}" + ("-mods" if with_mods else ""),
        line=line,
        packs=packs,
        baseline_id=bid,
    )
    return packset, bid, mods_json


def cmd_packset(args: argparse.Namespace) -> int:
    cfg = cfg_mod.load()
    out = Path(args.out) if args.out else (paths.BUILDS / "packset-preview.json")
    if not out.is_absolute():
        out = paths.ROOT / out
    packset, bid, _ = _packset_for(cfg, args.line, args.baseline, out)
    _print({"packset": str(packset), "baseline": bid, "content": json.loads(packset.read_text("utf-8"))})
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    cfg = cfg_mod.load()
    rd = runner.run_dir_for(f"smoke{args.line.replace('.', '')}")
    packset, bid, mods_json = _packset_for(
        cfg, args.line, args.baseline, rd / "packset.json", args.with_mods
    )
    res = runner.launch(
        cfg,
        args.line,
        packset=packset,
        main_scene=args.main,
        smoke=True,
        headless=True,
        profile=args.profile,
        run_dir=rd,
        timeout=args.timeout,
        mods_path=mods_json,
    )
    _print({"baseline": bid, **res.summary()})
    if res.stdout.strip():
        print("--- stdout ---")
        print(res.stdout.strip()[-4000:])
    if res.stderr.strip():
        print("--- stderr ---")
        print(res.stderr.strip()[-4000:])
    return 0 if res.ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    cfg = cfg_mod.load()
    rd = runner.run_dir_for(f"run{args.line.replace('.', '')}")
    packset, bid, mods_json = _packset_for(
        cfg, args.line, args.baseline, rd / "packset.json", args.with_mods
    )
    res = runner.launch(
        cfg,
        args.line,
        packset=packset,
        main_scene=args.main,
        smoke=False,
        headless=False,
        profile=args.profile,
        run_dir=rd,
        timeout=args.timeout,
        mods_path=mods_json,
    )
    _print({"baseline": bid, **res.summary()})
    return 0 if res.ok else 1


def cmd_ext(args: argparse.Namespace) -> int:
    """检查一个扩展包：升格 schema、校验文件、把钩子对到真实源码上。

    合集包（自身没有 extension.json、内层是多个扩展 zip）会被展开，逐个给报告。
    """
    from app.core import baseline as baseline_mod
    from app.core.ext import manifest as ext_manifest
    from app.core.ext.service import detect_bundle

    if not args.install:
        members = detect_bundle(args.package)
        if members:
            return _ext_bundle_report(args, members)

    if args.install:
        return _ext_install(args)

    cfg = cfg_mod.load()
    probe = ext_manifest.load(args.package)          # 先读一次，只为拿基线线别
    line = probe.engine_line or "4.6"

    src_root: Path | None = None
    if args.check_hooks:
        src_meta = (cfg.get("runtime_sources") or {}).get(line)
        if src_meta:
            src_root = Path(src_meta["project_godot"]).parent
        else:
            print(f"[警告] 没有 {line} 的源码根，跳过钩子核对")

    paths_set = None
    try:
        paths_set = baseline_mod.path_set(line)
    except FileNotFoundError as exc:
        print(f"[警告] {exc}")

    m = ext_manifest.load(args.package, src_root, paths_set)
    _print(m.summary())
    if args.verbose:
        print("--- files ---")
        for rec in m.files:
            mark = "覆盖" if (paths_set and rec["target"] in paths_set) else "新增"
            print(f"  [{mark}] {rec['source']}  ->  {rec['target']}")
        print("--- method_hooks ---")
        for h in m.method_hooks:
            print(f"  {h['target']}::{h['method']}  phase={h['phase']} prio={h['priority']}")
    return 0 if m.ok else 1


def _ext_install(args: argparse.Namespace) -> int:
    """安装一个扩展包；合集包会逐个安装。"""
    from app.core.ext.service import ExtensionService, detect_bundle, install_bundle

    cfg = cfg_mod.load()

    members = detect_bundle(args.package)
    if members:
        print(f"检测到合集包，内含 {len(members)} 个扩展，开始逐个安装…\n")
        result = install_bundle(
            cfg,
            args.package,
            progress=lambda p, label: print(f"  [{p:3d}%] {label}"),
            verify_smoke=args.verify,
            target_line=args.line,
            allow_partial=args.allow_partial,
            on_item=lambda item: print(
                f"  {'OK  ' if item.ok else 'FAIL'} {item.name}  {item.message}"
            ),
        )
        print()
        print(f"合集汇总：成功 {result.succeeded} / {result.total}，失败 {result.failed}")
        return 0 if result.ok else 1

    svc = ExtensionService(cfg)
    res = svc.install(
        args.package,
        lambda p, label: print(f"  [{p:3d}%] {label}"),
        verify_smoke=args.verify,
        target_line=args.line,
        allow_partial=args.allow_partial,
    )
    print()
    print(res.message if res.ok else f"安装失败：{res.message}")
    if res.ok:
        print(f"  id={res.ext_id}  引擎线={res.engine_line}  条目={res.entry_count}  状态={res.status}")
    return 0 if res.ok else 1


def _ext_bundle_report(args: argparse.Namespace, members: list[str]) -> int:
    """把合集包拆开，逐个跑一遍扩展检查并汇总。"""
    import tempfile
    import zipfile

    print(f"检测到合集包，内含 {len(members)} 个扩展：")
    for name in members:
        print(f"  - {name}")
    print("\n逐个检查：")

    failed = 0
    outer = zipfile.ZipFile(args.package)
    try:
        with tempfile.TemporaryDirectory(dir=str(paths.BUILDS)) as tmp:
            for index, name in enumerate(members, start=1):
                member = Path(tmp) / f"{index:02d}.zip"
                member.write_bytes(outer.read(name))
                print(f"\n[{index}/{len(members)}] {Path(name).name}")
                sub = argparse.Namespace(**{**vars(args), "package": str(member)})
                if cmd_ext(sub) != 0:
                    failed += 1
    finally:
        outer.close()

    print(f"\n合集汇总：{len(members)} 个，失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_ext_migrate(args: argparse.Namespace) -> int:
    """把「同一扩展装了多条线」收敛成一条线（1.4 跨线硬拒绝的配套迁移）。"""
    from app.core.ext import migrate as mig

    cfg = cfg_mod.load()
    plan = mig.migrate_single_line(cfg, dry_run=bool(getattr(args, "dry_run", False)))
    print(plan.message())
    for row in plan.items:
        if row.action == "keep":
            continue
        lines = ",".join(row.drop_lines) or "-"
        print(f"  [{row.action}] {row.id}  声明={row.declared}  删除线={lines}")
        for path in row.files:
            print(f"      - {path}")
    if plan.backup:
        print(f"备份：{plan.backup}")
    for error in plan.errors:
        print(f"[错误] {error}")
    if getattr(args, "dry_run", False):
        print("（dry-run：只打印计划，没有改动磁盘）")
    return 0 if not plan.errors else 1


def cmd_ext_audit(args: argparse.Namespace) -> int:
    """体检：扫已装扩展的跨扩展访问、钩子点撞车、声明与实现不符。

    这是「完全校验扩展模式」的**前置**：先把问题查出来给人看，
    补完声明再开 strict —— 而不是直接拒装让人一头雾水。

    输出三部分：
    1. 违规清单（每个扩展一行，列 L0 编号 + 文件 + 行号）
    2. 可直接粘进 ``extension.json`` 的 ``peer_access`` 草稿（--patch）
    3. 钩子点撞车表（谁挂在哪、实际执行序如何）
    """
    import json as _json

    import app.core.paths as paths
    from app.core.ext import isolation as iso
    from app.core.ext.service import ExtensionService

    cfg = cfg_mod.load()
    svc = ExtensionService(cfg)
    line = getattr(args, "line", cfg_mod.BUILTIN_LINE)
    tag = line.replace(".", "")

    mode = cfg_mod.extension_mode(cfg)
    extended_ctx = bool((cfg.get("extensions") or {}).get("hook_ctx_extended", True))

    # 扫每个已装扩展的解包源码
    per_ext: dict[str, list[iso.PeerFinding]] = {}
    runtime_hooks_by_ext: dict[str, list[dict]] = {}
    src_of: dict[str, "object"] = {}
    scanned = 0
    for item in svc.installed():
        ext_id = str(item.get("id") or "")
        if not ext_id:
            continue
        src = paths.BUILDS / f"_install_{ext_id}_{tag}" / "src"
        files = iso.load_src_files(src)
        if files:
            scanned += 1
            per_ext[ext_id] = iso.scan_tree(files, ext_id, extended_ctx=extended_ctx)
        if src.is_dir():
            src_of[ext_id] = src
        lines_meta = item.get("lines") or {}
        entry = lines_meta.get(line) if isinstance(lines_meta, dict) else None
        if isinstance(entry, dict):
            runtime_hooks_by_ext[ext_id] = entry.get("runtime_hooks") or []

    # 钩子点撞车（跨扩展）—— 用**真实注册的相位**，不用插桩表。
    #
    # 插桩层对缺 runtime_hooks 的老记录做 before+after 双插（保证运行期注册
    # 命中）；拿那份表判撞车会把「只注册 after」的包算成也注册了 before，
    # 和别人的 before 假撞车。手工核对过：17 包里 15 包缺 runtime_hooks。
    from .core.ext import hook_scan as scan_mod

    hooks_by_ext: dict[str, list[dict]] = {}
    for ext_id in src_of.keys() | runtime_hooks_by_ext.keys():
        real = runtime_hooks_by_ext.get(ext_id) or []
        if real:
            hooks_by_ext[ext_id] = real
            continue
        root = src_of.get(ext_id)
        if root is None:
            continue
        try:
            got = scan_mod.scan_package(root)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - 反查失败就跳过该包
            continue
        hooks_by_ext[ext_id] = [
            {"script": s.script, "method": s.method, "phase": s.phase} for s in got.sites
        ]
    # 键级判定：同点不等于同键。
    #
    # 面板上那句「详情：xxx cli ext-audit」得兑现 —— 只报「两个包注册了同一点」
    # 等于把判断推回给人。这里先算清每个共享点「谁写哪些键、是不是读—改—写」，
    # 再交给撞车检测：键不重叠 / 双方都是读—改—写 → 直接不报。
    from .core.ext import hook_keys as keys_mod

    writes_by_point: dict[tuple[str, str, str], list] = {}
    point_owners: dict[tuple[str, str, str], set[str]] = {}
    for ext_id, hooks in hooks_by_ext.items():
        for h in hooks:
            key = (
                str(h.get("script", "")),
                str(h.get("method", "")),
                str(h.get("phase", "")),
            )
            if all(key):
                point_owners.setdefault(key, set()).add(ext_id)
    for key, owners in point_owners.items():
        if len(owners) < 2:
            continue
        try:
            analysis = keys_mod.analyze_point(src_of, *key, sorted(owners))
        except Exception:  # noqa: BLE001 - 判定失败退回「需人工确认」
            continue
        writes_by_point[key] = analysis.infos

    collisions = iso.detect_hook_collisions(hooks_by_ext, writes_by_point)
    for c in collisions:
        per_ext.setdefault(c.ext_id, []).append(c)

    # peer 环 / 缺失
    peer_decl = {
        ext_id: ((item.get("lines") or {}).get(line) or {}).get("peer_access") or {}
        for item, ext_id in (
            (i, str(i.get("id") or "")) for i in svc.installed()
        )
        if ext_id
    }
    for f in iso.detect_peer_cycles(peer_decl) + iso.missing_peers(
        peer_decl, [str(i.get("id") or "") for i in svc.installed()]
    ):
        per_ext.setdefault(f.ext_id, []).append(f)

    all_findings = [f for fs in per_ext.values() for f in fs]
    summary = iso.summarize(all_findings)

    if getattr(args, "json", False):
        print(
            _json.dumps(
                {
                    "line": line,
                    "mode": mode,
                    "extended_ctx": extended_ctx,
                    "scanned": scanned,
                    "summary": summary,
                    "findings": [f.as_dict() for f in all_findings],
                    "per_ext": {
                        k: [f.as_dict() for f in v] for k, v in sorted(per_ext.items())
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not summary.get("block") else 1

    print(f"扩展隔离体检（{line} 线）")
    print(f"  当前模式：{mode}")
    print(f"  context 扩展键：{'开' if extended_ctx else '关'}")
    print(f"  已扫扩展：{scanned} 个")
    print()

    if not all_findings:
        print("没有发现跨扩展访问问题。")
        return 0

    by_level = {"block": [], "warn": []}
    for f in all_findings:
        by_level.setdefault(f.severity, []).append(f)

    for level, title in (("block", "阻断级"), ("warn", "警告级")):
        items = by_level.get(level) or []
        if not items:
            continue
        print(f"--- {title}（{len(items)} 项）---")
        for f in items:
            print(f"  {f.ext_id:28s} {f.human()}")
        print()

    if getattr(args, "patch", False):
        print("--- peer_access 草稿（贴进 extension.json 即可）---")
        for ext_id, findings in sorted(per_ext.items()):
            patch = iso.build_peer_access_patch(findings)
            if not patch:
                continue
            print(f"  {ext_id}:")
            print("    " + _json.dumps(patch, ensure_ascii=False))
        print()

    print(f"合计：{summary['total']} 项（阻断 {summary['block']}，警告 {summary['warn']}）")
    # L0-8 是**运行期共存**问题：两个包都装得上，只是同点回调可能有先后影响。
    # 把它和「装不装得上」混在一句话里，会让人以为包被拒了。
    install_blocked = [f for f in all_findings if f.severity == "block" and f.code != "L0-8"]
    if install_blocked:
        print("切到完全校验模式时，上面这些阻断级问题会导致对应扩展被拒绝安装/启用。")
    elif summary["block"]:
        print("钩子点撞车（L0-8）不影响安装，是运行期共存问题：同点回调可能互相影响。")
    return 0 if not summary["block"] else 1


def cmd_mods(args: argparse.Namespace) -> int:
    """列出 / 启用 / 停用 / 重扫钩子 / 批量安装。"""
    from pathlib import Path

    from app.core.ext.service import ExtensionService, rescan_runtime_hooks

    cfg = cfg_mod.load()
    svc = ExtensionService(cfg)

    if args.action == "install":
        # 与界面拖入等价：按指定引擎线安装，只是把结果打到 stdout。
        # 存在的理由：批量导入时界面只能一个个看，排障时需要一次性拿到
        # 每个包的成功/失败原因。
        targets = [p for p in ([args.ext_id] + list(args.paths or [])) if p]
        if not targets:
            from . import edition as _ed

            print(
                f"用法：{_ed.exe_name()} cli mods install "
                "<扩展包.zip> [更多...] [--line 4.6]"
            )
            return 2
        line = args.line or cfg_mod.BUILTIN_LINE
        print(f"安装目标版本：{line}")
        failed = 0
        for raw in targets:
            pkg = Path(raw)
            print(f"\n[{pkg.name}]")
            if not pkg.is_file():
                print("  跳过：文件不存在")
                failed += 1
                continue
            try:
                res = svc.install(pkg, target_line=line)
            except Exception as exc:  # noqa: BLE001 - CLI 要给出可读原因，别抛栈
                print(f"  异常：{exc}")
                failed += 1
                continue
            if res.ok:
                print(f"  OK  {res.name} {res.version}（{res.status}，{res.entry_count} 条目）")
                if res.message_extra:
                    print(f"      {res.message_extra}")
            else:
                print(f"  失败：{res.message}")
                for issue in res.issues or []:
                    detail = issue.get("message") or issue.get("detail") or ""
                    if detail:
                        print(f"      - {detail}")
                failed += 1
        print(f"\n完成：成功 {len(targets) - failed}，失败 {failed}")
        return 0 if failed == 0 else 1

    if args.action == "rescan":
        # 重扫已装扩展的 runtime_hooks。扫描器升级后（比如开始支持常量形式的
        # add_method_hook），老扩展的注册表里还是旧结果——不重装就不会更新。
        # 而漏掉的钩子会连锁：没被扫到 → 没被插桩 → 运行期注册被拒 →
        # 扩展的 install() 返回 false → 整个扩展静默失效。
        updates = rescan_runtime_hooks(cfg)
        if not updates:
            print("没有需要更新的扩展（钩子清单已是最新）。")
            return 0
        print(f"更新了 {len(updates)} 个扩展的钩子清单：")
        for u in updates:
            print(f"  {u['id']} [{u['line']}]：{u['before']} -> {u['after']}")
        print("\n下次启动游戏时生效。")
        return 0

    if args.action in ("enable", "disable"):
        if not args.ext_id:
            print("需要扩展 id")
            return 2
        line = getattr(args, "line", None) or None
        ok = svc.set_enabled(args.ext_id, args.action == "enable", line=line)
        scope = f"（{line} 线）" if line else ""
        print(f"{args.ext_id}{scope} -> {'启用' if args.action == 'enable' else '停用'}: {'ok' if ok else '未找到'}")
        return 0 if ok else 1

    items = svc.installed()
    if not items:
        print("尚未安装任何扩展。")
        return 0

    # ``--line`` 过去对这个子命令**完全无效**：参数收下了、输出照旧，
    # 于是「4.6 / 4.7 的安装互相独立」这件事在命令行上根本看不出来
    # （2026-11-13 实测：`mods list --line 4.7` 与 `--line 4.6` 输出一模一样）。
    # 现在按线取该线的执行档：启用位、状态、钩子、补丁都读 ``lines[<线>]``，
    # 而不是顶层的兼容字段。
    want = str(getattr(args, "line", "") or "").strip()
    if want:
        picked = [
            i for i in items
            if isinstance(i.get("lines"), dict) and want in i["lines"]
        ]
        if not picked:
            print(f"{want} 线没有已安装的扩展（共 {len(items)} 个包，但都不在这条线上）。")
            return 0
        print(f"{want} 线：{len(picked)} 个扩展")
        items = picked
    for item in items:
        per_line = item.get("lines") if isinstance(item.get("lines"), dict) else {}
        if want:
            src = per_line.get(want) or {}
            lines_label = ""
        else:
            # 没有指定线：一次看清「这个包装在哪几条线上」，多线共存时才看得出隔离
            src = item
            lines_label = f"[{'+'.join(sorted(per_line)) or item.get('engine_line') or '?'}] "
        enabled = src.get("enabled", item.get("enabled", True))
        status = src.get("status") or item.get("status") or ""
        hooks = src.get("runtime_hooks")
        if hooks is None:
            hooks = item.get("runtime_hooks") or []
        mark = "启用" if enabled else "停用"
        print(
            f"{lines_label}{item.get('id'):28s} {item.get('version',''):8s} {mark}  "
            f"{status:12s} 钩子 {len(hooks)}  "
            f"{src.get('installed_at') or item.get('installed_at','')}"
        )
        for patch in src.get("patches") or []:
            detail = "、".join(patch.get("details") or [])
            flag = "!" if patch.get("issues") else " "
            print(f"    {flag} 补丁 {patch.get('target')}（{patch.get('kind')}）{detail}")
            for issue in patch.get("issues") or []:
                print(f"        {issue}")
    return 0


def cmd_housekeeping(args: argparse.Namespace) -> int:
    """工作期缓存清理：启动时自动跑的那套，也可以手动触发。"""
    from app.core import housekeeping

    report = housekeeping.sweep(
        keep_days=args.keep_days,
        max_bytes=int(args.max_mb * 1024 * 1024),
        idle_seconds=args.idle_hours * 3600,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print("（dry-run：只打印计划，没有改动磁盘）")
    for name in report.removed:
        print(f"  删除 {name}")
    print(
        f"共 {report.count} 项，{housekeeping.human_size(report.bytes)}；"
        f"保留 {report.kept} 个失败现场"
    )
    for item in report.failures:
        print(f"  未清理：{item}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    """查看/重算扩展加载顺序（lock.json）。"""
    from app.core import runner
    from app.core.ext import enable as ext_enable
    from app.core.ext import resolver as resolver_mod
    from app.core.fsutil import read_json

    cfg = cfg_mod.load()
    line = args.line
    if args.show:
        path = Path(args.show)
        if not path.is_file():
            print(f"找不到 lock 文件：{path}")
            return 2
        data = read_json(path, default=None) or {}
        _print(data)
        return 0 if data.get("ok") else 1

    mods = ext_enable.enabled_mods(cfg, line)
    if not mods:
        print(f"{line} 线没有启用中的扩展。")
        return 0

    pairs: list[tuple[str, dict]] = []
    packages: dict[str, dict] = {}
    missing: list[str] = []
    for item in mods:
        ext_id = str(item.get("id"))
        # 按线取清单（manifest-46/47），不能硬编码 manifest.v2.json：
        # 只有旧包才留那一份，新装的包会读不到——CLI 与启动器就会给出
        # 两个不同的结论，排障时最容易被这个坑带偏。
        data = ext_enable.manifest_of(item, line)
        if not isinstance(data, dict):
            missing.append(ext_id)
            continue
        pairs.append((ext_id, data))
        packages[ext_id] = {
            "version": item.get("version", ""),
            "kind": data.get("kind", "extension"),
            "priority": data.get("priority", 50),
            "overlay": item.get("overlay", ""),
            "sha256": item.get("sha256", ""),
            "entries": item.get("entry_count", 0),
        }

    # 必须把处置台账读进来传给 resolve：否则已经处置过的位置重叠会一直被
    # 报成「待处置」，CLI 结论与界面（走 readiness）相反，排障时最容易被带偏。
    from app.core import paths as paths_mod
    from app.core.ext import decisions as decisions_mod

    store = decisions_mod.Store.load(paths_mod.CONFIG_DIR / "decisions.json")
    resolution = resolver_mod.resolve(
        pairs,
        compose=cfg_mod.extension_compose(cfg),
        mode=cfg_mod.extension_mode(cfg),
        line=line,
        store=store,
    )
    bid = DEFAULT_BASELINE.get(line, cfg_mod.BUILTIN_BASELINE)
    meta = (cfg.get("baselines") or {}).get(bid) or {}
    lock = resolver_mod.build_lock(
        resolution,
        packages,
        line=line,
        baseline={"id": bid, "sha256": meta.get("sha256", ""), "entry_count": meta.get("entry_count", 0)},
    )

    print(f"加载顺序（{line}）：")
    for index, entry in enumerate(lock["resolution_order"], 1):
        print(
            f"  {index}. {entry['id']:28s} {entry['kind']:10s} "
            f"priority={entry['priority']:<4} {entry['entries']:>6} 条目"
        )
    print(f"指纹：{lock['fingerprint']}")
    errors = [p for p in resolution.problems if p.level == "error"]
    warnings = [p for p in resolution.problems if p.level == "warn"]
    infos = [p for p in resolution.problems if p.level == "info"]
    for problem in errors:
        print(f"  [冲突] {problem.message}")
    for problem in warnings:
        print(f"  [注意] {problem.message}")
    for problem in infos:
        print(f"  [提示] {problem.message}")
    if missing:
        # 读不到清单的包无法参与解析。以前这里是静默跳过，看起来像
        # 「这个包没问题」，其实根本没检查。
        print(f"  [提示] 缺少可读清单，未参与解析：{'、'.join(missing)}")

    if args.write:
        target = runner.run_dir_for(f"lock{line.replace('.', '')}") / "lock.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {target}")
    return 0 if not errors else 1


def cmd_minigame(args: argparse.Namespace) -> int:
    """小游戏：列出 / 登记 / 移除 / 隔离开关 / 启动。"""
    from app.core import minigames as mini

    cfg = cfg_mod.load()

    if args.action == "list":
        items = mini.load()
        if not items:
            print("尚未登记小游戏。用 `minigame add <exe>` 添加。")
            return 0
        for m in items:
            print(
                f"{m.id:24s} {'隔离' if m.isolate_save else '直连'}  "
                f"{'就绪' if m.exists else '文件缺失'}  {m.exe}"
            )
        return 0

    if args.action == "add":
        if not args.exe:
            print("需要 exe 路径")
            return 2
        mg = mini.add(args.exe, name=args.name or "", args=args.args or [])
        _print(mg.as_dict())
        return 0

    if not args.mg_id:
        print("需要小游戏 id")
        return 2
    mg = mini.get(args.mg_id)
    if mg is None:
        print(f"未找到：{args.mg_id}")
        return 1

    if args.action == "remove":
        mini.remove(mg.id)
        print(f"{mg.id} 已移除")
        return 0

    if args.action == "isolate":
        mg.isolate_save = not mg.isolate_save
        mini.update(mg)
        print(f"{mg.id} 存档隔离 -> {'开' if mg.isolate_save else '关'}")
        return 0

    # run
    if not mg.exists:
        print(f"程序不存在：{mg.exe}")
        return 1
    program, launch_args, cwd, env = mini.build_invocation(mg, cfg, args.profile)
    print(f"启动 {mg.name}（cwd={cwd}，隔离={'开' if mg.isolate_save else '关'}）")
    try:
        proc = subprocess.run([program, *launch_args], cwd=cwd, env=env, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print("超时，已结束")
        return 124
    print(f"退出码 {proc.returncode}")
    return 0 if proc.returncode == 0 else 1


def _parse_size(text: str) -> int:
    """解析 ``1.8G`` / ``512M`` / ``1932735283`` 这类大小。"""
    raw = text.strip().upper().replace("IB", "").replace("B", "")
    mult = 1
    if raw.endswith("G"):
        mult, raw = 1024**3, raw[:-1]
    elif raw.endswith("M"):
        mult, raw = 1024**2, raw[:-1]
    elif raw.endswith("K"):
        mult, raw = 1024, raw[:-1]
    try:
        return int(float(raw) * mult)
    except ValueError as exc:
        raise SystemExit(f"无法解析大小：{text!r}") from exc


def cmd_shard(args: argparse.Namespace) -> int:
    """把基线（+ 覆盖层）重新打成一组 ≤ 上限的分片。"""
    from app.core.pck import packset as packset_mod
    from app.core.pck.reader import PckReader

    cfg = cfg_mod.load()
    bid = args.baseline or DEFAULT_BASELINE[args.line]
    meta = cfg_mod.baseline(cfg, bid)
    src = Path(meta["path"])
    if not src.exists():
        print(f"基线不存在：{src}")
        return 1

    readers = [PckReader.open(src)]
    for extra in args.overlay or []:
        p = Path(extra)
        if not p.exists():
            print(f"覆盖层不存在：{p}")
            return 1
        readers.append(PckReader.open(p))

    limit = _parse_size(args.limit)
    out = Path(args.out) if args.out else (paths.PACKS / f"{bid}-shards")
    if not out.is_absolute():
        out = paths.ROOT / out

    merged = packset_mod._merged_entries(readers, args.match)  # noqa: SLF001 - 仅用于预演统计
    plans, warnings = packset_mod.plan(((p, e.size) for p, (_r, e) in merged.items()), limit)
    total_bytes = sum(p.byte_size for p in plans)
    print(
        f"来源 {len(readers)} 个包，条目 {len(merged):,}，合计 {total_bytes:,} 字节 "
        f"({total_bytes / 1024**3:.2f} GiB)"
    )
    print(f"计划切成 {len(plans)} 片，上限 {limit:,} 字节：")
    for item in plans:
        print(f"  pack_{item.index:03d}.pck  条目 {len(item.entries):>7,}  {item.byte_size:>14,} 字节")
    for w in warnings:
        print(f"  [警告] {w}")

    if args.deactivate:
        cfg["baselines"][bid]["shard_packset"] = ""
        cfg_mod.save(cfg)
        print(f"已恢复用原包启动：{bid}")
        return 0

    if args.dry_run:
        print("（--dry-run，未写文件）")
        return 0

    from app.core.fsutil import require_space

    require_space(out if out.exists() else out.parent, total_bytes, factor=1.1)

    def progress(done: int, total: int, name: str) -> None:
        print(f"\r  写入 {done}/{total}：{name}", end="", flush=True)

    result = packset_mod.build(
        readers,
        out,
        line=args.line,
        limit=limit,
        packset_id=f"{bid}-shards",
        match=args.match,
        verify=not args.no_verify,
        progress=progress,
    )
    print()
    _print(result.as_dict())

    if args.activate:
        cfg["baselines"][bid]["shard_packset"] = str(result.packset)
        cfg_mod.save(cfg)
        print(f"已启用分片启动：{bid} -> {result.packset}")
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    """组装发布目录（或单文件 exe）。"""
    from app.core.build.package import PackageSpec, build_package

    spec = PackageSpec(
        line=args.line,
        name=args.name,
        out_dir=Path(args.out) if args.out else None,
        shards_packset=Path(args.shards) if args.shards else None,
        single_file=args.single_file,
    )
    print(f"开始组装 {args.line} 发布包 → {spec.out_dir or '(默认 builds 下)'}")
    result = build_package(spec)
    _print(result.as_dict())
    return 0


def cmd_compat(args: argparse.Namespace) -> int:
    """检查扩展包（或合集包）对某条引擎线的兼容性。"""
    from app.core.ext.compat_cmd import cmd_compat as _run

    return _run(args)


def cmd_drift(args: argparse.Namespace) -> int:
    """源码与出货字节码的一致性检测。"""
    from app.core import drift as drift_mod

    report = drift_mod.analyze(args.line, args.source, args.samples)
    data = report.as_dict()
    _print(data)

    counts = data["counts"]
    print()
    print(
        f"[{args.line}] 脚本 基线 {report.baseline_scripts} / 源码 {report.source_scripts}，"
        f"场景 基线 {report.baseline_scenes} / 源码 {report.source_scenes}"
    )
    print(
        f"  缺 .gdc {counts['missing_scripts']}，多 .gdc {counts['extra_scripts']}，"
        f"缺场景 {counts['missing_scenes']}，魔数异常 {counts['bad_magic']}，"
        f"钩子 {counts['hooks']}（不通过 {counts['hooks_bad']}）"
    )
    for note in report.notes:
        print(f"  ! {note}")
    print(f"结论：{'一致' if report.ok else '存在漂移'}；报告 {drift_mod.report_path(args.line)}")
    return 0 if report.ok else 1


def cmd_dump(args: argparse.Namespace) -> int:
    r = PckReader.open(args.path)
    if args.search:
        for e in r.search(args.search, limit=args.limit):
            print(f"{e.size:>12,}  {e.md5}  {e.path}")
        return 0
    _print(r.stats())
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = cfg_mod.load()
    meta = cfg_mod.baseline(cfg, args.baseline)
    p = Path(meta["path"])
    if not p.exists():
        _print({"ok": False, "error": "文件不存在", "path": str(p)})
        return 1
    actual = sha256_file(p)
    ok = actual == meta.get("sha256")
    _print({"baseline": args.baseline, "ok": ok, "expected": meta.get("sha256"), "actual": actual})
    return 0 if ok else 1


def cmd_custom_baseline(args: argparse.Namespace) -> int:
    """自定义基线：verify 只校验、import 校验通过后登记、status 查当前状态。"""
    from app.core import custom_baseline as cb
    from app.core import custom_lines as cl
    from app.core import identity_gate, line_desc

    cfg = cfg_mod.load()
    line_id = str(args.line or "")
    if not line_id:
        _print({"ok": False, "error": "需要 --line custom:<ID>"})
        return 2
    line = cl.get_line(cfg, line_id)
    if line is None:
        _print({"ok": False, "error": f"找不到自定义线 {line_id}"})
        return 2

    if args.action == "status":
        st = cb.status(line_id, line.identity_hash)
        _print({"line": line_id, **st})
        return 0 if st.get("ready") else 1
    if not args.path:
        _print({"ok": False, "error": "verify / import 需要给基线目录"})
        return 2

    game = str(args.game or line.path)
    verdict = identity_gate.probe(game)
    if not verdict.accepted:
        _print({"ok": False, "error": f"本体未通过身份闸门：{verdict.reason}"})
        return 2

    required: set[str] = set()
    desc_path = line_desc.desc_path_for(game)
    if desc_path.is_file():
        try:
            for cap in line_desc.load_desc(desc_path).get("capabilities") or []:
                if isinstance(cap, dict) and str(cap.get("kind") or "") in ("method", "hook"):
                    if cap.get("target"):
                        required.add(str(cap["target"]))
        except (line_desc.LineDescError, OSError, ValueError):
            pass

    report = cb.verify(args.path, verdict, game, required_scripts=required)
    payload: dict = {"line": line_id, "action": args.action, **report.as_dict()}
    if args.action == "import" and report.ok:
        payload["registered"] = cb.register(line_id, args.path, report)
    _print(payload)
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jjxf", description="《机甲旋风》启动器内核 CLI")
    # 全局选项。argparse 要求全局选项写在**子命令之前**：
    #     jjxf cli --ext-mode strict smoke 4.6
    # 子命令级再给一个同名选项（smoke/run），就能覆盖全局的。
    ap.add_argument(
        "--ext-mode",
        choices=[*cfg_mod.EXTENSION_MODES, *cfg_mod.MODE_ALIASES],
        default=None,
        help="扩展隔离模式，覆盖配置文件里的 extensions.extension_mode",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stat", help="环境与基线体检").set_defaults(func=cmd_stat)

    p = sub.add_parser("memtrim", help="试跑「启动前释放系统内存」")
    p.add_argument(
        "--apply",
        action="store_true",
        help="真的执行（不加只列出会腾哪些进程、能腾多少）",
    )
    p.add_argument(
        "--exclude-pid",
        type=int,
        action="append",
        default=[],
        help="不动的进程 pid，可重复（例如正在用的编辑器）",
    )
    p.set_defaults(func=cmd_memtrim)

    p = sub.add_parser("base", help="构建兼容底座")
    p.add_argument("lines", nargs="*", default=["46", "47"])
    p.set_defaults(func=cmd_base)

    p = sub.add_parser("index", help="重建基线索引")
    p.add_argument("ids", nargs="*", default=[])
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("packset", help="生成 packset")
    p.add_argument("line", choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--baseline", default=None)
    p.add_argument("-o", "--out", default=None)
    p.set_defaults(func=cmd_packset)

    p = sub.add_parser("smoke", help="冒烟测试")
    p.add_argument("line", choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--baseline", default=None)
    p.add_argument("--main", default="res://scenes/start_page.tscn")
    p.add_argument("--profile", default=None)
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--with-mods", action="store_true", help="带上已启用的扩展")
    p.add_argument(
        "--ext-mode",
        choices=[*cfg_mod.EXTENSION_MODES, *cfg_mod.MODE_ALIASES],
        # SUPPRESS 而不是 None：argparse 里子命令的同名选项会整体覆盖全局的
        # namespace，default=None 也会把全局的值盖掉。SUPPRESS 是「没写就不放」，
        # 全局的值才留得住。优先级：子命令 > 全局 > 配置文件 > normal。
        default=argparse.SUPPRESS,
        help="扩展隔离模式（覆盖全局 --ext-mode 与配置文件）",
    )
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("run", help="带窗口启动游戏")
    p.add_argument("line", choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--baseline", default=None)
    p.add_argument("--main", default="res://scenes/start_page.tscn")
    p.add_argument("--profile", default=None)
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--with-mods", action="store_true", help="带上已启用的扩展")
    p.add_argument(
        "--ext-mode",
        choices=[*cfg_mod.EXTENSION_MODES, *cfg_mod.MODE_ALIASES],
        # SUPPRESS 而不是 None：argparse 里子命令的同名选项会整体覆盖全局的
        # namespace，default=None 也会把全局的值盖掉。SUPPRESS 是「没写就不放」，
        # 全局的值才留得住。优先级：子命令 > 全局 > 配置文件 > normal。
        default=argparse.SUPPRESS,
        help="扩展隔离模式（覆盖全局 --ext-mode 与配置文件）",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("package", help="组装发布包（目录版 / 单文件 exe）")
    p.add_argument("line", choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--name", default="机甲旋风", help="产物名（exe 与目录名）")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--shards", default=None, help="分片 packset.json；不给则复制原基线")
    p.add_argument("--single-file", action="store_true", help="把底座 pck 内嵌进 exe")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("shard", help="把基线重新打成一组分片")
    p.add_argument("line", choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--baseline", default=None)
    p.add_argument("-o", "--out", default=None, help="输出目录，默认 packs/<基线>-shards")
    p.add_argument("--limit", default="1.8G", help="单片上限，如 1.8G / 900M / 字节数")
    p.add_argument("--match", default=None, help="只打包路径含该子串的条目（试跑用）")
    p.add_argument("--overlay", action="append", default=None, help="额外叠加的 pck（可多次）")
    p.add_argument("--dry-run", action="store_true", help="只出计划不写文件")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--activate", action="store_true", help="构建后把该 packset 设为基线的启动来源")
    p.add_argument("--deactivate", action="store_true", help="取消分片启动，恢复原包")
    p.set_defaults(func=cmd_shard)

    p = sub.add_parser("mods", help="列出/启用/停用/重扫/安装已安装扩展")
    p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "enable", "disable", "rescan", "install"],
    )
    p.add_argument("ext_id", nargs="?", default=None)
    p.add_argument("paths", nargs="*", default=[], help="install 时的扩展包路径")
    p.add_argument("--line", default=None,
                   help="目标引擎线（list / install / enable / disable 用）；"
                        "list 不传则显示每个包装在哪几条线")
    p.set_defaults(func=cmd_mods)

    p = sub.add_parser("housekeeping", help="清理工作期缓存（启动时也会自动跑一次）")
    p.add_argument("--keep-days", type=float, default=7.0, help="失败现场保留天数，默认 7")
    p.add_argument("--max-mb", type=float, default=200.0, help="失败现场总量上限（MB），默认 200")
    p.add_argument("--idle-hours", type=float, default=24.0, help="瞬时目录空闲多久算废弃，默认 24")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不删任何东西")
    p.set_defaults(func=cmd_housekeeping)

    p = sub.add_parser("lock", help="查看扩展加载顺序与冲突")
    p.add_argument("line", nargs="?", default=cfg_mod.BUILTIN_LINE, choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--write", action="store_true", help="把 lock.json 写到 builds/ 下")
    p.add_argument("--show", default=None, metavar="PATH", help="直接查看某个 lock.json")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("minigame", help="小游戏：列出/登记/移除/隔离开关/启动")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "isolate", "run"])
    p.add_argument("mg_id", nargs="?", default=None)
    p.add_argument("--exe", default=None, help="add 时的程序路径")
    p.add_argument("--name", default="")
    p.add_argument("--args", nargs="*", default=[])
    p.add_argument("--profile", default=None)
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(func=cmd_minigame)

    p = sub.add_parser("ext", help="检查扩展包（schema 1/2）")
    p.add_argument("package", help="zip 包或解包目录")
    p.add_argument("--check-hooks", action="store_true", help="把钩子对到反解源码上核对")
    p.add_argument("--install", action="store_true", help="装上这个扩展（合集包则逐个装）")
    p.add_argument("--verify", action="store_true", help="安装后跑一次挂载冒烟")
    p.add_argument(
        "--line",
        choices=list(cfg_mod.BASELINE_OF),
        default=None,
        help="目标引擎线；默认按清单里的 baseline 推断",
    )
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help="补丁只命中一部分也算装上（默认严格，装了但补丁没生效更糟）",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_ext)

    p = sub.add_parser("ext-migrate", help="收敛双线扩展：只保留清单声明的那条线")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不改动磁盘")
    p.set_defaults(func=cmd_ext_migrate)

    p = sub.add_parser("ext-audit", help="体检：扫已装扩展的跨扩展访问与钩子撞车")
    p.add_argument("line", nargs="?", default=cfg_mod.BUILTIN_LINE, choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--json", action="store_true", help="输出 JSON（便于 CI）")
    p.add_argument(
        "--patch",
        action="store_true",
        help="额外打印可直接粘进 extension.json 的 peer_access 草稿",
    )
    p.set_defaults(func=cmd_ext_audit)

    p = sub.add_parser("compat", help="检查扩展包对某条引擎线的兼容性")
    p.add_argument("package", help="zip 包或合集包")
    p.add_argument("--line", choices=list(cfg_mod.BASELINE_OF), default=cfg_mod.BUILTIN_LINE, help="目标引擎线")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_compat)

    p = sub.add_parser("drift", help="源码与出货字节码的一致性检测")
    p.add_argument("line", nargs="?", default=cfg_mod.BUILTIN_LINE, choices=list(cfg_mod.BASELINE_OF))
    p.add_argument("--source", default=None, help="反解工程目录（默认从配置取）")
    p.add_argument("--samples", type=int, default=24, help=".gdc 魔数抽样数")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser("dump", help="解析 pck")
    p.add_argument("path")
    p.add_argument("--search", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("verify", help="校验基线 sha256")
    p.add_argument("baseline")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("custom-baseline", help="自定义基线：验收/导入改版创作者提供的反解源码")
    p.add_argument("action", choices=("verify", "import", "status"))
    p.add_argument("path", nargs="?", help="基线目录（verify / import 必填）")
    p.add_argument("--line", default="", help="自定义线 id（custom:<ID>）")
    p.add_argument("--game", default="", help="改版本体文件；缺省取登记项里的路径")
    p.set_defaults(func=cmd_custom_baseline)

    return ap


def _redirect_console_when_frozen() -> object | None:
    """把 CLI 输出接到文件。

    打包版是 ``console=False``（双击不弹黑框），冻结后 ``sys.stdout`` 是
    ``None``，所有 print 都会抛 AttributeError 或静默丢失。命令行子命令
    因此完全没法用。这里在冻结环境把 stdout/stderr 指到
    ``logs/cli-<时间>.log``，至少事后能查。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    if not getattr(sys, "frozen", False):
        return None

    import datetime
    from pathlib import Path

    try:
        root = Path(sys.executable).resolve().parent
        logdir = root / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        handle = open(
            logdir / f"cli-{datetime.datetime.now():%Y%m%d-%H%M%S}.log",
            "w",
            encoding="utf-8",
            buffering=1,
        )
    except OSError:
        return None
    handle.write(f"# argv: {sys.argv}\n")
    sys.stdout = handle  # type: ignore[assignment]
    sys.stderr = handle  # type: ignore[assignment]
    return handle


#: 兜底日志句柄要留住引用，否则被回收时文件跟着关掉。
_CLI_LOG_HANDLE: object | None = None


def main(argv: list[str] | None = None) -> int:
    global _CLI_LOG_HANDLE
    if _CLI_LOG_HANDLE is None:
        _CLI_LOG_HANDLE = _redirect_console_when_frozen()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except LauncherError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    finally:
        if _CLI_LOG_HANDLE is not None:
            try:
                _CLI_LOG_HANDLE.flush()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 - 刷日志失败不该改退出码
                pass


if __name__ == "__main__":
    raise SystemExit(main())
