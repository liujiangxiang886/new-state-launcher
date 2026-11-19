"""冲突处置的持久化：停用谁、兼容成什么值、谁依赖谁。

为什么要单独存
--------------
处置是**玩家本地决定**，不能写进扩展包（那会污染作者产物，也没法回退）。
但它必须能复现：下次启动不能重新问一遍，换包之后也不能拿旧决定硬套。
所以每条决定都带一个内容指纹。

指纹的口径（2026-11-05 修订）
------------------------------
**只对「这一冲突位置」本身取指纹，不对整份清单取指纹。**

旧口径把 ``id@version#整份清单摘要`` 全部算进去，后果是：
包只要升个版本号（哪怕这次只改了一行无关的 README），
该包参与的所有处置记录**立刻全部失配**，开发者校验会把已经处置过的重叠
当成「未处置」而拒绝安装。

实测事故：``ai_act.combat_workbench`` 从 0.22.2 升到 0.22.3（内容只动了两个
``script_members`` 的 target），13 条处置记录一起失效，装包直接报
「与已启用扩展存在未处置冲突」。

新口径保留的仍然是最重要的那层保护：**这一处的内容真的变了就失效**。
判断依据是这一处各方写的 ``find`` / 写入值 / 整文件来源，见
:func:`_party_signature`。版本号、清单里无关的增删都不再影响它。

文件位置：``config/decisions.json``。只存这一处，不碰注册表。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..fsutil import atomic_write_json, read_json

#: 合法处置。``disable`` 是包级，其余两个是位置级。
ACTIONS = ("disable", "compatible", "dependent")


def manifest_digest(data: dict) -> str:
    """整份清单的内容指纹（与安装时间、覆盖层哈希无关，换包内容才变）。

    注意：**不要再拿它当处置指纹的构成**。它含 version，升级即失配，
    见模块开头「指纹的口径」。保留此函数供其它诊断用途。
    """
    blob = json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _party_signature(pid: str, target: str, locator: str, data: dict) -> str:
    """提取「某个包在这一处写了什么」。

    这是处置是否仍然有效的真正依据：

    - ``text_patches``：参与这一处的 ``find``（＋``mode``）
    - ``data_patches``：该指针的写入值
    - 整文件覆盖：该文件条目的 source
    - 钩子：方法名与相位

    版本号、包名、README 之类**不参与**，所以升级版本不会让处置失效；
    但这一处的补丁被作者改写时，签名会变，处置自动过期 —— 这正是我们要的。
    """
    data = data or {}
    frags: list[str] = []

    # --- 文本补丁：只取命中这一处的那些
    for patch in data.get("text_patches") or []:
        if str(patch.get("target") or "") != target:
            continue
        find = str(patch.get("find") or "")
        mode = str(patch.get("mode") or "literal")
        # locator 形如 text:literal:<hash>，只认同一模式的
        if locator.startswith("text:") and mode not in locator:
            continue
        frags.append(f"t:{mode}:{find}")

    # --- 数据补丁：结构是 {"target":..., "operations":[{"op","path","value"}]}
    #     locator 就是那个 JSON Pointer，所以要按 path 精确对上这一处。
    ptr = locator
    for patch in data.get("data_patches") or []:
        if str(patch.get("target") or "") != target:
            continue
        ops = patch.get("operations")
        if isinstance(ops, list):
            for o in ops:
                if not isinstance(o, dict):
                    continue
                path = str(o.get("path") or "")
                if ptr and path and path != ptr:
                    continue
                frags.append(f"d:{path}={o.get('value')!r}")
        elif patch.get("pointer") is not None:
            # 兼容旧写法（顶层 pointer/value）
            frags.append(f"d:{patch.get('pointer')}={patch.get('value')!r}")

    # --- 整文件覆盖
    for rec in data.get("files") or []:
        if str(rec.get("target") or "") != target:
            continue
        frags.append(f"f:{rec.get('source') or ''}")

    # --- 方法钩子
    for hook in data.get("method_hooks") or []:
        if str(hook.get("target") or "") != target:
            continue
        frags.append(f"h:{hook.get('method')}:{hook.get('phase') or ''}")

    blob = "|".join(sorted(frags))
    # 提取不到可识别的写法时返回空串（**不要**退回整份清单摘要）：
    # 整份摘要含 version，会把「只升版本」也判成失效，而这正是本函数
    # 要修掉的问题。空签名表达的是「这个包在这一处没写东西」，
    # 这是内容事实，不会随版本变化；真改了这一处自然会有签名。
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def fingerprint(
    line: str,
    target: str,
    locator: str,
    parties: Iterable[str],
    meta: dict[str, dict],
) -> str:
    """处置指纹：``线|目标|位置|各方在这一处写了什么``。

    与旧版的区别：不再拼 ``id@version``，也不再用整份清单摘要。
    只认「这一处的实际内容」，因此**升级版本号不会让处置失效**。
    """
    parts = [str(line), str(target), str(locator)]
    for pid in sorted(str(p) for p in parties):
        sig = _party_signature(pid, target, locator, meta.get(pid) or {})
        parts.append(f"{pid}#{sig}")
    return "sha256:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass
class Decision:
    line: str
    target: str
    locator: str
    parties: tuple[str, ...]
    action: str
    party: str = ""          # disable 时停谁
    value: Any = None        # compatible 的最终值
    before: str = ""         # dependent：谁在前
    after: str = ""          # dependent：谁在后，且获得该位置的写入权
    fingerprint: str = ""
    created_at: str = ""

    @property
    def scope(self) -> str:
        return "package" if self.action == "disable" else "location"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "target": self.target,
            "locator": self.locator,
            "parties": list(self.parties),
            "action": self.action,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }
        if self.party:
            out["party"] = self.party
        if self.action == "compatible":
            out["value"] = self.value
        if self.action == "dependent":
            out["before"] = self.before
            out["after"] = self.after
        return out

    @classmethod
    def from_dict(cls, line: str, raw: dict) -> "Decision":
        parties = tuple(str(x) for x in (raw.get("parties") or []))
        return cls(
            line=line,
            target=str(raw.get("target") or ""),
            locator=str(raw.get("locator") or ""),
            parties=parties,
            action=str(raw.get("action") or ""),
            party=str(raw.get("party") or ""),
            value=raw.get("value"),
            before=str(raw.get("before") or ""),
            after=str(raw.get("after") or ""),
            fingerprint=str(raw.get("fingerprint") or ""),
            created_at=str(raw.get("created_at") or ""),
        )


@dataclass
class Store:
    path: Path
    lines: dict[str, list[Decision]] = field(default_factory=dict)
    dirty: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "Store":
        p = Path(path)
        store = cls(path=p)
        if not p.exists():
            return store
        try:
            raw = read_json(p)
        except (OSError, ValueError):
            return store
        if not isinstance(raw, dict):
            return store
        for line, items in (raw.get("lines") or {}).items():
            out: list[Decision] = []
            for item in items or []:
                if isinstance(item, dict) and item.get("action") in ACTIONS:
                    out.append(Decision.from_dict(str(line), item))
            store.lines[str(line)] = out
        return store

    def items(self, line: str) -> list[Decision]:
        return list(self.lines.get(str(line)) or [])

    def match(
        self,
        line: str,
        target: str,
        locator: str,
        parties: Iterable[str],
        meta: dict[str, dict],
    ) -> Decision | None:
        """只返回指纹仍然对得上的决定；对不上视为过期，不参与合成。"""
        want = fingerprint(line, target, locator, parties, meta)
        for item in self.items(line):
            if item.target == target and item.locator == locator and item.fingerprint == want:
                if set(item.parties) == set(str(p) for p in parties):
                    return item
        return None

    def put(self, decision: Decision) -> None:
        bucket = self.lines.setdefault(decision.line, [])
        bucket[:] = [
            x
            for x in bucket
            if not (
                x.target == decision.target
                and x.locator == decision.locator
                and set(x.parties) == set(decision.parties)
            )
        ]
        bucket.append(decision)
        self.dirty = True

    def drop(self, line: str, target: str, locator: str, parties: Iterable[str]) -> None:
        bucket = self.lines.get(str(line)) or []
        keep = [
            x
            for x in bucket
            if not (
                x.target == target
                and x.locator == locator
                and set(x.parties) == set(str(p) for p in parties)
            )
        ]
        if len(keep) != len(bucket):
            self.lines[str(line)] = keep
            self.dirty = True

    def prune(self, line: str, live: set[tuple[str, str, tuple[str, ...]]]) -> int:
        """删掉指向已卸载/已换版本组合的决定。返回删除条数。"""
        bucket = self.lines.get(str(line)) or []
        keep = [
            x
            for x in bucket
            if (x.target, x.locator, tuple(sorted(x.parties))) in live
        ]
        removed = len(bucket) - len(keep)
        if removed:
            self.lines[str(line)] = keep
            self.dirty = True
        return removed

    def save(self) -> Path | None:
        if not self.dirty:
            return None
        payload = {
            "schema": 1,
            "lines": {
                line: [d.as_dict() for d in items]
                for line, items in sorted(self.lines.items())
                if items
            },
        }
        path = atomic_write_json(self.path, payload)
        self.dirty = False
        return path
