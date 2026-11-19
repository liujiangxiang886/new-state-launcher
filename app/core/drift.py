"""基线漂移检测：反解源码与出货脚本/场景的一致性。

为什么需要：覆盖层与钩子插桩都建立在「反解源码 ≈ 出货字节码」这个假设上。
一旦基线换了版本、或反解时漏了脚本，插桩出来的脚本就会和游戏其它部分对不上，
表现为难查的运行期错误。这里把假设变成可测的检查：

1. **脚本集比对**：源码 ``scripts/x.gd`` 必须在基线里有 ``scripts/x.gdc``；
   反过来基线多出来的 ``.gdc`` 说明反解漏了文件。
2. **场景集比对**：``scenes/x.tscn`` 对应基线的 ``scenes/x.tscn.remap``。
3. **字节码魔数**：抽样读 ``.gdc`` 头部，必须是 ``GDSC``，否则不是编译脚本。
4. **插桩目标**：已安装扩展的每个钩子，目标脚本要真在基线里、方法要真在源码里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import paths
from .baseline import path_set
from .fsutil import atomic_write_json, read_json

#: 编译脚本的魔数（Godot 4.x）
GDC_MAGIC = b"GDSC"

REPORT_DIR = paths.LOGS


@dataclass
class DriftReport:
    line: str
    source_root: str
    baseline_scripts: int = 0
    baseline_scenes: int = 0
    source_scripts: int = 0
    source_scenes: int = 0
    missing_scripts: list[str] = field(default_factory=list)
    extra_scripts: list[str] = field(default_factory=list)
    missing_scenes: list[str] = field(default_factory=list)
    extra_scenes: list[str] = field(default_factory=list)
    bad_magic: list[str] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked_at: str = ""

    @property
    def ok(self) -> bool:
        return not (
            self.missing_scripts
            or self.extra_scripts
            or self.missing_scenes
            or self.bad_magic
            or any(not h.get("ok") for h in self.hooks)
        )

    def summary(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "line": self.line,
            "source_root": self.source_root,
            "baseline": {"scripts": self.baseline_scripts, "scenes": self.baseline_scenes},
            "source": {"scripts": self.source_scripts, "scenes": self.source_scenes},
            "missing_scripts": self.missing_scripts[:40],
            "extra_scripts": self.extra_scripts[:40],
            "missing_scenes": self.missing_scenes[:40],
            "extra_scenes": self.extra_scenes[:40],
            "bad_magic": self.bad_magic[:20],
            "hooks": self.hooks,
            "notes": self.notes,
            "checked_at": self.checked_at,
        }

    def as_dict(self) -> dict[str, object]:
        data = self.summary()
        data["counts"] = {
            "missing_scripts": len(self.missing_scripts),
            "extra_scripts": len(self.extra_scripts),
            "missing_scenes": len(self.missing_scenes),
            "extra_scenes": len(self.extra_scenes),
            "bad_magic": len(self.bad_magic),
            "hooks": len(self.hooks),
            "hooks_bad": sum(1 for h in self.hooks if not h.get("ok")),
        }
        return data


def report_path(line: str) -> Path:
    suffix = line.replace(".", "")
    return REPORT_DIR / f"drift-{suffix}.json"


def _source_root(cfg: dict, line: str) -> Path | None:
    """找该引擎线对应的反解工程目录。

    1.4 起只查 ``sources.reversed_project``（旧的 ``story47.reversed_project``
    在配置加载时已并入这里，见 ``config.migrate_legacy``）。
    """
    configured = (cfg.get("sources") or {}).get("reversed_project") or ""
    if configured and Path(configured).is_dir():
        return Path(configured)
    meta = (cfg.get("runtime_sources") or {}).get(line) or {}
    project = meta.get("project_godot") or ""
    return Path(project).parent if project and Path(project).exists() else None


def _scan_sources(root: Path) -> tuple[set[str], set[str]]:
    scripts = {
        p.relative_to(root).as_posix()
        for p in (root / "scripts").rglob("*.gd")
    } if (root / "scripts").is_dir() else set()
    scenes = {
        p.relative_to(root).as_posix()
        for p in (root / "scenes").rglob("*.tscn")
    } if (root / "scenes").is_dir() else set()
    return scripts, scenes


def _check_magic(baseline_file: Path, limit: int) -> list[str]:
    """抽样读 .gdc 头部；读不到或魔数不对都算问题。"""
    from .pck import PckReader

    bad: list[str] = []
    try:
        reader = PckReader.open(baseline_file)
    except Exception as exc:  # noqa: BLE001
        return [f"无法打开基线包：{exc}"]
    entries = [e for e in reader.entries if e.path.endswith(".gdc")]
    if not entries:
        return [f"基线里没有 .gdc 条目：{baseline_file.name}"]
    step = max(1, len(entries) // max(1, limit))
    for entry in entries[::step][:limit]:
        try:
            head = reader.read_bytes(entry)[:4]
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{entry.path}：读取失败 {exc}")
            break
        if head != GDC_MAGIC:
            bad.append(f"{entry.path}：头部 {head!r}，不是 GDSC")
    return bad


def _check_hooks(cfg: dict, line: str, gdc_scripts: set[str], root: Path | None) -> list[dict]:
    """核对已安装扩展的插桩点。"""
    out: list[dict] = []
    try:
        from .ext.service import ExtensionService

        items = ExtensionService(cfg).installed()
    except Exception:  # noqa: BLE001 - 扩展模块不可用不影响漂移结论
        return out
    for item in items:
        if not item.get("enabled", True):
            continue
        for hook in item.get("runtime_hooks") or []:
            script = str(hook.get("script") or "").replace("res://", "")
            method = str(hook.get("method") or "")
            in_baseline = script in gdc_scripts
            in_source = None
            if root is not None and script:
                target = root / script
                if target.exists():
                    in_source = f"func {method}(" in target.read_text(encoding="utf-8", errors="replace")
            out.append(
                {
                    "ext": item.get("id"),
                    "script": script,
                    "method": method,
                    "phase": hook.get("phase"),
                    "in_baseline": in_baseline,
                    "in_source": in_source,
                    "ok": in_baseline and (in_source is not False),
                }
            )
    return out


def analyze(line: str = cfg_mod.BUILTIN_LINE, source_root: str | Path | None = None, magic_samples: int = 24) -> DriftReport:
    """跑一次漂移检测。只做只读操作，不碰基线文件。"""
    cfg = cfg_mod.load()
    root = Path(source_root) if source_root else _source_root(cfg, line)

    report = DriftReport(line=line, source_root=str(root or ""), checked_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        paths_set = path_set(line)
    except FileNotFoundError:
        report.notes.append(f"缺少 {line} 的基线索引，无法比对（先跑 python -m app.cli index）")
        return report

    gdc = {p[:-1] for p in paths_set if p.endswith(".gdc")}
    remaps = {p[: -len(".remap")] for p in paths_set if p.endswith(".tscn.remap")}
    report.baseline_scripts = len(gdc)
    report.baseline_scenes = len(remaps)

    if root is None or not root.is_dir():
        report.notes.append("没有找到反解工程目录，只能比对基线自身")
        report.missing_scripts = []
    else:
        src_scripts, src_scenes = _scan_sources(root)
        report.source_scripts = len(src_scripts)
        report.source_scenes = len(src_scenes)
        report.missing_scripts = sorted(src_scripts - gdc)
        report.extra_scripts = sorted(gdc - src_scripts)
        report.missing_scenes = sorted(src_scenes - remaps)
        report.extra_scenes = sorted(remaps - src_scenes)

    # 字节码抽样
    bid = next((b for b, m in (cfg.get("baselines") or {}).items() if str(m.get("engine")) == line), None)
    if bid:
        meta = cfg["baselines"][bid]
        baseline_file = Path(meta["path"])
        if baseline_file.exists():
            report.bad_magic = _check_magic(baseline_file, magic_samples)
        else:
            report.notes.append(f"基线文件不存在：{baseline_file}")

    report.hooks = _check_hooks(cfg, line, gdc, root if root and root.is_dir() else None)

    # 结论补充
    if report.missing_scripts:
        report.notes.append(f"{len(report.missing_scripts)} 个源脚本在基线里没有对应 .gdc（源码比出货新，或基线换了版本）")
    if report.extra_scripts:
        report.notes.append(f"{len(report.extra_scripts)} 个 .gdc 没有对应源脚本（反解遗漏）")

    atomic_write_json(report_path(line), report.as_dict())
    return report


def load_last(line: str = cfg_mod.BUILTIN_LINE) -> dict | None:
    data = read_json(report_path(line), default=None)
    return data if isinstance(data, dict) else None
