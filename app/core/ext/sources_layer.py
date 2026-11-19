"""源码兜底层：补齐「扩展改写的脚本所依赖、但本体只有 .gdc」的那些 .gd。

背景见 :func:`app.core.runner._insert_sources_layer` 的说明。这里只负责
「算出要补什么、打成 pck、按内容缓存」。

缓存键
------
用「参与计算的 overlay 路径 + 大小 + mtime」+「基线源码层指纹」做哈希。
任何一个扩展换了版本、或者基线源码层变了，键就变，自动重建 —— 不会出现
「包升级了但兜底层还是旧的」这种最难查的错。
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

from .. import config as cfg_mod
from .. import paths
from ..pck.reader import PckReader
from ..pck.writer import PckWriter

_PRELOAD_RE = re.compile(r'preload\("res://([^"]+)"\)')
_EXTENDS_RE = re.compile(
    r"""^[ \t]*extends[ \t]+(?:[A-Za-z_]\w*|"([^"]+)"|'([^']+)')""", re.M
)


def _extends_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """``文件 -> 父类文件``。只收「继承自另一个 .gd」的，内建类型不算。"""
    out: dict[str, str] = {}
    for name in zf.namelist():
        if not name.endswith(".gd"):
            continue
        text = zf.read(name).decode("utf-8", "replace")
        m = _EXTENDS_RE.search(text)
        if not m:
            continue
        parent = (m.group(1) or m.group(2) or "").replace("res://", "")
        if parent:
            out[name] = parent
    return out


def _cache_dir() -> Path:
    d = Path(getattr(paths, "CACHE", Path("cache"))) / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _canonical_zip(line: str) -> Path | None:
    from . import basecontent

    return basecontent.canonical_zip(line)


def _layer_key(pack_paths: list[Path], src_zip: Path) -> str:
    h = hashlib.sha256()
    h.update(str(src_zip).encode("utf-8"))
    try:
        st = src_zip.stat()
        h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
    except OSError:
        pass
    for p in pack_paths:
        h.update(str(p).encode("utf-8"))
        try:
            st = p.stat()
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            pass
    return h.hexdigest()[:20]


def _baseline_has_gd(root: Path, line: str) -> set[str]:
    """本体里以 ``.gd`` **源码**形态存在的脚本（通常是空集）。"""
    idx = Path(getattr(paths, "BASELINE", Path("baseline")))
    cand = idx / f"index-{_line_tag(line)}.json.gz"
    if not cand.is_file():
        return set()
    import gzip

    try:
        data = json.loads(gzip.open(cand, "rt", encoding="utf-8").read())
    except (OSError, ValueError):
        return set()
    return {e[0] for e in data.get("entries", []) if str(e[0]).endswith(".gd")}


def _line_tag(line: str) -> str:
    # 1.45 -> 1.45；4.6 -> 4.6。索引命名用基线版本号，内置线是 1.45。
    return "1.45" if str(line).startswith("4") else str(line)


def collect_needed(
    pack_paths: list[Path], line: str, overlay_paths: list[str] | None = None
) -> tuple[list[str], dict[str, str]]:
    """算出需要补的 .gd 列表，并返回它们的源码内容。

    两类都要补（2026-11-05 补上第二类，这是灰屏的最后一块）：

    1. **preload 依赖**：overlay 里的源码脚本 ``preload`` 的目标。
       本体里只有 ``.gdc``，不补就 ``Preload file ... does not exist``。

    2. **子类**：继承了 overlay 里源码脚本的类。
       本体里那份是 ``.gdc`` 字节码，而字节码类**不能**把「运行时才编译的
       ``.gd`` 源码」当父类 —— 引擎会报
       ``Could not resolve class "res://scripts/xxx.gd"``。

       实测：``opening_run.gd``（本体只有 .gdc）
       ``extends "res://scripts/training_run.gd"``（工作台提供的 .gd 源码）
       → 进主城时 ``game_world.gd:67`` 实例化失败 → 灰屏。

       所以「父类变源码」必须连带「子类也变源码」，否则父子形态不一致。
    """
    src_zip = _canonical_zip(line)
    if src_zip is None or not src_zip.is_file():
        return [], {}
    zf = zipfile.ZipFile(src_zip)
    src_names = {n for n in zf.namelist() if n.endswith(".gd")}
    have_src = _baseline_has_gd(paths.ROOT, line)
    parent_of = _extends_map(zf)

    # overlay 里已有的源码
    provided: dict[str, str] = {}
    for p in pack_paths:
        try:
            r = PckReader.open(p)
        except Exception:  # noqa: BLE001 - 单个包读不了不影响其它包
            continue
        for e in r:
            if e.path.endswith(".gd") and not e.path.endswith(".remap"):
                provided[e.path] = r.read_text(e)

    # 调用方显式点名的（合并层产出的文件等）也算「已以源码形式存在」
    for extra in overlay_paths or []:
        if extra in src_names and extra not in provided:
            provided[extra] = zf.read(extra).decode("utf-8", "replace")

    need: set[str] = set()
    queue = list(provided)
    seen: set[str] = set()
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        text = provided.get(cur)
        if text is None:
            if cur in src_names:
                text = zf.read(cur).decode("utf-8", "replace")
                need.add(cur)
            else:
                continue
        for dep in _PRELOAD_RE.findall(text):
            if not dep.endswith(".gd"):
                continue
            if dep in have_src or dep in provided:
                queue.append(dep)
                continue
            if dep in src_names:
                need.add(dep)
                queue.append(dep)

    # --- 2) 子类：继承链上凡是父类要变源码的，子类也得变源码
    children: dict[str, list[str]] = {}
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)

    # base_src = 将以源码形态存在的集合（overlay 提供的 + 已补的）
    base_src = set(provided) | need
    changed = True
    while changed:
        changed = False
        for parent in list(base_src):
            for child in children.get(parent, []):
                if child in base_src or child in have_src:
                    continue
                if child not in src_names:
                    continue
                base_src.add(child)
                need.add(child)
                changed = True
                # 子类自己也可能 preload 别的东西，一并处理
                ctext = zf.read(child).decode("utf-8", "replace")
                for dep in _PRELOAD_RE.findall(ctext):
                    if not dep.endswith(".gd"):
                        continue
                    if dep in have_src or dep in base_src:
                        continue
                    if dep in src_names:
                        base_src.add(dep)
                        need.add(dep)
                        changed = True

    todo = sorted(n for n in need if n not in have_src and n in src_names)
    contents = {
        n: (provided.get(n) or zf.read(n).decode("utf-8", "replace")) for n in todo
    }
    return todo, contents


def ensure_layer(packs: list[dict], line: str) -> Path | None:
    """返回（必要时生成）源码兜底层 pck 的路径；不需要时返回 None。"""
    pack_paths = [
        Path(str(p.get("path")))
        for p in packs
        if str(p.get("role") or "") in ("mod", "story")
        and str(p.get("path") or "").endswith(".pck")
    ]
    if not pack_paths:
        return None

    src_zip = _canonical_zip(line)
    if src_zip is None:
        return None

    #: 算法版本。改动 collect_needed 的判定逻辑时必须+1，
    #: 否则会命中按旧逻辑生成的缓存，出现「代码改了但行为没变」的假象。
    #: v2: 加入「子类连带」（父类变源码时，子类也必须变源码）
    _ALGO = "v2"

    key = _layer_key(pack_paths, src_zip) + f"-{_ALGO}"
    dest = _cache_dir() / f"layer-{_line_tag(line)}-{key}.pck"
    if dest.is_file():
        return dest

    # 合并层产出的 .gd 也是「源码形态」，必须一起参与判定，
    # 否则它的父类/子类关系会被漏掉。
    extra_src: list[str] = []
    for p in packs:
        if str(p.get("role") or "") != "merged":
            continue
        mp = Path(str(p.get("path") or ""))
        if not mp.is_file():
            continue
        try:
            mr = PckReader.open(mp)
        except Exception:  # noqa: BLE001
            continue
        extra_src += [
            e.path for e in mr if e.path.endswith(".gd") and not e.path.endswith(".remap")
        ]

    todo, contents = collect_needed(pack_paths, line, extra_src)
    if not todo:
        return None

    writer = PckWriter(dest, line=str(line))
    for path in todo:
        writer.add_text(path, contents[path])
        # 自指 remap：盖掉本体那条指向 .gdc 的 remap，否则引擎仍去读字节码
        writer.add_remap(path)
    writer.commit()
    return dest