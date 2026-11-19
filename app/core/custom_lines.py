"""自定义游戏线：让玩家把自己手上的改版游戏登记进来，由启动器统一启动。

两种形态（这是本模块最重要的分野）
----------------------------------

| ``kind`` | 玩家选的文件 | 启动方式 | 扩展 |
|---|---|---|---|
| ``exe`` | 改版自带的完整可执行文件 | **直接执行它**，参数原样透传 | 不加载 |
| ``pck`` | 只有资源包、没有可用 exe | 用**启动器底座引擎**挂载这个 pck | 不加载 |

``exe`` 形态针对「改版把数据内嵌进 exe、自带运行时」的情况：启动器只负责
执行 + 存档隔离，绝不介入它的资源挂载。

``pck`` 形态针对「作者只发了 .pck、或它的 exe 是未改动的 Godot 导出模板」
的情况。这类包自己没有可用的运行时，必须借底座引擎：

    底座 exe + 底座 pck（提供 project.godot 与 _compat）
      → --packset 列出玩家那个 pck
      → 底座挂载它，再切到 --main 指定的场景

为什么 pck 形态**不**走 ``enable.build_for_launch``
---------------------------------------------------

那条链路里 ``resolver`` / ``readiness`` / ``mods.json`` 都是按内置线
（内置的 ``"4.6"``）硬编码引擎表写的，第三方的 pck 进去会得到
「扩展装错线」的混乱结果。所以 pck 形态只复用 ``runner`` 的**启动原语**
（host_paths / make_packset / 同名配对规则），不碰扩展装配。

接入闸门（v0.2）
----------------
自定义线登记时必须**按文件内容自证身份**：只有确认是 Godot 的机甲旋风改版
本体才放行（见 ``identity_gate.probe``，内置 1.45 正典特征库，不采信创作者
声明）。冒名包、别的 Godot 游戏、扩展覆盖层一律拒绝登记。

放行后默认 ``access_tier="probe-only"``（仅启动 + 只读探测）；创作者随改版
提供 ``line.json`` 描述与反解工程索引、并通过一致性校验后，才升级到
``full``（可协商挂载扩展）。源码、反解工程、能力索引都由创作者自备，
启动器不逆向、不猜、不补全。

数据存放
--------

写在 ``config/launcher.json`` 的 ``custom_lines`` 数组里（跟着配置走，
换机器拷目录即生效）。不单独建文件，避免又多一个要同步的路径。

字段演进
--------

早期版本只有 ``exe`` 字段（且只支持 exe 形态）。现在字段叫 ``path``，
``from_dict`` 仍接受旧键 ``exe``——老配置读进来会自动当成 ``kind="exe"``，
下次保存时写成新格式，不需要玩家手工迁移。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: id 前缀，便于和内置线（"4.6"）区分
ID_PREFIX = "custom:"

#: 直接执行改版自带的 exe
KIND_EXE = "exe"
#: 用底座引擎挂载 .pck
KIND_PCK = "pck"
KINDS = (KIND_EXE, KIND_PCK)

#: pck 形态的默认主场景（机甲旋风系列沿用这个路径；可在界面上改）
DEFAULT_MAIN_SCENE = "res://scenes/start_page.tscn"

#: 接入分档：仅启动 / 自证身份但未接描述 / 描述校验通过可协商扩展
TIER_LAUNCH_ONLY = "launch-only"
TIER_PROBE_ONLY = "probe-only"
TIER_FULL = "full"

#: 身份判定缓存：改个名字不该重扫 3GB 本体。键含 (size, mtime_ns)，
#: exe 形态还会把配对 pck 的 stat 揉进来。
_IDENTITY_CACHE: dict[str, tuple[tuple[int, int], object]] = {}


def _probe_identity(path: Path):
    """扫内容自证身份；带缓存，避免改个名字就重扫 2.9GB 的本体。"""
    from . import identity_gate

    try:
        st = path.stat()
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        key = (-1, -1)
    # exe 无内嵌 pck 时会配对旁边同名 pck；那个文件变了缓存也要失效
    sibling = path.with_suffix(".pck")
    if path.suffix.lower() == ".exe" and sibling.is_file():
        try:
            ss = sibling.stat()
            key = (key[0] + ss.st_size, key[1] ^ ss.st_mtime_ns)
        except OSError:
            pass
    cache_key = str(path).lower()
    hit = _IDENTITY_CACHE.get(cache_key)
    if hit and hit[0] == key:
        return hit[1]
    verdict = identity_gate.probe(path)
    _IDENTITY_CACHE[cache_key] = (key, verdict)
    return verdict


def _apply_identity(line: "CustomLine", verdict) -> None:
    """写闸门结果，并按创作者提供的 line.json 决定接入分档。

    换文件后旧的身份指纹与分档必须一起失效；描述校验不过只降级、不阻断启动。
    """
    from . import line_desc

    line.identity_hash = verdict.identity_hash
    line.identity_engine_line = verdict.engine_line
    line.identity_baseline = verdict.baseline
    tier, note = line_desc.try_upgrade(line.path, verdict)
    line.access_tier = TIER_FULL if tier == line_desc.TIER_FULL else TIER_PROBE_ONLY
    line.access_note = note


def _norm_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in KINDS else KIND_EXE


@dataclass
class CustomLine:
    """一条自定义游戏线。"""

    id: str
    name: str
    #: ``exe``（直接执行）或 ``pck``（底座引擎挂载）
    kind: str = KIND_EXE
    #: 游戏文件路径：exe 形态是 .exe，pck 形态是 .pck
    path: str = ""
    #: 仅 pck 形态：用哪条底座引擎线（目前只有 "4.6"）
    engine_line: str = ""
    #: 仅 pck 形态：挂载完切哪个场景（留空用默认）
    main_scene: str = ""
    #: 可选的启动参数（空格分隔的简单场景足够；复杂的不做，避免自己造命令行解析）
    args: str = ""
    #: 存档隔离目录名；空则用 ``custom_<id后缀>``
    profile: str = ""
    #: 归属分类 id（见 ``categories.py``）；空表示"未分类"
    category: str = ""
    added_at: str = ""
    #: 备注，玩家自己看
    note: str = ""

    # ---- 接入框架：身份闸门结果与接入分档（旧配置缺这些字段时留空）
    #: 内容判定出的身份指纹，用于绑定创作者的 line.json 描述
    identity_hash: str = ""
    #: 按 pack_version 判定的引擎线（比按文件头猜的更可信）
    identity_engine_line: str = ""
    #: 判定所用的内置基准（jjxf.v1.45）
    identity_baseline: str = ""
    #: launch-only / probe-only / full —— 见模块开头「接入闸门」
    access_tier: str = ""
    #: 升级/降级原因（描述校验没过时要能说清为什么没生效）
    access_note: str = ""

    # ---------------------------------------------------------------- 路径

    @property
    def file_path(self) -> Path:
        return Path(self.path)

    @property
    def file_name(self) -> str:
        try:
            return self.file_path.name
        except (OSError, ValueError):
            return self.path or "（未设置文件）"

    @property
    def exists(self) -> bool:
        try:
            return self.file_path.is_file()
        except (OSError, ValueError):
            return False

    @property
    def is_pck(self) -> bool:
        return self.kind == KIND_PCK

    #: 兼容旧调用点：以前字段就叫 ``exe``。新代码用 ``path`` / ``file_path``。
    @property
    def exe(self) -> str:
        return self.path

    def profile_name(self) -> str:
        """存档隔离用的目录名（唯一且文件系统安全）。"""
        raw = self.profile.strip() or f"custom_{self.id.split(':', 1)[-1][:8]}"
        return _safe_name(raw)

    def main_scene_or_default(self) -> str:
        return self.main_scene.strip() or DEFAULT_MAIN_SCENE

    # ---------------------------------------------------------------- 序列化

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "engine_line": self.engine_line,
            "main_scene": self.main_scene,
            "args": self.args,
            "profile": self.profile,
            "category": self.category,
            "added_at": self.added_at,
            "note": self.note,
            "identity_hash": self.identity_hash,
            "identity_engine_line": self.identity_engine_line,
            "identity_baseline": self.identity_baseline,
            "access_tier": self.access_tier,
            "access_note": self.access_note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CustomLine":
        # 旧键 ``exe`` 兜底：早期版本只有这一个路径字段
        raw_path = data.get("path")
        if not raw_path:
            raw_path = data.get("exe")
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            kind=_norm_kind(data.get("kind")),
            path=str(raw_path or ""),
            engine_line=str(data.get("engine_line") or ""),
            main_scene=str(data.get("main_scene") or ""),
            args=str(data.get("args") or ""),
            profile=str(data.get("profile") or ""),
            category=str(data.get("category") or ""),
            added_at=str(data.get("added_at") or ""),
            note=str(data.get("note") or ""),
            identity_hash=str(data.get("identity_hash") or ""),
            identity_engine_line=str(data.get("identity_engine_line") or ""),
            identity_baseline=str(data.get("identity_baseline") or ""),
            access_tier=str(data.get("access_tier") or ""),
            access_note=str(data.get("access_note") or ""),
        )


def _safe_name(value: str) -> str:
    """转成 Windows 可用的目录名（去掉非法字符与控制字符）。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned or "custom"


def is_custom_id(line_id: str) -> bool:
    return str(line_id).startswith(ID_PREFIX)


def load_all(cfg: dict) -> list[CustomLine]:
    """读出全部自定义线（按添加顺序）。数据坏了就跳过那一条，不整块失败。"""
    raw = cfg.get("custom_lines")
    if not isinstance(raw, list):
        return []
    out: list[CustomLine] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        line = CustomLine.from_dict(item)
        if not line.id or not is_custom_id(line.id) or line.id in seen:
            continue
        seen.add(line.id)
        out.append(line)
    return out


def save_all(cfg: dict, lines: list[CustomLine]) -> None:
    cfg["custom_lines"] = [line.as_dict() for line in lines]


def new_id() -> str:
    return f"{ID_PREFIX}{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------- 校验


def normalized_engine_line(cfg: dict, value: str, pack: Path | None = None) -> str:
    """把 ``engine_line`` 收敛成配置里真实存在的引擎线。

    pck 形态必须借一条底座引擎，随便填会在启动时才炸。这里直接拒绝，
    并把可选项写进报错里，玩家不用去翻配置。

    **没填时不再「挑最高版本」**。那个规则在只有 4.6 一条线时看不出问题，
    加上 4.7 之后就成了坑：市面上绝大多数 pck 是 4.6 导出的，自动选 4.7
    会让它们一启动就报「pck 版本不兼容」。现在改成：

    1. 给了 ``pack``（pck 路径）→ 读它头部的 ``engine_version``，
       匹配主次号相同的那条线；
    2. 读不出来或匹配不上 → 回落内置线（``BUILTIN_LINE``）；
    3. 玩家显式指定的值仍然优先。
    """
    engines = [str(k) for k in (cfg.get("engines") or {})]
    text = str(value or "").strip()
    if text and text in engines:
        return text
    if text:
        # 显式写了但不存在 → 报错，不要悄悄改
        raise ValueError(
            f"引擎线「{text}」不可用。可选的只有：{'、'.join(engines) or '（配置里没有任何引擎线）'}"
        )

    if pack is not None:
        guessed = engine_line_for_pack(pack, engines)
        if guessed:
            return guessed
    if not engines:
        raise ValueError("配置里没有任何引擎线，无法为 pck 指定底座")

    # 延迟导入：本模块被 UI / CLI 独立调用，顶层导入 config 会牵扯包装顺序
    from . import config as cfg_mod

    if cfg_mod.BUILTIN_LINE in engines:
        return cfg_mod.BUILTIN_LINE
    return sorted(engines)[0]


def engine_line_for_pack(pack: Path, engines: list[str]) -> str:
    """按 pck 头部的引擎版本猜引擎线；猜不出返回空串。

    只比主次号（``4.7.2`` → ``4.7``），因为配置里的线名就是主次号形式。
    """
    try:
        from .pck.reader import PckReader

        header = PckReader.open(pack).header
        version = str(getattr(header, "engine", "") or "")
    except Exception:  # noqa: BLE001 - 读不出来就交给兜底，不该因此报错
        return ""
    if not version:
        return ""
    major_minor = ".".join(version.split(".")[:2])
    return major_minor if major_minor in engines else ""


def _validate_file(path: Path, kind: str):
    """校验后缀 + 按内容自证身份；通过返回 Verdict，失败抛 ValueError。

    身份闸门是硬条件：创作者在界面上填什么名字、什么能力都不参与判定。
    """
    if not path.is_file():
        raise ValueError(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    if kind == KIND_EXE and suffix != ".exe":
        raise ValueError(f"exe 形态需要 .exe 可执行文件：{path.name}")
    if kind == KIND_PCK and suffix != ".pck":
        raise ValueError(f"pck 形态需要 .pck 资源包：{path.name}")
    verdict = _probe_identity(path)
    if not verdict.accepted:
        raise ValueError(f"这个文件不能接入：{verdict.reason}（{verdict.kind}）")
    return verdict


# ---------------------------------------------------------------- 增删改


def add_line(
    cfg: dict,
    name: str,
    path: str,
    *,
    kind: str = KIND_EXE,
    engine_line: str = "",
    main_scene: str = "",
    args: str = "",
    profile: str = "",
    category: str = "",
    note: str = "",
) -> CustomLine:
    """新增一条自定义线。文件必须存在，否则报错（不做「先加后补」的悬空记录）。"""
    norm_kind = _norm_kind(kind)
    target = Path(path)
    verdict = _validate_file(target, norm_kind)
    resolved = str(target.resolve())
    if norm_kind == KIND_PCK:
        # 把 pck 路径传进去：没指定引擎线时按包头部版本自动匹配，
        # 而不是「挑最高版本」（那会让 4.6 导出的包被塞给 4.7 引擎）
        engine = normalized_engine_line(cfg, engine_line, target)
    else:
        # exe 形态自带运行时，引擎线无意义——清空，避免配置里留下误导字段
        engine = ""
    lines = load_all(cfg)
    # 同一个文件不重复登记：重复会让下拉里出现两条一模一样的项
    for existing in lines:
        if existing.path.lower() == resolved.lower():
            raise ValueError(f"这个文件已经登记过了：{existing.name}")
    line = CustomLine(
        id=new_id(),
        name=name.strip() or target.stem,
        kind=norm_kind,
        path=resolved,
        engine_line=engine,
        main_scene=main_scene.strip(),
        args=args.strip(),
        profile=profile.strip(),
        category=category.strip(),
        added_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        note=note.strip(),
    )
    _apply_identity(line, verdict)
    lines.append(line)
    save_all(cfg, lines)
    return line


def update_line(cfg: dict, line_id: str, **fields) -> bool:
    """改一条（名称/类型/文件/参数/存档目录/分类/备注）。换文件也要重新校验。"""
    lines = load_all(cfg)
    hit = False
    for line in lines:
        if line.id != line_id:
            continue
        hit = True
        if "kind" in fields:
            line.kind = _norm_kind(fields["kind"])
        if "path" in fields and str(fields["path"]).strip():
            candidate = Path(str(fields["path"]))
            verdict = _validate_file(candidate, line.kind)
            line.path = str(candidate.resolve())
            _apply_identity(line, verdict)
        # 换类型后原文件后缀可能不匹配，统一再验一次
        if ("kind" in fields or "path" in fields) and line.path:
            _apply_identity(line, _validate_file(Path(line.path), line.kind))
        if line.kind == KIND_PCK and ("engine_line" in fields or "kind" in fields):
            line.engine_line = normalized_engine_line(
                cfg,
                str(fields.get("engine_line", line.engine_line)),
                Path(line.path) if line.path else None,
            )
        if line.kind == KIND_EXE:
            line.engine_line = ""
        if "main_scene" in fields:
            line.main_scene = str(fields["main_scene"]).strip()
        if "name" in fields:
            line.name = str(fields["name"]).strip() or line.name
        if "args" in fields:
            line.args = str(fields["args"]).strip()
        if "profile" in fields:
            line.profile = str(fields["profile"]).strip()
        if "category" in fields:
            line.category = str(fields["category"]).strip()
        if "note" in fields:
            line.note = str(fields["note"]).strip()
        break
    if hit:
        save_all(cfg, lines)
    return hit


def remove_line(cfg: dict, line_id: str) -> bool:
    """移除登记（**不删游戏文件**，只是启动器不再管理它）。"""
    lines = load_all(cfg)
    kept = [line for line in lines if line.id != line_id]
    if len(kept) == len(lines):
        return False
    save_all(cfg, kept)
    return True


def get_line(cfg: dict, line_id: str) -> CustomLine | None:
    return next((line for line in load_all(cfg) if line.id == line_id), None)


# ---------------------------------------------------------------- 启动


@dataclass
class LaunchPlan:
    """自定义线的启动计划：命令 + 环境（存档隔离）。"""

    command: list[str]
    env_appdata: Path | None = None
    warnings: list[str] = field(default_factory=list)
    #: pck 形态生成的 packset 路径（诊断用）
    packset: Path | None = None


def command_for(line: CustomLine) -> list[str]:
    """拼出启动命令。参数按空格切分（不做引号解析，够用且不引入坑）。"""
    cmd = [line.path]
    if line.args.strip():
        cmd.extend(part for part in line.args.split() if part)
    return cmd


def _appdata_for(cfg: dict, line: CustomLine, profiles_root: Path) -> tuple[Path | None, list[str]]:
    warnings: list[str] = []
    redirect = bool((cfg.get("profiles", {}) or {}).get("redirect_appdata", True))
    if not redirect:
        return None, ["已关闭存档隔离，自定义线会写到系统默认存档目录"]
    appdata = profiles_root / line.profile_name() / "appdata"
    try:
        appdata.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, [f"存档隔离目录创建失败（将使用系统默认存档）：{exc}"]
    return appdata, warnings


def _packset_for(cfg: dict, line: CustomLine, profiles_root: Path,
                 extra_packs: list[dict] | None = None) -> Path:
    """给 pck 形态生成 packset.json（只含玩家那一个包）。

    放到存档目录旁边而不是 ``builds/``：``builds/`` 是「每次启动一个新目录」
    的产物区，会被清理；自定义线的 packset 需要稳定路径，否则玩家重装启动器
    （或清缓存）后，快捷方式里的旧命令就指向了不存在的文件。
    """
    from . import runner

    engine = line.engine_line or normalized_engine_line(
        cfg, "", Path(line.path) if line.kind == KIND_PCK and line.path else None
    )
    out_dir = profiles_root / line.profile_name() / "custom"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "packset.json"
    return runner.make_packset(
        out,
        packset_id=f"custom-{line.id.split(':', 1)[-1]}",
        line=engine,
        # 顺序＝优先级：改版本体在最前，协商通过的扩展覆盖层依次覆盖它
        packs=[{"path": line.path, "offset": 0, "role": "baseline"}, *(extra_packs or [])],
        baseline_id=None,
    )


def prepare_launch(cfg: dict, line: CustomLine, profiles_root: Path | str) -> LaunchPlan:
    """准备启动：只做存档隔离（pck 形态额外生成 packset），不挂任何扩展。"""
    root = Path(profiles_root)
    if not line.exists:
        raise ValueError(f"游戏文件不在了：{line.path}")

    appdata, warnings = _appdata_for(cfg, line, root)

    if line.kind == KIND_EXE:
        return LaunchPlan(
            command=command_for(line),
            env_appdata=appdata,
            warnings=warnings,
        )

    # ---------------- pck 形态：借底座引擎
    from . import runner

    engine = line.engine_line or normalized_engine_line(
        cfg, "", Path(line.path) if line.path else None
    )
    exe, base_pck = runner.host_paths(engine)

    # 能力协商：只有 full 档的 pck 形态才可能挂扩展；结果（含每个跳过原因）
    # 落 negotiation.json，摘要进 warnings，界面日志能直接看到。
    from .ext import negotiate as neg_mod

    report_dir = root / line.profile_name() / "custom"
    report_dir.mkdir(parents=True, exist_ok=True)
    agreement = neg_mod.negotiate(cfg, line)
    mods_path, _ = agreement.write_artifacts(report_dir)
    warnings.extend(agreement.warnings)
    packset = _packset_for(cfg, line, root, extra_packs=agreement.packs)
    scene = line.main_scene_or_default()
    cmd: list[str] = [str(exe)]
    # 与 runner.build_command 同一套配对规则：模板 exe 不支持 --main-pack，
    # 靠「与 exe 同名的 .pck」自动配对（底座 pck 就在 exe 旁边）。
    kind = str((cfg.get("engines", {}).get(engine) or {}).get("kind", "editor"))
    if kind != "template":
        cmd += ["--main-pack", str(base_pck)]
    cmd.append("--")
    cmd.append(f"--packset={packset.resolve().as_posix()}")
    if agreement.mods and mods_path is not None:
        # 与内置线同一份契约：bootstrap.gd 读它实例化 ModHost 入口
        cmd.append(f"--mods={mods_path.resolve().as_posix()}")
    cmd.append(f"--main={scene}")
    # 必须显式给 --log：bootstrap 在缺省会写到 exe 所在目录（runtime/hostXX/），
    # 那会把发布目录弄脏，还会在下次启动时被当成旧报告读走。
    cmd.append(f"--log={(report_dir / 'mount_report.json').resolve().as_posix()}")
    if line.args.strip():
        cmd.extend(part for part in line.args.split() if part)

    warnings.append(
        f"pck 形态用 {engine} 底座引擎挂载，主场景 {scene}；"
        "若该包依赖自己的 project.godot 设置，可能与底座不一致（启动失败时先看 mount_report）"
    )
    return LaunchPlan(
        command=cmd,
        env_appdata=appdata,
        warnings=warnings,
        packset=packset,
    )
