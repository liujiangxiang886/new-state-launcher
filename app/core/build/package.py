"""发布包组装：把底座 exe + 底座 pck + 内容分片 + 启动脚本拼成可脱离启动器的目录。

两种形态：

``目录版``
    ``<name>.exe`` + ``<name>.pck``（底座，同名配对自动加载）+ ``packs/`` + 启动.bat

``单文件版``
    把底座 pck 追加到 exe 尾部（``[PE][pck][u64 size]["GDPC"]``），只留一个 exe；
    内容分片仍在 ``packs/`` 里——3 GB 塞进 exe 既慢又容易被杀软扫描拖死。

内容来源二选一：

- 传入分片 ``packset.json``（推荐，产物不再依赖原版文件）
- 不传则复制原基线包进 ``packs/``（省事，但发布目录会多一个 3 GB 文件）
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config as cfg_mod
from .. import paths, runner
from ..errors import BuildError
from ..fsutil import atomic_write_json, require_space
from ..pck.writer import embed_pck

#: 4.6 是编辑器构建，不会自动加载同名 pck，必须走 --main-pack
NEEDS_MAIN_PACK = {"4.6"}


@dataclass
class PackageSpec:
    line: str
    name: str = "机甲旋风"
    out_dir: Path | None = None
    shards_packset: Path | None = None
    single_file: bool = False
    copy_baseline: bool = True


@dataclass
class PackageResult:
    root: Path
    exe: Path
    packs_dir: Path
    packset: Path
    single_file: bool
    files: list[str] = field(default_factory=list)
    total_bytes: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root.as_posix(),
            "exe": self.exe.as_posix(),
            "packset": self.packset.as_posix(),
            "single_file": self.single_file,
            "files": self.files,
            "total_bytes": self.total_bytes,
            "seconds": round(self.seconds, 1),
        }


def _safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in name).strip() or "机甲旋风"
    return cleaned


def build_package(spec: PackageSpec) -> PackageResult:
    started = time.time()
    cfg = cfg_mod.load()
    line = spec.line
    name = _safe_name(spec.name)
    tag = line.replace(".", "")
    root = Path(spec.out_dir) if spec.out_dir else (paths.BUILDS / f"release-{tag}-{name}")
    root = root.expanduser().resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    packs_dir = root / "packs"
    packs_dir.mkdir(parents=True, exist_ok=True)

    # --- 底座 exe / pck
    host_exe, host_pck = runner.host_paths(line)
    if not host_exe.exists() or not host_pck.exists():
        raise BuildError(f"{line} 运行时缺失，先跑 tools/build_base.py", line=line)

    exe_target = root / f"{name}.exe"
    base_pck_target = root / f"{name}.pck"

    # --- 内容
    shard_meta: dict | None = None
    if spec.shards_packset:
        src_packset = Path(spec.shards_packset)
        if not src_packset.exists():
            raise BuildError(f"分片 packset 不存在：{src_packset}", path=str(src_packset))
        shard_meta = json.loads(src_packset.read_text(encoding="utf-8"))
        packs = shard_meta.get("packs") or []
        if not packs:
            raise BuildError("分片 packset 里没有 packs", path=str(src_packset))
        need = sum(int(p.get("size") or 0) for p in packs)
        require_space(root, need + host_exe.stat().st_size, factor=1.05)
        copied = []
        for index, pack in enumerate(packs):
            src = Path(pack["path"])
            if not src.exists():
                raise BuildError(f"分片缺失：{src}", path=str(src))
            dst = packs_dir / f"pack_{index:03d}.pck"
            shutil.copyfile(src, dst)
            copied.append({"path": dst.as_posix(), "offset": 0, "entries": pack.get("entries", 0),
                           "bytes": pack.get("bytes", 0), "sha256": pack.get("sha256", "")})
        packset_payload = {
            "schema": 1,
            "id": f"{spec.name}-{line}",
            "line": line,
            "entry_count": shard_meta.get("entry_count", 0),
            "total_bytes": shard_meta.get("total_bytes", 0),
            "packs": copied,
        }
    elif spec.copy_baseline:
        bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
        meta = cfg_mod.baseline(cfg, bid)
        src = Path(meta["path"])
        dst = packs_dir / "pack_000.pck"
        require_space(root, src.stat().st_size, factor=1.05)
        shutil.copyfile(src, dst)
        entry = {"path": dst.as_posix(), "offset": 0}
        if meta.get("kind") == "exe":
            from ..pck.reader import locate_pck

            entry["offset"] = locate_pck(src)[0]
        packset_payload = {
            "schema": 1,
            "id": f"{spec.name}-{line}",
            "line": line,
            "entry_count": int(meta.get("entry_count") or 0),
            "total_bytes": src.stat().st_size,
            "packs": [entry],
        }
    else:
        raise BuildError("既没有分片 packset，也没允许复制基线", line=line)

    packset_path = atomic_write_json(packs_dir / "packset.json", packset_payload)

    # --- 底座 pck / exe
    if spec.single_file:
        shutil.copyfile(host_exe, exe_target)
        embed_pck(exe_target, host_pck, exe_target)
        base_name = exe_target.name
    else:
        shutil.copyfile(host_exe, exe_target)
        shutil.copyfile(host_pck, base_pck_target)
        base_name = base_pck_target.name

    # --- 启动脚本
    launcher_cfg = {
        "schema": 1,
        "line": line,
        "main_pack": base_name,
        "single_file": spec.single_file,
        "packset": "packs/packset.json",
        "main_scene": "res://scenes/start_page.tscn",
    }
    atomic_write_json(root / "launcher.json", launcher_cfg)

    main_pack_arg = f' --main-pack "%~dp0{base_name}"' if (line in NEEDS_MAIN_PACK and not spec.single_file) else ""
    bat = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "%~dp0"\r\n'
        f'start "" "%~dp0{exe_target.name}"{main_pack_arg} -- '
        f'--packset="%~dp0packs\\packset.json" --main="res://scenes/start_page.tscn"\r\n'
    )
    (root / "启动.bat").write_text(bat, encoding="utf-8", newline="")

    readme = (
        f"{spec.name} · 发布包（{line}）\r\n"
        f"================================\r\n"
        f"双击「启动.bat」开始游戏；也可以直接运行 {exe_target.name}"
        + ("（单文件版，底座已内嵌）" if spec.single_file else f"（同目录需保留 {base_name}）")
        + "。\r\n\r\n"
        f"内容分片在 packs\\ 下，共 {len(packset_payload['packs'])} 片；"
        f"packs\\packset.json 记录了逐片 sha256，可用于校验完整性。\r\n"
        f"本包不包含启动器功能（扩展管理、剧本切换等需回到启动器）。\r\n"
    )
    (root / "README.txt").write_text(readme, encoding="utf-8", newline="")

    files = sorted(p.name for p in root.iterdir())
    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    return PackageResult(
        root=root,
        exe=exe_target,
        packs_dir=packs_dir,
        packset=packset_path,
        single_file=spec.single_file,
        files=files,
        total_bytes=total,
        seconds=time.time() - started,
    )
