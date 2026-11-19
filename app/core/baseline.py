"""基线索引查询。

``baseline/index-<line>.json.gz`` 是 P0 产出的条目清单，格式见
:meth:`app.core.pck.reader.PckReader.to_index`。

这里提供「某条路径是否在基线里」的快速判断——导入扩展时用它区分
**覆盖既有资源**（必须写自指 remap）与 **新增文件**（无需 remap）。
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

from . import paths


@lru_cache(maxsize=8)
def _suffix_for_line(line: str) -> str:
    """引擎线 → 索引文件后缀。

    索引按**基线版本**命名（如 ``index-1.45``），不是引擎线（``4.6``），
    两者容易混。这里从配置里查一遍，避免硬编码。
    """
    from . import config as cfg_mod

    try:
        cfg = cfg_mod.load()
        for bid, meta in (cfg.get("baselines") or {}).items():
            if str(meta.get("engine")) == line:
                return bid.split(".v")[-1]
    except Exception:  # 配置不可用时退化
        pass
    return line


def index_path(line: str) -> Path:
    return paths.BASELINE / f"index-{_suffix_for_line(line)}.json.gz"


@lru_cache(maxsize=4)
def load_index(line: str) -> dict[str, object]:
    """读整份索引（含 header 与 entries）。结果按引擎线缓存。"""
    p = index_path(line)
    if not p.exists():
        raise FileNotFoundError(f"缺少基线索引：{p}（先跑 python tools/make_index.py）")
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=4)
def path_set(line: str) -> frozenset[str]:
    """基线里全部条目路径的集合。"""
    data = load_index(line)
    return frozenset(row[0] for row in data["entries"])  # type: ignore[index]


def header(line: str) -> dict[str, object]:
    return load_index(line)["header"]  # type: ignore[index]


def entry_count(line: str) -> int:
    return int(load_index(line)["entry_count"])  # type: ignore[index]


def exists_in(line: str, target: str) -> bool:
    return target.replace("\\", "/").lstrip("/") in path_set(line)


def stats(line: str) -> dict[str, object]:
    h = header(line)
    return {
        "line": line,
        "engine": h.get("engine"),
        "entry_count": entry_count(line),
        "index_file": index_path(line).name,
        "index_bytes": index_path(line).stat().st_size if index_path(line).exists() else 0,
    }
