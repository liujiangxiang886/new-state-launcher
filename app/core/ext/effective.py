"""这条引擎线上「谁真的会被挂载」的唯一真相源。

为什么单独一层
--------------

``gate.plan()`` 早就算出了「保留谁、剔除谁」，但它只被 ``readiness`` 与
``enable.build_for_launch`` 用过；两处界面（扩展管理、游戏库右栏）读的是
注册表全量，于是出现这种场面：启动日志写着「剔除 13 个，剩 5 个可启动」，
界面上 18 个扩展却全是「已就绪 / 已启用」。同一个问题，三处说法。

结论只能有一个。凡是展示「哪些扩展在生效」的地方，都走 :func:`of`，
不要自己拼 ``installed()`` + ``enabled_mods()``。

缓存
----

``gate.plan`` 要跑多轮 ``resolver.resolve``（18 个包实测约 40 ms），界面
每刷新一次都算就太浪费。这里按「隔离模式 + 线 + 注册表 mtime + packs 目录
mtime」做键缓存；装/卸/启停扩展都会改动这两处，缓存自动失效。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .. import config as cfg_mod
from .. import paths
from . import gate as gate_mod

#: 状态：真正会被挂载
ACTIVE = "active"
#: 状态：开发者模式下被整组剔除
DROPPED = "dropped"
#: 状态：用户主动停用
DISABLED = "disabled"

_CACHE: dict[str, tuple[str, "Effective"]] = {}


@dataclass
class Effective:
    """一条线在某模式下的生效视图。"""

    line: str
    mode: str
    keep: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    plan: gate_mod.GatePlan | None = None

    @property
    def developer(self) -> bool:
        """当前是否处于会按判定剔除的档位。

        名字保留是为了不动调用点；语义已统一为 :func:`config.is_strict` ——
        「开发者档」这个概念没有了，只有「完全校验」与「宽松校验」。
        """
        return cfg_mod.is_strict(self.mode)

    @property
    def keep_set(self) -> set[str]:
        return set(self.keep)

    @property
    def dropped_set(self) -> set[str]:
        return set(self.dropped)

    def status_of(self, ext_id: str, *, enabled: bool = True) -> str:
        """给界面用的三态：启用 / 停用 / 被剔除。

        停用优先于剔除：已经手工停掉的包，再说它「被剔除」只会让人困惑。
        """
        if not enabled:
            return DISABLED
        if ext_id in self.dropped_set:
            return DROPPED
        return ACTIVE

    def reasons_of(self, ext_id: str) -> list[str]:
        """这个包被剔除的原因（撞在哪些目标上）。没被剔除就是空表。"""
        if self.plan is None or ext_id not in self.dropped_set:
            return []
        for pid, tags in self.plan.by_ext():
            if pid == ext_id:
                return list(tags)
        return []

    def text_of(self, ext_id: str) -> str:
        """一行说明，直接放进 tooltip。"""
        reasons = self.reasons_of(ext_id)
        if not reasons:
            return ""
        return (
            "开发者模式启动时会被整组剔除（不保留其中任何一个）：\n"
            + "\n".join(f"· 与其它扩展同改 {t}" for t in reasons)
            + "\n\n停用它即可从「剔除名单」里去掉；想让它生效需等作者适配。"
        )

    def summary(self) -> str:
        if not self.developer:
            return f"{len(self.keep)} 个扩展生效（{self.mode}）"
        if not self.dropped:
            return f"{len(self.keep)} 个扩展全部通过，无需剔除"
        return f"生效 {len(self.keep)} 个 · 剔除 {len(self.dropped)} 个"

    def count_text(self, line_label: str = "", total: int | None = None) -> str:
        """游戏库底部的计数文案。"""
        head = f"共 {total} 项 · " if total is not None else ""
        line_part = f"{line_label} 线 " if line_label else ""
        if not self.developer or not self.dropped:
            return f"{head}{line_part}生效 {len(self.keep)} 项"
        return (
            f"{head}{line_part}生效 {len(self.keep)} 项 · "
            f"开发者模式剔除 {len(self.dropped)} 项"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "mode": self.mode,
            "keep": list(self.keep),
            "dropped": list(self.dropped),
            "summary": self.summary(),
        }


def _stamp(cfg: dict, line: str) -> str:
    """缓存键：模式 + 线 + 注册表与 packs 目录的 mtime。

    注册表路径取 ``service.REGISTRY`` 而不是 ``CONFIG_DIR / extensions.json``：
    测试会把前者指到临时目录，写死路径会让缓存键对着一个没变的旧文件，
    于是「改了注册表、视图还是旧结论」。
    """
    from pathlib import Path

    from . import service as service_mod

    parts = [line, cfg_mod.extension_mode(cfg)]
    for path in (Path(service_mod.REGISTRY), paths.PACKS):
        try:
            st = path.stat()
            parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{path.name}:-")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def of(cfg: dict, line: str, *, refresh: bool = False) -> Effective:
    """取这条线的生效视图。纯计算，只读配置与磁盘，不写任何东西。"""
    key = _stamp(cfg, line)
    if not refresh:
        cached = _CACHE.get(line)
        if cached and cached[0] == key:
            return cached[1]

    plan = gate_mod.plan(cfg, line)
    view = Effective(
        line=line,
        mode=plan.mode,
        keep=list(plan.keep),
        dropped=list(plan.dropped_ids),
        plan=plan,
    )
    _CACHE[line] = (key, view)
    return view


def invalidate(line: str = "") -> None:
    """装/卸扩展之后手动作废（mtime 精度不够时兜底，例如同一秒内连装两个）。"""
    if line:
        _CACHE.pop(line, None)
    else:
        _CACHE.clear()
