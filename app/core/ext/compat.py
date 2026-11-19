"""扩展包与引擎线的兼容性探测。

一个扩展包常常声明某条线（``baseline: jjxf.v1.45.original`` → 4.6）。
能不能装到目标线，取决于扩展的**补丁**：

- **文件覆盖类**（只往包里加资源 / 换资源实现）：与引擎线无关，
  只要目标路径在目标线基线里能落位就没问题；
- **补丁类**（``text_patches`` / ``data_patches``）：``find`` 是原版脚本里的
  字面量，换了游戏版本就可能找不到。找不到就等于「这个补丁没生效」，
  而扩展往往依赖这个补丁才能正常工作。

所以兼容性的判据是：**补丁在目标基线里能不能全部命中**。
本模块只做静态比对，不写盘、不装包，因此很快，可以用于批量评估。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .basecontent import BaseContent
from . import patcher
from ..baseline import path_set
from ..pck.remap import needs_remap

#: 兼容性结论
COMPATIBLE = "compatible"
PARTIAL = "partial"
INCOMPATIBLE = "incompatible"
UNKNOWN = "unknown"

VERDICT_TEXT = {
    COMPATIBLE: "兼容",
    PARTIAL: "部分兼容",
    INCOMPATIBLE: "不兼容",
    UNKNOWN: "待查",
}


@dataclass
class ProbeResult:
    """一次兼容性探测的结果。"""

    ext_id: str = ""
    name: str = ""
    version: str = ""
    line: str = ""

    files: int = 0
    hooks: int = 0
    data_patches: int = 0
    text_patches: int = 0

    #: 补丁命中数 / 应命中数
    patch_ok: int = 0
    patch_total: int = 0
    #: 覆盖类：落在基线既有资源上的 / 新增的
    override_hit: int = 0
    override_new: int = 0
    #: 需要自指 remap 的条目数（写对了引擎才会读新字节）
    remap_needed: int = 0

    #: 目标基线只有 ``.gdc`` 字节码、没有 ``.gd`` 源码（补丁类扩展全都要落空）。
    #: 单独成字段，是为了让界面能给出**一句**能照做的结论，而不是让玩家
    #: 自己从十几条「目标基线里没有这个文件」里猜。
    sources_missing: bool = False

    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.error:
            return UNKNOWN
        if self.patch_total == 0:
            # 没有补丁 → 只做文件覆盖，与引擎线无关
            return COMPATIBLE
        if not self.failures:
            return COMPATIBLE
        if self.patch_ok == 0:
            return INCOMPATIBLE
        return PARTIAL

    def as_dict(self) -> dict:
        return {
            "id": self.ext_id,
            "name": self.name,
            "version": self.version,
            "line": self.line,
            "files": self.files,
            "hooks": self.hooks,
            "data_patches": self.data_patches,
            "text_patches": self.text_patches,
            "patch_ok": self.patch_ok,
            "patch_total": self.patch_total,
            "override_hit": self.override_hit,
            "override_new": self.override_new,
            "remap_needed": self.remap_needed,
            "sources_missing": self.sources_missing,
            "failures": list(self.failures),
            "notes": list(self.notes),
            "error": self.error,
            "verdict": self.verdict,
            "verdict_text": VERDICT_TEXT[self.verdict],
        }


def _source_hint(bc, target: str) -> str:
    """补丁目标取不到原文时，补一句「为什么、怎么办」。

    4.7 有不带源码的导出构建（pck 里只有 ``.gdc``）。那种本体上所有文本补丁
    都会取不到原文，而旧文案只说「没有这个文件」，玩家会以为是包坏了。
    """
    if not target.endswith(".gd"):
        return ""
    try:
        if bc.compiled_only(target):
            return "（基线里只有 .gdc 字节码，没有 .gd 源码；该版本本体不带源码，补丁无法应用）"
    except Exception:  # noqa: BLE001 - 诊断失败不影响主流程
        return ""
    return ""


def probe(package: str | Path, line: str, cfg: dict | None = None) -> ProbeResult:
    """把一个扩展包放到指定引擎线上做静态试装，返回兼容性诊断。"""
    from . import manifest as ext_manifest

    out = ProbeResult(line=line)

    try:
        m = ext_manifest.load(package)
    except Exception as exc:  # noqa: BLE001
        out.error = f"读清单失败：{exc}"
        return out

    out.ext_id = m.id
    out.name = m.data.get("name", "")
    out.version = m.data.get("version", "")
    out.files = len(m.files)
    out.hooks = len(m.method_hooks)
    out.data_patches = len(m.data.get("data_patches") or [])
    out.text_patches = len(m.data.get("text_patches") or [])

    try:
        paths = path_set(line)
    except FileNotFoundError as exc:
        out.error = f"基线索引不可用：{exc}"
        return out

    # 排除「正在探测的这个包自己」：它可能已经装过一版，覆盖层里的文件是
    # **打过补丁之后**的内容。不排除的话，第二次探测会把自己的产物当原文，
    # 报出「命中 0 处」这种和第一次互相矛盾的结论。
    bc = BaseContent(cfg, line, exclude=out.ext_id)

    # --- 文件覆盖：能不能落到目标路径
    for rec in m.files:
        target = rec["target"]
        if needs_remap(target, paths).needed:
            out.remap_needed += 1
            if target in paths or f"{target}.remap" in paths:
                out.override_hit += 1
            else:
                out.override_new += 1
                if len(out.notes) < 8:
                    out.notes.append(f"{target}：基线里没有这个路径，将作为新资源加入")
        else:
            out.override_new += 1

    # --- 文本补丁：字面量在目标基线里命中几处
    for patch in m.data.get("text_patches") or []:
        target = patch.get("target", "")
        find = patch.get("find", "")
        expect = int(patch.get("expect_count") or 1)
        out.patch_total += 1
        if not find:
            out.failures.append(f"{target}：补丁没有 find 内容")
            continue
        raw = bc.read(target)
        if raw is None:
            if bc.compiled_only(target):
                out.sources_missing = True
            out.failures.append(f"{target}：目标基线里没有这个文件{_source_hint(bc, target)}")
            continue
        # 换行归一：4.7 的 .gd 是 CRLF，而清单里的锚点基本是 LF。
        # 这里过去用朴素 ``text.count(find)``，多行锚点在 CRLF 文件上恒为 0，
        # 于是预检报「命中 0 处」，而真正的 ``patcher.apply_text``（有 \r?\n
        # 容错）装上去是能命中的 —— 纯粹的假警报，把好包说成半残。
        text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
        find = str(find or "").replace("\r\n", "\n")
        scope = str(patch.get("scope") or "").strip()
        if scope:
            # 作用域锚点：只在指定函数里数。口径必须和真正应用补丁时一致，
            # 否则预检说「命中 2 处」而实际能正常应用，会把好包判死。
            start, end, error = patcher.scope_region(text, scope)
            if error:
                out.failures.append(f"{target}：{error}")
                continue
            text = text[start:end]
        if patch.get("mode") == "regex":
            try:
                hits = len(re.findall(find, text))
            except re.error as exc:
                out.failures.append(f"{target}：正则非法 {exc}")
                continue
        else:
            hits = text.count(find)
        if hits == expect:
            out.patch_ok += 1
        else:
            out.failures.append(f"{target}：命中 {hits} 处，期望 {expect} 处")

    # --- 数据补丁：目标文件在不在
    for patch in m.data.get("data_patches") or []:
        target = patch.get("target", "")
        out.patch_total += 1
        raw = bc.read(target)
        if raw is None:
            out.failures.append(f"{target}（data）：目标基线里没有这个文件")
            continue
        out.patch_ok += 1

    return out


def probe_bundle(package: str | Path, line: str, cfg: dict | None = None) -> list[ProbeResult]:
    """对合集包里的每个成员分别探测。"""
    import io
    import tempfile
    import zipfile

    from .service import detect_bundle
    from .. import paths

    members = detect_bundle(package)
    if not members:
        return [probe(package, line, cfg)]

    results: list[ProbeResult] = []
    outer = zipfile.ZipFile(package)
    try:
        with tempfile.TemporaryDirectory(dir=str(paths.temp_dir())) as tmp:
            for index, name in enumerate(members, start=1):
                member = Path(tmp) / f"{index:02d}.zip"
                member.write_bytes(outer.read(name))
                results.append(probe(member, line, cfg))
    finally:
        outer.close()
    return results
