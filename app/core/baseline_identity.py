"""分层基线校验：说清「这台机器上跑的是不是我们认的那份」。

为什么需要
==========

4.6 的校验只有一条：``size`` 对不对。它对 4.6 够用（内容冻结、玩家群体
稳定、改动风险低），但对 4.7 不够：

- 4.7 有三层结构（本体 pck + 引擎 exe + 剧情/代码层），任一层被换掉
  都会出问题，而 ``size`` 只能发现「整包换了」；
- 玩家换的是**剧情包**（160 MB 的子集），不是整个 2.9 GB 本体 ——
  ``size`` 完全发现不了；
- 扩展的锚点是按**代码层**算的，代码层一变旧扩展就失配，这件事必须
  能精确判定「是哪个脚本变了」。

分层
====

| 层 | 是什么 | 粒度 | 谁会改它 |
|---|---|---|---|
| ``body`` | 本体 ``.pck`` | 整包 sha256 | 玩家换版本 / 换整合包 |
| ``exe`` | 引擎可执行文件 | 整包 sha256 | 几乎没人改 |
| ``code`` | 包内 ``.gd`` + ``.tscn`` | **逐文件** sha256 | 剧情更新、脚本修复 |

``code`` 层逐文件是为了两个用途：一是能指出「哪个脚本变了」（比「整包
不匹配」有用得多），二是给出一个 ``code_layer.digest`` 供验证标志绑定
（见 :mod:`app.core.ext.verify_mark`）。

stamp 快检
==========

整包 sha256 要读 2.9 GB，实测约 8~10 秒。每次启动都算等于每次多等 10 秒，
这是不可接受的。所以用 **stamp**（路径 + 大小 + mtime_ns）做快检：

1. 有 stamp 且与当前文件 stat 一致 → **信任 stamp，跳过哈希**（毫秒级）；
2. stamp 缺失或 stat 变了 → 老老实实算哈希，然后**重写 stamp**。

「信任 stamp」的安全性边界要说清：有人可以同时改文件内容并把 mtime 改回去
（``os.utime``），那样 stamp 会误判。这是**刻意的取舍** —— 我们要防的是
「玩家换了本体/剧情包」，不是「有人精心伪造以绕过校验」。真要防伪造
必须上签名，那是另一件事（而且离线签名只能防篡改，不能防「文件本身
就是假的」）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths
from .fsutil import atomic_write_json, read_json
from .pck.reader import PckReader

#: identity 文件的路径（按基线 id）。
def identity_path(baseline_id: str) -> Path:
    """identity 文件路径。

    命名规则取自现有约定：``jjxf.v1.45`` → ``identity-1.45.json``，
    ``jjxf.v4.7`` → ``identity-4.7.json``。也就是去掉 ``jjxf.v`` 前缀，
    而不是只去掉 ``jjxf.`` —— 后者会得到 ``v1.45``，与现存文件名不符。
    """
    slug = baseline_id
    for prefix in ("jjxf.v", "jjxf."):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    return paths.BASELINE / f"identity-{slug}.json"


#: stamp 放哪。放在 CACHE 下而不是 BASELINE：它是本机派生数据，
#: 换台机器就该重算，不该跟着基线文件走。
def _stamp_path(baseline_id: str) -> Path:
    return paths.CACHE / "baseline-stamp" / f"{baseline_id}.json"


#: 逐文件哈希的块大小。8 MB 在本机实测比 1 MB 快约 15%。
_CHUNK = 8 * 1024 * 1024

#: 参与代码层指纹的条目后缀。
CODE_SUFFIXES = (".gd", ".tscn")


@dataclass
class LayerCheck:
    """某一层的校验结果。"""

    name: str
    ok: bool = True
    #: 期望值 vs 实测值，用于错误消息
    expected: str = ""
    actual: str = ""
    #: ``hash`` / ``stamp`` / ``missing`` —— 这一轮是怎么判的
    mode: str = ""
    reason: str = ""
    #: 代码层专有：变了哪些文件
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "layer": self.name,
            "ok": self.ok,
            "mode": self.mode,
            "reason": self.reason,
        }
        if self.changed:
            out["changed"] = self.changed[:20]
        if self.added:
            out["added"] = self.added[:20]
        if self.removed:
            out["removed"] = self.removed[:20]
        return out


@dataclass
class IdentityCheck:
    """整条基线的校验结果。"""

    baseline_id: str
    line: str
    ok: bool = True
    layers: list[LayerCheck] = field(default_factory=list)
    #: 一眼能看懂的结论
    message: str = ""
    #: 所有层都靠 stamp 过的（说明没读大文件）
    fast: bool = False

    def failing(self) -> list[LayerCheck]:
        return [x for x in self.layers if not x.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline_id,
            "line": self.line,
            "ok": self.ok,
            "fast": self.fast,
            "message": self.message,
            "layers": [x.as_dict() for x in self.layers],
        }


# ---------------------------------------------------------------- 读取


def load_identity(cfg: dict, line: str) -> dict | None:
    """读这条线的 identity。不存在返回 None。

    路径优先取 ``cfg['baselines'][bid]['identity']``（允许自定义基线自带
    identity），否则按 id 推默认文件名。
    """
    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    meta = (cfg.get("baselines") or {}).get(bid) or {}
    raw = str(meta.get("identity") or "").strip()
    path = cfg_mod.resolve_path(raw) if raw else identity_path(bid)
    if path is None or not path.is_file():
        return None
    data = read_json(path, default=None)
    return data if isinstance(data, dict) else None


def code_digest_of(cfg: dict, line: str) -> str:
    """这条线的**代码层**指纹。

    验证标志（:mod:`app.core.ext.verify_mark`）绑的就是它：脚本一变，
    所有「按旧代码验过」的记录同时失效。

    没有 identity 时返回空串。**空串不等于「哪版都算通过」** ——
    调用方必须把空串当成「无法判定」，不要当成匹配。
    """
    ident = load_identity(cfg, line)
    if not ident:
        return ""
    layer = ident.get("code_layer") or {}
    return str(layer.get("digest") or "")


# ---------------------------------------------------------------- 哈希


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _stat_key(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _stamp_matches(baseline_id: str, layers: dict[str, Path]) -> dict[str, Any] | None:
    """stamp 是否仍然对得上这些文件。对得上就返回 stamp，否则 None。"""
    data = read_json(_stamp_path(baseline_id), default=None)
    if not isinstance(data, dict):
        return None
    recorded = data.get("layers") or {}
    for name, path in layers.items():
        slot = recorded.get(name)
        if not isinstance(slot, dict):
            return None
        try:
            now = _stat_key(path)
        except OSError:
            return None
        if (
            slot.get("path") != now["path"]
            or slot.get("size") != now["size"]
            or slot.get("mtime_ns") != now["mtime_ns"]
        ):
            return None
    return data


def _write_stamp(baseline_id: str, layers: dict[str, Path], result: IdentityCheck) -> None:
    payload: dict[str, Any] = {"schema": 1, "baseline": baseline_id, "layers": {}}
    for name, path in layers.items():
        try:
            payload["layers"][name] = _stat_key(path)
        except OSError:
            continue
    payload["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload["fast"] = False
    try:
        atomic_write_json(_stamp_path(baseline_id), payload)
    except OSError:
        pass


# ---------------------------------------------------------------- 代码层


def scan_code_layer(pck: Path, expected_files: dict[str, dict]) -> tuple[str, dict[str, dict], LayerCheck]:
    """扫包内代码层，返回 ``(digest, 逐文件表, 差异检查结果)``。"""
    check = LayerCheck(name="code", mode="hash")
    actual: dict[str, dict] = {}
    reader = PckReader.open(pck)
    for entry in reader.entries:
        if not entry.path.endswith(CODE_SUFFIXES):
            continue
        data = reader.read_bytes(entry)
        actual[entry.path] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }

    blob = "".join(f"{k}:{v['sha256']};" for k, v in sorted(actual.items()))
    digest = hashlib.sha256(blob.encode()).hexdigest()

    if expected_files:
        want = {k: v.get("sha256") for k, v in expected_files.items()}
        got = {k: v["sha256"] for k, v in actual.items()}
        check.changed = sorted(k for k in want.keys() & got.keys() if want[k] != got[k])
        check.added = sorted(got.keys() - want.keys())
        check.removed = sorted(want.keys() - got.keys())
        check.ok = not (check.changed or check.added or check.removed)
        if not check.ok:
            parts = []
            if check.changed:
                parts.append(f"{len(check.changed)} 个脚本被改")
            if check.added:
                parts.append(f"{len(check.added)} 个新增")
            if check.removed:
                parts.append(f"{len(check.removed)} 个缺失")
            check.reason = "代码层与登记不符：" + "、".join(parts)
    return digest, actual, check


# ---------------------------------------------------------------- 主入口


def verify(cfg: dict, line: str, *, force_hash: bool = False) -> IdentityCheck:
    """校验这条线的基线。

    ``force_hash=True`` 时忽略 stamp，强制整包重算（「重新校验」按钮用）。

    没有 identity 文件时返回 ``ok=False`` 且消息说明「未登记」——
    调用方（``runner`` / ``readiness``）据此决定是阻断还是只提示。
    """
    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    result = IdentityCheck(baseline_id=bid, line=line)

    ident = load_identity(cfg, line)
    if not ident:
        result.ok = False
        result.message = (
            f"{line} 基线还没有登记身份指纹，无法校验。"
            "请在「诊断」页跑一次「登记基线」，或重新导入游戏本体。"
        )
        return result

    meta = (cfg.get("baselines") or {}).get(bid) or {}
    layers_spec = ident.get("layers") or {}

    # 实际文件路径：优先用配置里的（玩家导入后的位置），其次 identity 里的。
    #
    # 为什么两边都要看：identity 是「出厂/冻结时」的相对路径（可能指向
    # 打包机上的位置），配置里才是玩家这台机器上真正那份。玩家重新导入
    # 本体后，配置路径更新、identity 不变 —— 这时候必须用配置的。
    files: dict[str, Path] = {}
    for name, slot in layers_spec.items():
        if not isinstance(slot, dict):
            continue
        candidates: list[str] = []
        # body 层沿用 baselines.<id>.path（历史字段），其余层用 <name>_path
        if name == "body":
            candidates.append(str(meta.get("path") or ""))
        candidates.append(str(meta.get(f"{name}_path") or ""))
        candidates.append(str(slot.get("path") or ""))

        found: Path | None = None
        tried: list[str] = []
        for raw in candidates:
            text = raw.strip()
            if not text:
                continue
            tried.append(text)
            resolved = cfg_mod.resolve_path(text)
            if resolved is not None and resolved.is_file():
                found = resolved
                break
        if found is not None:
            files[name] = found
        else:
            # 说清「缺什么 + 怎么办」。只写「文件不存在」，玩家不知道该做什么；
            # 本体在 D 盘某个目录里躺着的玩家尤其需要这句话。
            result.layers.append(
                LayerCheck(
                    name=name,
                    ok=False,
                    mode="missing",
                    reason=(
                        f"缺少 {name} 层文件（{tried[0] if tried else '未配置路径'}）。"
                        "请点「游戏库 → 导入游戏本体」选中这份 4.7 的 pck 与 exe。"
                    ),
                )
            )

    if not files:
        result.ok = False
        result.message = "找不到这条基线的任何文件，请重新导入本体。"
        return result

    # ---- stamp 快检
    stamp = None if force_hash else _stamp_matches(bid, files)
    if stamp is not None:
        result.fast = True
        result.ok = all(layer.ok for layer in result.layers)  # 已判缺失的层仍然算失败
        for name in files:
            result.layers.append(LayerCheck(name=name, ok=True, mode="stamp"))
        if not result.ok:
            missing = [x.name for x in result.failing()]
            result.message = f"缺少 {('、'.join(missing))} 层文件，请重新导入本体。"
        else:
            result.message = "基线指纹与登记一致（快速检查）"
        return result

    # ---- 老老实实算
    for name, slot in layers_spec.items():
        if name not in files:
            continue
        path = files[name]
        want = str((slot or {}).get("sha256") or "")
        check = LayerCheck(name=name, mode="hash", expected=want)

        try:
            actual = _sha256_file(path)
        except OSError as exc:
            check.ok = False
            check.reason = f"读取失败：{exc}"
            result.layers.append(check)
            continue
        check.actual = actual
        check.ok = (not want) or (actual == want)
        if not check.ok:
            check.reason = f"{name} 层被替换过（登记 {want[:12]}…，实际 {actual[:12]}…）"
        result.layers.append(check)

    # ---- 代码层：从 source 指定的那一层里扫
    code_layer = ident.get("code_layer") or {}
    if code_layer:
        source = str(code_layer.get("source") or "body")
        host = files.get(source)
        check = LayerCheck(name="code", mode="hash")
        if host is None:
            check.ok = False
            check.reason = f"代码层来源 {source} 不可用"
        else:
            expected_files = code_layer.get("files") or {}
            digest, _actual, diff = scan_code_layer(host, expected_files)
            want_digest = str(code_layer.get("digest") or "")
            check.expected = want_digest
            check.actual = digest
            check.ok = diff.ok and (not want_digest or digest == want_digest)
            if not check.ok:
                check.changed = diff.changed
                check.added = diff.added
                check.removed = diff.removed
                check.reason = diff.reason or "代码层指纹与登记不符"
        result.layers.append(check)

    result.ok = all(layer.ok for layer in result.layers)
    if result.ok:
        result.message = "基线指纹与登记一致"
        _write_stamp(bid, files, result)
    else:
        bad = result.failing()
        # 代码层的错误消息要指出是哪个脚本，比「整包不符」有用得多
        parts = []
        for layer in bad:
            if layer.name == "code" and (layer.changed or layer.added or layer.removed):
                sample = (layer.changed or layer.added or layer.removed)[:3]
                parts.append(f"代码层变动（{'、'.join(sample)}）")
            elif layer.reason:
                parts.append(layer.reason)
        result.message = "；".join(parts) or "基线校验未通过"
    return result


def describe(check: IdentityCheck) -> str:
    """给界面/日志用的多行说明。"""
    lines = [check.message or ("基线校验通过" if check.ok else "基线校验未通过")]
    for layer in check.layers:
        mark = "OK" if layer.ok else "NG"
        extra = f" [{layer.mode}]" if layer.mode else ""
        lines.append(f"  [{mark}] {layer.name}{extra}" + (f" — {layer.reason}" if layer.reason else ""))
    return "\n".join(lines)