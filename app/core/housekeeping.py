"""工作期缓存清理：成功即删 + 启动清扫。

为什么需要它
------------
每次启动都会在 ``builds/`` 下开一个工作目录（``run46-s1-20261101-201225``），
里面放 ``packset.json`` / ``mods.json`` / ``mount_report.json``；安装扩展会在
``builds/_install_<id>_<line>/`` 里解包源码；验证冒烟、重建缓存、导入剧情包
也各开一个。这些目录**启动器不会再读**，但以前没人删——跑几十次之后
``builds/`` 里躺着几百个目录，排障时根本分不清哪个是这次的现场。

两条策略
--------
**成功即删**
    游戏正常退出后立刻删掉本次的工作目录。注意是「退出后」而不是「启动时」：
    游戏运行期间还引用着里面的 ``packset.json``，删早了游戏直接崩。

**启动清扫**
    每次启动器起来时扫一遍 ``builds/``：

    - 失败现场（异常退出、被杀、启动器自己崩了）留 7 天，且总量压在 200 MB
      以内，超了从最旧的开始删；
    - 其它瞬时目录（``_verify_`` / ``_rebuild_`` / ``_bundle_`` / ``import-`` …）
      过 1 天就清掉——它们只是中间产物，没有排障价值。

**不删的东西**（每一条都是有意保留）
    - ``_install_<id>_<line>/``：里面是扩展源码，钩子重扫要靠它
      （见 ``service.rescan_runtime_hooks``）；
    - ``_ext_src/``：用户装的扩展包原件，重装用；
    - ``release-*``：用户自己打出来的发布包；
    - ``packs/``、``profiles/``、``cache/ui/``：用户数据与素材。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

#: 失败现场保留天数
KEEP_DAYS = 7

#: 失败现场总量上限（超出从最旧的删）
MAX_BYTES = 200 * 1024 * 1024

#: 非 run 的瞬时目录：超过这个年龄就认为是被中断留下的
IDLE_SECONDS = 24 * 3600

#: ``runner.run_dir_for("run46-s1")`` 生成的目录名
_RUN_DIR = re.compile(r"^run\d+-s\d+-\d{8}-\d{6}$")

#: 其它一次性工作目录前缀（都在 ``builds/`` 下，都不含用户数据）
_TRANSIENT_PREFIXES = ("_verify_", "_rebuild_", "_bundle_", "_ext_", "import-")

#: 看起来像瞬时目录、其实不能删的（``_ext_src`` 存着用户装的扩展包原件）
_KEEP_ALWAYS = frozenset({"_ext_src"})


@dataclass
class SweepReport:
    """一次清扫的结果。``kept`` 是「留着待观察」的失败现场数量。"""

    removed: list[str] = field(default_factory=list)
    bytes: int = 0
    kept: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.removed)

    def message(self) -> str:
        if not self.removed and not self.failures:
            return "" if not self.kept else f"保留 {self.kept} 个失败现场待排查"
        parts: list[str] = []
        if self.removed:
            parts.append(f"清理工作期缓存 {len(self.removed)} 项（{human_size(self.bytes)}）")
        if self.kept:
            parts.append(f"保留 {self.kept} 个失败现场")
        if self.failures:
            parts.append(f"{len(self.failures)} 项未清理")
        return "；".join(parts)


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def _age(path: Path, now: float | None = None) -> float:
    """目录有多久没被动过（秒）。取目录自身 mtime，够用且只看一次 stat。"""
    try:
        return (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return 0.0


def run_dirs() -> list[Path]:
    """``builds/`` 下所有「一次启动」的工作目录，按时间从旧到新。"""
    out: list[Path] = []
    if not paths.BUILDS.is_dir():
        return out
    for child in paths.BUILDS.iterdir():
        if child.is_dir() and _RUN_DIR.match(child.name):
            out.append(child)
    out.sort(key=lambda p: _age(p), reverse=True)
    return out


def transient_dirs() -> list[Path]:
    """非 run 的一次性工作目录（验证、重建、合集解包、剧情导入…）。"""
    out: list[Path] = []
    if not paths.BUILDS.is_dir():
        return out
    for child in paths.BUILDS.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if name in _KEEP_ALWAYS:
            continue
        if any(name.startswith(prefix) for prefix in _TRANSIENT_PREFIXES):
            out.append(child)
    return out


def _remove(path: Path, report: SweepReport, dry_run: bool = False) -> None:
    size = _dir_bytes(path)
    if dry_run:
        report.removed.append(path.name)
        report.bytes += size
        return
    try:
        shutil.rmtree(path, ignore_errors=False)
    except OSError as exc:
        report.failures.append(f"{path.name}：{exc}")
        return
    report.removed.append(path.name)
    report.bytes += size


def cleanup_run(run_dir: str | Path | None, *, ok: bool) -> bool:
    """游戏退出后处理本次的工作目录。``ok=False`` 时保留现场。

    返回是否真的删掉了。删不掉不算错误——下次启动清扫会再试一次。
    """
    if not run_dir:
        return False
    path = Path(run_dir)
    if not path.is_dir():
        return False
    if not ok:
        return False
    try:
        shutil.rmtree(path, ignore_errors=False)
        return True
    except OSError:
        return False


def cleanup_orphan_cache() -> int:
    """删掉 ``cache/instrumented/`` 里「只剩一半」的缓存。

    插桩缓存是 ``<hash>.pck`` + ``<hash>.json`` 一对：元数据丢了就没法判断
    它属于哪条线、基于哪份覆盖层，留着只会让「按线清理」漏掉它（历史 bug）。
    """
    cache_dir = paths.CACHE / "instrumented"
    if not cache_dir.is_dir():
        return 0
    removed = 0
    pcks = {p.stem for p in cache_dir.glob("*.pck")}
    jsons = {p.stem for p in cache_dir.glob("*.json")}
    for stem in pcks - jsons:
        try:
            (cache_dir / f"{stem}.pck").unlink()
            removed += 1
        except OSError:
            continue
    for stem in jsons - pcks:
        try:
            (cache_dir / f"{stem}.json").unlink()
            removed += 1
        except OSError:
            continue
    return removed


def sweep(
    *,
    keep_days: float = KEEP_DAYS,
    max_bytes: int = MAX_BYTES,
    idle_seconds: float = IDLE_SECONDS,
    now: float | None = None,
    dry_run: bool = False,
) -> SweepReport:
    """启动时跑一次：清瞬时目录、按保留策略裁剪失败现场。只动 ``builds/``。"""
    report = SweepReport()
    stamp = time.time() if now is None else now

    for path in transient_dirs():
        if _age(path, stamp) < idle_seconds:
            continue  # 可能是另一个实例正在用
        _remove(path, report, dry_run)

    remaining = run_dirs()
    survivors: list[Path] = []
    for path in remaining:
        if _age(path, stamp) > keep_days * 86400:
            _remove(path, report, dry_run)
        else:
            survivors.append(path)

    # 总量仍超标就从最旧的开始删（survivors 已按旧→新排序）
    total = sum(_dir_bytes(p) for p in survivors)
    for path in list(survivors):
        if total <= max_bytes:
            break
        size = _dir_bytes(path)
        _remove(path, report, dry_run)
        if path.name in report.removed:
            total -= size
            survivors.remove(path)
    report.kept = len(survivors)

    if not dry_run:
        try:
            cleanup_orphan_cache()
        except Exception as exc:  # noqa: BLE001 - 清缓存失败不该挡住启动
            report.failures.append(f"孤立缓存：{exc}")
    return report
