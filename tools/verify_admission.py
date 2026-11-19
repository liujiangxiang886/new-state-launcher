#!/usr/bin/env python3
"""工程端「开发者校验」准入真判定。

和作者端 ``jjm_pack.py verify`` 的区别：那个是单文件静态预检（作者只有
发布版 exe 也能跑），**本工具直接调用启动器本体**的清单解析、组合解析、
位置裁决和联机闸门——也就是点了「导入」按钮时真正走的那几段代码。

本工具会**复用** ``jjm_pack.py verify`` 的同一份规范加严判定，再叠加启动器
本体的组合解析，所以结论只会与作者端相同或更严，不会出现互相打架。

用法（在项目根执行，需要启动器源码与依赖环境）：

    python tools/verify_admission.py 我的扩展.zip
    python tools/verify_admission.py 我的扩展.zip --with 已有包A.zip --with 已有包B.zip
    python tools/verify_admission.py 合集包.zip --json > 准入报告.json

判定范围：
  能判  静态清单 error / 联机闸门 / 组合 error 冲突 / 未处置重叠(pending)
        / 合集成员是否全部通过
  不判  真实基线上的补丁命中、运行期场景加载（那要装完跑冒烟，见 tools/mods_e2e.py）
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (str(ROOT), str(ROOT / "tools")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

MODE = "strict"
LINE = "4.6"


def _spec_errors(package: Path, with_packages: list[Path]) -> list[dict]:
    """规范加严项：直接复用作者端 ``jjm_pack.py verify`` 的同一份实现。

    为什么要复用而不是重写：启动器本体允许 schema 2（只给一条 warn），
    但《开发者校验打包规范》要求 schema 3、钩子固化、接管三条件齐全。
    如果这里另写一套判定，就会出现「作者端说不许发、工程端说能发」。
    """
    import jjm_pack

    report = jjm_pack.verify_package(package, with_packages)
    return [f.as_dict() for f in report.errors]


def _launcher():
    """延迟导入：CLI 场景下先让 sys.path 就位。"""
    from app.core.ext import decisions as decisions_mod
    from app.core.ext import manifest as ext_manifest
    from app.core.ext import netguard
    from app.core.ext import resolver as resolver_mod
    from app.core.ext import service as service_mod

    return ext_manifest, resolver_mod, decisions_mod, netguard, service_mod


def _problems(resolution, ext_id: str) -> tuple[list[dict], list[dict]]:
    """只取「新包参与」的问题——已有包互相之间的老问题不算这次导入的账。

    与 ``ExtensionService.preview_install`` 的口径保持一致。
    """
    errors, warnings = [], []
    for problem in resolution.problems:
        row = problem.as_dict()
        if ext_id not in (row.get("ids") or []):
            continue
        (errors if row.get("level") == "error" else warnings).append(row)
    return errors, warnings


def admit_one(
    package: Path,
    with_packages: list[Path],
    work_dir: Path,
    ext_manifest=None,
    resolver_mod=None,
    decisions_mod=None,
    netguard=None,
    spec_errors: list[dict] | None = None,
) -> dict:
    """对单个包做一次与启动器同源的准入判定。纯读，不装、不写注册表。"""
    if ext_manifest is None:
        ext_manifest, resolver_mod, decisions_mod, netguard, _ = _launcher()

    result: dict = {
        "package": str(package),
        "mode": MODE,
        "verdict": "REJECTED",
        "reasons": [],
        "static": {},
        "spec": {},
        "netguard": {},
        "combination": {},
        "policy": {},
        "notes": [],
    }

    try:
        probe = ext_manifest.load(package)
    except Exception as exc:  # noqa: BLE001 - 清单读不出来也要给结构化结论
        result["reasons"].append(f"清单解析失败：{exc}")
        return result

    ext_id = probe.id
    result["ext_id"] = ext_id
    result["static"] = probe.summary()
    static_errors = [i for i in result["static"]["issues"] if i.get("level") == "error"]
    if static_errors:
        result["reasons"].append(
            "静态校验未通过："
            + "；".join(str(i.get("message") or "") for i in static_errors[:3])
        )

    net = netguard.audit_manifest_only(probe.data)
    result["netguard"] = {
        "blocked": bool(net.blocked),
        "blocks": [str(getattr(h, "reason", h)) for h in net.blocks],
    }
    if net.blocked:
        result["reasons"].append("联机闸门阻断：" + str(net.message()))

    # 规范加严项（schema 3 / 钩子固化 / 接管三条件）与作者端共用同一实现。
    errors_of_spec = (
        _spec_errors(package, with_packages) if spec_errors is None else spec_errors
    )
    result["spec"] = {"ok": not errors_of_spec, "errors": errors_of_spec}
    if errors_of_spec:
        result["reasons"].append(
            "规范加严项未通过："
            + "；".join(
                f"{e.get('code')} {e.get('message')}" for e in errors_of_spec[:3]
            )
        )

    pairs: list[tuple[str, dict]] = []
    for other in with_packages:
        try:
            m = ext_manifest.load(other)
        except Exception as exc:  # noqa: BLE001
            result["reasons"].append(f"--with 包解析失败 {other}：{exc}")
            continue
        if m.id == ext_id:
            continue  # 同 id 是升版本重装，不算组合冲突
        pairs.append((m.id, m.data))
    pairs.append((ext_id, probe.data))

    store = decisions_mod.Store.load(work_dir / "decisions.json")
    resolution = resolver_mod.resolve(
        pairs, compose=True, mode=MODE, line=LINE, store=store
    )
    errors, warnings = _problems(resolution, ext_id)
    skipped: list[dict] = []
    if not with_packages:
        # 作者手头不一定有依赖包。没传 --with 时把「依赖未启用」标成未验证，
        # 而不是判死——它在真实启动器里确实是错误，但这里缺的是输入不是包有问题。
        kept = []
        for row in errors:
            (skipped if row.get("code") == "requires.missing" else kept).append(row)
        errors = kept
    result["combination"] = {
        "compared": len(pairs) - 1,
        "order": list(resolution.order),
        "errors": errors,
        "warnings": warnings,
        "skipped": skipped,
    }
    if errors:
        result["reasons"].append(
            "组合硬冲突：" + "；".join(str(e.get("message") or "") for e in errors[:3])
        )
    if skipped:
        result["notes"].append(
            f"未提供 --with，{len(skipped)} 条依赖未验证；发布前请带上依赖包再跑一次"
        )

    plan = getattr(resolution, "plan", None)
    pending = []
    if plan is not None:
        for state in getattr(plan, "pending", []):
            row = state.as_dict()
            if ext_id in (row.get("parties") or []):
                pending.append(row)
    result["policy"] = {"pending": pending, "pending_count": len(pending)}
    if pending:
        result["reasons"].append(
            f"还有 {len(pending)} 处位置重叠未处置；开发者校验在导入阶段会直接拒装"
        )

    if not result["reasons"]:
        result["verdict"] = "ADMITTED"
    return result


def admit_bundle(package: Path, with_packages: list[Path], work_dir: Path) -> dict:
    """合集包：整包事务，一个成员不过就全不装。"""
    _, _, _, _, service_mod = _launcher()
    members = service_mod.detect_bundle(package)
    if not members:
        return None
    ext_manifest, resolver_mod, decisions_mod, netguard, _ = _launcher()
    result: dict = {
        "package": str(package),
        "mode": MODE,
        "verdict": "ADMITTED",
        "kind": "bundle",
        "member_count": len(members),
        "members": [],
        "reasons": [],
        "notes": [],
    }
    with zipfile.ZipFile(package) as outer:
        for index, name in enumerate(members):
            dest = work_dir / f"member-{index}.zip"
            dest.write_bytes(outer.read(name))
            one = admit_one(
                dest, with_packages + [work_dir / f"member-{i}.zip" for i in range(index)],
                work_dir, ext_manifest, resolver_mod, decisions_mod, netguard,
            )
            one["member_name"] = name
            result["members"].append(one)
            if one["verdict"] != "ADMITTED":
                result["reasons"].append(
                    f"成员 {name} 未通过（整包事务会导致整个合集不装）："
                    + "；".join(one["reasons"][:2])
                )

    errors_of_spec = _spec_errors(package, with_packages)
    result["spec"] = {"ok": not errors_of_spec, "errors": errors_of_spec}
    if errors_of_spec:
        result["reasons"].append(
            "合集规范加严项未通过："
            + "；".join(
                f"{e.get('code')} {e.get('message')}" for e in errors_of_spec[:2]
            )
        )
    if result["reasons"]:
        result["verdict"] = "REJECTED"
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        prog="verify_admission",
        description="用启动器本体的判定逻辑，验证扩展包能否通过完全校验（strict）",
    )
    ap.add_argument("package", help="扩展包或合集包（zip）")
    ap.add_argument(
        "--with", dest="with_pkg", action="append", default=[], metavar="已有包",
        help="会与本包共存的已有扩展，可重复",
    )
    ap.add_argument("--json", action="store_true", help="输出机器可读报告")
    args = ap.parse_args(argv)

    package = Path(args.package)
    if not package.exists():
        print(f"文件不存在：{package}", file=sys.stderr)
        return 2
    with_packages = [Path(x) for x in args.with_pkg]

    with tempfile.TemporaryDirectory(prefix="jjxf-admission-") as tmp:
        work_dir = Path(tmp)
        result = admit_bundle(package, with_packages, work_dir)
        if result is None:
            result = admit_one(package, with_packages, work_dir)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"包：{result['package']}")
        print(f"模式：{MODE}（导入即准入）")
        if result.get("kind") == "bundle":
            print(f"合集成员：{result['member_count']} 个")
        else:
            print(f"扩展 id：{result.get('ext_id', '')}")
            print(f"静态校验：{'通过' if result['static'].get('ok') else '未通过'}")
            print(f"规范加严：{'通过' if result['spec'].get('ok') else '未通过'}")
            print(f"联机闸门：{'阻断' if result['netguard'].get('blocked') else '通过'}")
            print(f"组合比对：{result['combination'].get('compared', 0)} 个已启用扩展，"
                  f"error {len(result['combination'].get('errors', []))} 条，"
                  f"warn {len(result['combination'].get('warnings', []))} 条")
            print(f"未处置重叠：{result['policy'].get('pending_count', 0)} 处")
        for reason in result["reasons"]:
            print(f"  - {reason}")
        for note in result.get("notes", []):
            print(f"  提示：{note}")
        print()
        print("准入结论：" + ("ADMITTED（可发）" if result["verdict"] == "ADMITTED"
                           else "REJECTED（禁止发布）"))
    return 0 if result["verdict"] == "ADMITTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
