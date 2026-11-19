"""运行时编排：拼 packset、准备 profile、启动底座、回收报告。

启动模型（两代统一）：引擎二进制 + ``--main-pack`` 指向底座 pck，
底座里的 ``Compat`` 再按 packset 挂载游戏基线包。

    <host>/jjxf47.exe --headless --main-pack <host>/jjxf47.pck \
        -- --packset=<run>/packset.json --main=res://scenes/start_page.tscn \
           --log=<run>/mount_report.json --smoke

注意：所有业务参数必须放在 ``--`` **之后**，因为底座用
``OS.get_cmdline_user_args()`` 取值；放在前面会被引擎自己吃掉。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod, paths
from .errors import EngineMissing, LauncherError, PackVersionMismatch
from .fsutil import atomic_write_json


@dataclass
class LaunchResult:
    ok: bool
    returncode: int
    seconds: float
    command: list[str]
    stdout: str = ""
    stderr: str = ""
    mount_report: dict | None = None
    smoke_report: dict | None = None
    run_dir: Path | None = None
    notes: list[str] = field(default_factory=list)
    saves: dict = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 2),
            "mounted": (self.mount_report or {}).get("mounted", []),
            "errors": (self.mount_report or {}).get("errors", []),
            "smoke": self.smoke_report,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "saves": (self.saves or {}).get("counts", {}),
            "command": self.command,
        }


def host_paths(line: str) -> tuple[Path, Path]:
    """返回 (引擎 exe, 底座 pck)。"""
    tag = paths.line_tag(line)
    host = paths.ROOT / "runtime" / f"host{tag}"
    exe = host / f"jjxf{tag}.exe"
    pck = host / f"jjxf{tag}.pck"
    if not exe.exists():
        raise EngineMissing(
            f"{line} 运行时不存在：{exe}（先跑 python tools/build_base.py {tag}）",
            line=line,
            exe=str(exe),
        )
    if not pck.exists():
        raise EngineMissing(
            f"{line} 底座包不存在：{pck}（先跑 python tools/build_base.py {tag}）",
            line=line,
            pck=str(pck),
        )
    return exe, pck


def check_host_pairing(line: str) -> str:
    """检查引擎 exe 与底座 pck 是否**同名配对**，返回一句人话。

    为什么要在启动前查
    ------------------

    Godot 的**模板 exe 不支持 ``--main-pack``**（编译时没开
    ``disable_path_overrides``），它靠「与 exe 同名的 .pck」自动配对。
    一旦对不上（比如用户把 exe 单独拷走、或者底座 pck 被改名），
    引擎会弹一个**模态 ALERT**：

        Error: Couldn't load project data at path "...". Is the .pck file missing?
        If you've renamed the executable, the associated .pck file should also
        be renamed to match the executable's name (without the extension).

    那个弹窗会**卡住进程**，玩家看到的就是「双击游戏就弹一个看不懂的英文框」。
    在启动器里先查一遍、给出中文提示，比让玩家去猜引擎的英文报错强得多。

    返回空串表示配对没问题。
    """
    tag = paths.line_tag(line)
    host = paths.ROOT / "runtime" / f"host{tag}"
    exe = host / f"jjxf{tag}.exe"
    if not exe.exists():
        return ""  # 缺失由 host_paths() 报，那边的提示更准
    if not host.is_dir():
        return ""
    # 找该目录里实际的 pck（可能被人改过名）
    pkcs = sorted(host.glob("*.pck"))
    if not pkcs:
        return ""  # 没有 pck 由 host_paths() 报
    if any(p.stem == exe.stem for p in pkcs):
        return ""  # 有同名配对的，没问题
    others = "、".join(p.name for p in pkcs)
    return (
        f"{line} 引擎与底座包没有同名配对：{exe.name} 需要 {exe.stem}.pck，"
        f"但该目录下只有 {others}。引擎会自动弹一个英文的「pck missing」"
        f"错误框（那是模态窗口，会卡住进程）。"
        f"请把 {others} 改名成 {exe.stem}.pck，或重新跑 "
        f"python tools/build_base.py {tag} 重建。"
    )


def enforce_baseline_lock(cfg: dict, line: str) -> None:
    """4.7 硬门：基线分层指纹不符就抛 ``LauncherError``，不许启动。

    只在 4.7 生效。4.6 走老流程（``size`` 比对只在 readiness 里给警告）——
    那条线有自制整合包的存量玩家，硬拦会让一批人打不开游戏，比「校验不严」
    更糟。

    校验器本身出错时**放行**：一个辅助判定不该让玩家连游戏都开不了。
    这种情况下只记一条日志，把失败原因留在那里。

    消息里会指出是**哪一层**不符、以及代码层变了哪些文件 —— 后者比
    「整包不匹配」有用得多：玩家换了剧情包时，需要知道「我换了这个，
    所以旧扩展要重新验证」。
    """
    if str(line) != "4.7":
        return

    from . import baseline_identity

    try:
        check = baseline_identity.verify(cfg, line)
    except Exception as exc:  # noqa: BLE001 - 判定器出错不该拦住启动
        _log_baseline(f"基线指纹校验未完成（已放行）：{exc}")
        return

    if check.ok or not check.layers:
        return

    lines = ["这台机器上的 4.7 游戏文件与登记不符，已拒绝启动。"]
    for layer in check.failing():
        if layer.name == "code" and (layer.changed or layer.added or layer.removed):
            sample = (layer.changed or layer.added or layer.removed)[:5]
            lines.append(
                f"· 代码层有 {len(layer.changed) + len(layer.added) + len(layer.removed)} 个文件变动："
                f"{'、'.join(sample)}"
            )
        else:
            lines.append(f"· {layer.reason or layer.name + ' 层不符'}")
    lines.append("")
    lines.append(
        "如果不是你主动替换过游戏文件，请重新导入 4.7 本体；"
        "如果你确实换了剧情包，请在扩展页点「验证全部」重新登记。"
    )
    raise LauncherError("\n".join(lines), line=line, check=check.as_dict())


def _log_baseline(message: str) -> None:
    """基线门的日志落点。独立成一个函数是为了测试可以拦它。"""
    try:
        from . import logging as log_mod

        log_mod.get("runner").warning(message)
    except Exception:  # noqa: BLE001
        pass


def baseline_pack(cfg: dict, baseline_id: str) -> dict[str, object]:
    """把配置里的基线描述转换成 packset 条目（含内嵌 offset）。"""
    meta = cfg_mod.baseline(cfg, baseline_id)
    path = Path(meta["path"])
    if not path.exists():
        raise LauncherError(f"基线文件缺失：{path}", baseline=baseline_id, path=str(path))
    entry: dict[str, object] = {"path": path.as_posix(), "baseline": baseline_id}
    # 内嵌在 exe 里的包需要偏移；独立 pck 为 0
    if meta.get("kind") == "exe":
        from .pck.reader import locate_pck

        start, _size = locate_pck(path)
        entry["offset"] = start
    else:
        entry["offset"] = 0
    # 记录期望指纹，底座可据此做前置校验（后续版本使用）
    entry["sha256"] = meta.get("sha256", "")
    return entry


def story_packs(cfg: dict, baseline_id: str) -> list[dict]:
    """剧情覆盖层的挂载条目（4.7 线）。

    为什么必须排在 mod **之前**：Godot 是「后挂覆盖先挂」。剧情包不是普通的
    附加内容，它是 4.7 线的**源码层 + 内容层**——原版 4.7 pck 里脚本只有
    ``.gdc``（138 个），剧情包带着同名的 ``.gd`` 源码与自指 remap，mod 的
    ``text_patches`` 正是按这份源码算出来的。1.4 的加载波次是
    ``framework → extension → story``，剧情包排在 mod 之后，于是整片剧情
    内容把 mod 覆盖层盖掉，表现就是「mod 装了但没效果」。

    发布者版把剧情包当基线的一部分：baseline → story → mod → instrumented。
    """
    meta = cfg_mod.baseline(cfg, baseline_id)
    raw = str(meta.get("story_overlay") or "")
    if not raw:
        return []
    path = cfg_mod.resolve_path(raw)
    if path is None or not path.is_file():
        return []
    return [{"path": path.as_posix(), "offset": 0, "role": "story"}]


def baseline_packs(cfg: dict, baseline_id: str) -> list[dict]:
    """基线的挂载条目（含 4.7 的剧情覆盖层）。

    配置里填了 ``shard_packset`` 且文件存在时，用分片取代原包——这样分片产物
    能直接走正常启动路径，而不只是 CLI 里的一个附赠功能。
    """
    meta = cfg_mod.baseline(cfg, baseline_id)
    shard_packset = str(meta.get("shard_packset") or "")
    if shard_packset:
        p = Path(shard_packset)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                packs = [
                    {
                        "path": item["path"],
                        "offset": int(item.get("offset", 0)),
                        # role 让底座能校验「基线必须在覆盖层之前」
                        "role": "baseline",
                        "pack_version": item.get("pack_version", 0),
                    }
                    for item in data.get("packs", [])
                    if item.get("path")
                ]
            except (OSError, json.JSONDecodeError, KeyError):
                packs = []
            if packs:
                return packs + story_packs(cfg, baseline_id)
    return [
        {**baseline_pack(cfg, baseline_id), "role": "baseline"},
        *story_packs(cfg, baseline_id),
    ]


def _insert_sources_layer(packs: list[dict], line: str) -> list[dict]:
    """在基线之后插入「源码兜底层」。

    为什么需要
    ----------
    扩展用 ``text_patches`` 改 ``scripts/x.gd`` 时，改动结果是 **x 的源码**，
    会以 ``.gd`` 形态写进 overlay。由此产生两类断链：

    1. **preload 断链**：x 里 ``preload("res://scripts/y.gd")``，而本体里 y 只有
       ``.gdc`` → ``Preload file ... does not exist``。
    2. **继承断链**：``opening_run.gd``（本体只有 .gdc）``extends`` 了被改的
       ``training_run.gd``。字节码类不能把运行时才编译的源码当父类
       → ``Could not resolve class``。所以子类也必须一起变源码。

    实测（2026-11-05）：工作台 + 更好的体验 + 浮游炮 + 教官 共存时，
    第 2 类断链直接导致 ``game_world.gd:67`` 实例化失败 → 进主城灰屏。

    挂载位置（2026-11-05 修正）
    --------------------------
    **排在所有覆盖层之后**，而不是之前。

    先前放在基线之后、overlay 之前，结果 ``opening_run``（兜底层）比它的父类
    ``training_run``（工作台 overlay）先可见，照样报 ``Could not resolve class``。

    放最后是安全的：兜底层只补「本体本来只有 .gdc 的那些文件」，这些路径
    不会被任何扩展的 overlay 覆盖（扩展要改它们就会自己提供 .gd，那就不缺了）。
    所以排最后不会盖掉谁的改动，只保证所有源码类同时可见。
    """
    from .ext import sources_layer as sl_mod

    import os

    if os.environ.get("JJXF_NO_SOURCES_LAYER"):
        return packs

    try:
        layer = sl_mod.ensure_layer(packs, line)
    except Exception as exc:  # noqa: BLE001 - 兜底层失败不能拦住启动
        # 宁可少一层兜底，也不要因为「补源码」这个次要目标让游戏起不来。
        # 缺失时行为退回改动前：可能灰屏，但至少能启动排障。
        print(f"[sources] 源码兜底层生成失败，已跳过：{exc}")
        return packs

    if layer is None:
        return packs

    # 位置：所有 mod / story 之后，merged / instrumented 之前。
    #
    # - 不能放 mod 之前：`opening_run`（兜底层）会比父类 `training_run`
    #   （工作台 overlay）先可见，报 Could not resolve class。
    # - 不能放 merged / instrumented 之后：那两层是「最终生效版本」，
    #   必须盖住兜底层。
    #
    # 兜底层补的都是「本体只有 .gdc」的文件，实测与所有 overlay 交集为 0，
    # 所以插在 mod 之后不会盖掉任何扩展的改动。
    out: list[dict] = []
    inserted = False
    for item in packs:
        role = str(item.get("role") or "")
        if not inserted and role in ("merged", "instrumented"):
            out.append(
                {
                    "path": layer.as_posix(),
                    "offset": 0,
                    "role": "sources",
                    "pack_version": 3,
                }
            )
            inserted = True
        out.append(item)
    if not inserted:
        out.append(
            {
                "path": layer.as_posix(),
                "offset": 0,
                "role": "sources",
                "pack_version": 3,
            }
        )
    return out


def make_packset(
    out_path: str | Path,
    *,
    packset_id: str,
    line: str,
    packs: list[dict],
    baseline_id: str | None = None,
) -> Path:
    """写 packset.json，并给每个分片补上 ``pack_version``。

    为什么必须记：实测（``tests/test_pack_version.py``）表明版本约束是**向下兼容**
    ——v4 引擎读得了 v3 包，但 v3 引擎明确拒绝 v4（`Pack version unsupported: 4`）。
    所以「v4 包挂到 v3 引擎上」是最容易踩的坑，得在写 packset 时就把版本记下来，
    给了错配的组合就能提前判断而不是等引擎报错。
    """
    from .pck.reader import locate_pck

    enriched: list[dict] = []
    for item in packs:
        rec = dict(item)
        raw = str(rec.get("path") or "")
        path = Path(raw)
        # 必须写成绝对路径：游戏进程的 cwd 是 exe 所在目录，不是启动器目录，
        # 相对路径会被它按自己的 cwd 解析 → mount_report 报"文件不存在"。
        if raw and not path.is_absolute():
            path = (paths.ROOT / raw).resolve() if not path.exists() else path.resolve()
            rec["path"] = path.as_posix()
        if "pack_version" not in rec and path.is_file():
            try:
                offset = int(rec.get("offset") or 0)
                if offset == 0:
                    offset = locate_pck(path)[0] or 0
                with path.open("rb") as handle:
                    handle.seek(offset + 4)
                    rec["pack_version"] = int.from_bytes(handle.read(4), "little")
            except (OSError, ValueError, IndexError, LauncherError):
                # 读不出版本只是少一条预检信息，不能让启动直接失败：
                # 非 pck 文件、被截断的包、无内嵌包的 exe 都会走到这里，
                # 真正的判定交给引擎的 mount_report。
                pass
        enriched.append(rec)

    enriched = _insert_sources_layer(enriched, line)

    payload = {
        "schema": 1,
        "id": packset_id,
        "line": line,
        "baseline": baseline_id,
        "pack_version": max((int(p.get("pack_version") or 0) for p in enriched), default=0),
        "packs": enriched,
        "entry_count": len(enriched),
    }
    return atomic_write_json(out_path, payload)


def prepare_profile(name: str) -> Path:
    """建 profile（``appdata`` 会被当成重定向后的 ``APPDATA``）。

    不再建空的 ``saves/``：真实存档落在
    ``appdata/Godot/app_userdata/<底座工程名>/``，多一个空目录只会让人找错地方。
    """
    p = paths.profile_dir(name)
    (p / "appdata").mkdir(parents=True, exist_ok=True)
    return p


def run_dir_for(tag: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = paths.BUILDS / f"{tag}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_command(
    cfg: dict,
    line: str,
    *,
    packset: str | Path,
    main_scene: str,
    smoke: bool,
    headless: bool,
    log_path: str | Path,
    mods_path: str | Path | None = None,
    fps: int | None = None,
    vsync: int | None = None,
) -> list[str]:
    """拼出完整启动命令。UI 与 CLI 共用，避免两处规则漂移。

    **所有传给游戏的路径必须是绝对路径。**
    游戏进程的 cwd 是 exe 所在目录（``runtime/host46``），不是启动器目录；
    传相对路径会被它按自己的 cwd 解析，于是 packset 找不到、mods.json 解析失败、
    日志写不出来——表现就是"点了开始游戏进不去"。
    """

    def _abs(value: str | Path) -> str:
        return Path(value).resolve().as_posix()

    # 帧率与 vsync：调用方显式传了就用它，否则从配置取。
    #
    # 显式参数是给「冒烟测试」这类场景用的 —— 冒烟要跑得飞快，不该被
    # 玩家的 60 帧上限拖慢（无头模式下限帧反而会让 smoke 超时）。
    if fps is None:
        fps = cfg_mod.fps_limit(cfg)
    if vsync is None:
        vsync = cfg_mod.vsync_mode(cfg)

    exe, base_pck = host_paths(line)
    cmd: list[str] = [str(exe)]
    if headless:
        cmd.append("--headless")
    # 模板导出时可能关掉路径覆盖（报 "compiled without support for
    # path overrides"），这类引擎靠「同名 .pck」自动配对，不能传 --main-pack。
    kind = (cfg.get("engines", {}).get(line) or {}).get("kind", "editor")
    if kind != "template":
        cmd += ["--main-pack", str(base_pck)]
    cmd.append("--")
    cmd.append(f"--packset={_abs(packset)}")
    cmd.append(f"--main={main_scene}")
    cmd.append(f"--log={_abs(log_path)}")
    # 帧率策略由玩家在设置页决定，必须走命令行传进去。
    #
    # 为什么不能只靠 project.godot：那是**构建期**写死的，玩家改配置改不动它。
    # 底座 bootstrap 在 `_init()` 里读这两个参数并设 Engine.max_fps ——
    # 那是所有 Autoload 里最早的时机，早于主场景实例化，游戏代码读到的
    # 就是玩家选的档位。
    #
    # 总是传（而不是「非默认值才传」）：显式优于隐式，日志里能直接看出
    # 这一局用的是哪档，排查「为什么还是 60」时不用回头翻配置。
    cmd.append(f"--max-fps={fps}")
    cmd.append(f"--vsync={vsync}")
    if mods_path:
        cmd.append(f"--mods={_abs(mods_path)}")
    if smoke:
        cmd.append("--smoke")
    return cmd


def build_env(cfg: dict, profile: str | None, env_extra: dict[str, str] | None = None) -> tuple[dict[str, str], Path]:
    """构造子进程环境：把 ``user://`` 重定向到 profile。"""
    env = os.environ.copy()
    name = profile or (cfg.get("profiles", {}) or {}).get("default", "default")
    prof_dir = prepare_profile(name)
    if (cfg.get("profiles", {}) or {}).get("redirect_appdata", True):
        env["APPDATA"] = str(prof_dir / "appdata")
        env["LOCALAPPDATA"] = str(prof_dir / "appdata")
        # 与 profiles.env_for 保持一致：少数 Godot 构建会认 XDG 路径，
        # 只重定向 APPDATA 的话它们仍会写到真实用户目录，导致「隔离没生效」。
        env["XDG_DATA_HOME"] = str(prof_dir / "appdata")
    if env_extra:
        env.update(env_extra)
    return env, prof_dir


def check_packset_versions(cfg: dict, line: str, packset: str | Path) -> list[str]:
    """校验 packset 里的分片版本能否被该引擎线读取。

    实测规则（``tests/test_pack_version.py``）：引擎能读 ``<=`` 自身的版本。
    4.6 读 v3 没问题；把 v4 分片挂到 4.6 上会直接 ``Pack version unsupported: 4``。
    这种情况在启动前就该拦下——否则表现是「游戏起不来」，很难联想到版本。
    """
    from .pck.format import PACK_VERSION_BY_ENGINE

    try:
        data = json.loads(Path(packset).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    limit = PACK_VERSION_BY_ENGINE.get(line)
    if limit is None:
        return []
    problems: list[str] = []
    for item in data.get("packs") or []:
        version = item.get("pack_version")
        if version is None:
            continue
        if int(version) > limit:
            name = Path(str(item.get("path") or "")).name
            problems.append(
                f"{name} 是 pack_version {version}，{line} 引擎最高只支持 {limit}（引擎会拒绝加载）"
            )
    return problems


def _win_long(path: Path | str) -> str:
    """转成 Windows 扩展长度路径（``\\\\?\\`` 前缀），绕过 260 字符上限。

    收敛到 :func:`app.core.fsutil.win_long_path`：那边额外做了 DOS 短名展开
    （``\\\\?\\`` 路径里残留 ``LIJIXI~1`` 会报 WinError 123），安装解包也用它。
    显式加前缀仍是必需的：不加时超长路径上的 ``stat``/``rmtree`` 会静默失败。
    注：PyInstaller 6.12 默认清单其实带 longPathAware，但不能只靠它——
    开发环境、旧构建、子进程都未必有。
    """
    from .fsutil import win_long_path

    return win_long_path(path)


def _tree_size(root: Path) -> int:
    """递归统计目录内所有普通文件的字节数（走长路径，避免超限后统计成 0）。"""
    total = 0
    stack = [_win_long(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(_win_long(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _rmtree_force(path: Path) -> bool:
    """尽力删掉整棵目录，返回是否真的不存在了。"""
    target = _win_long(path)
    shutil.rmtree(target, ignore_errors=True)
    return not os.path.exists(target)


def _sweep_cache_trash(base: Path, keep: Path | None = None) -> None:
    """删掉历史遗留的 ``shader_cache.trash-*`` 目录，最多保留 ``keep`` 这一份。

    「先改名再删」是为了保证引擎下次启动看不到旧缓存，但游戏刚退出时
    缓存文件句柄可能还占用着，改名后的目录删不掉。不扫的话每次启动都会
    多留一份（约 1 MB）且永久堆积。这里在每次清理时顺手回收旧的。
    """
    try:
        with os.scandir(_win_long(base)) as it:
            stale = sorted(
                (Path(e.path) for e in it
                 if e.name.startswith("shader_cache.trash-")
                 and e.is_dir(follow_symlinks=False)),
                key=lambda p: p.name,
            )
    except OSError:
        return
    for p in stale:
        if keep is not None and p == keep:
            continue
        _rmtree_force(p)


def purge_shader_cache(cfg: dict, line: str, profile: str | None = None) -> int:
    """清掉该档位下 Godot 的着色器缓存，返回释放的字节数（0 = 没清）。

    为什么要在启动前清
    ------------------

    Compatibility 渲染器把编译好的 GLSL 二进制缓存在
    ``user://shader_cache/``，下次启动优先读它。

    **进程被强杀时这份缓存可能只写了一半**（关窗口、任务管理器结束、
    启动器超时、崩溃），下一次启动读到坏缓存 → ``ShaderGLES3::initialize()``
    失败 → 所有材质回退成白色。表现是**角色立绘全白、UI 与文字完全正常**，
    极容易被误判成「存档坏了」或「某个 mod 的锅」。

    2026-11-11 实测（RTX 4060 + Godot 4.7.2 Compatibility）：

    - 删掉 shader_cache → 立绘立刻恢复
    - 不删直接重启 → 必白，连续复现两次
    - 期间停用三个可疑扩展无效，日志里除 ``shader_gles3.cpp:802``
      没有任何脚本/贴图错误

    代价是每次多花 1–3 秒重新编译，远低于「角色全白」的代价。
    想保留缓存排障：``config/launcher.json`` 里设
    ``runtime.clear_shader_cache = false``。
    """
    freed, _why = purge_shader_cache_ex(cfg, line, profile)
    return freed


def purge_shader_cache_ex(
    cfg: dict, line: str, profile: str | None = None
) -> tuple[int, str]:
    """同 :func:`purge_shader_cache`，但把「为什么没清」一并返回，便于写日志。

    2026-11-11 现场：第二次启动立绘全白，但引擎日志里**没有**
    ``shader_gles3.cpp:802``；缓存目录的时间戳还是上一次的，说明清理没生效，
    可当时唯一的返回值是 0，分不清是「没缓存」「游戏在跑」还是「删失败」。
    所以这里把原因带出来。

    删除方式改成「先改名再删」：``shader_cache`` 改成一个带时间戳的名字，
    引擎下次启动就看不到它了 —— 即使改名后的目录因为文件句柄没释放而删不掉，
    也不影响这次启动拿到干净状态。直接 ``rmtree`` 时，只要有文件被占用，
    整个目录就可能残留一半，下一次照样读坏缓存。
    """
    if not bool((cfg.get("runtime") or {}).get("clear_shader_cache", True)):
        return 0, "已禁用（runtime.clear_shader_cache = false）"
    try:
        from . import runtime_lock

        if runtime_lock.is_game_running():
            # 游戏在跑时缓存文件被占用，删不干净，还可能让正在跑的进程出错
            return 0, "有游戏进程在运行，跳过"
    except Exception:  # noqa: BLE001 - 拿不到锁信息不该阻止启动
        pass

    try:
        from . import profiles as profiles_mod
        from .userdir import launcher_dir

        name = profile or profiles_mod.active_name(cfg)
        target = Path(launcher_dir(name, line, cfg)) / "shader_cache"
    except Exception as exc:  # noqa: BLE001
        return 0, f"路径解析失败：{exc}"
    if not os.path.isdir(_win_long(target.parent)):
        return 0, "没有缓存目录"
    # 先回收历史残留：上次退出时缓存文件还锁着，改名后的目录删不掉，
    # 于是每次启动都会多留一份。**必须放在下面 is_dir 提前返回之前**，
    # 否则「没有缓存目录」这条最常见的路径永远扫不到（2026-11-11 现场：
    # 连测 4 次启动，4 份 trash 全留着，正是因为每次都从这里提前返回）。
    _sweep_cache_trash(target.parent)
    if not os.path.isdir(_win_long(target)):
        return 0, "没有缓存目录"

    # 缓存目录的真实路径可达 330+ 字符（档位名 + 中文 userdata 名 + 64 位哈希
    # 子目录），超过 Windows 260 上限。原来的 rglob+stat 在这种路径上会全部
    # 返回 0 字节，rmtree 也会静默失败 —— 这正是「清理了但缓存还在」的根因。
    total = _tree_size(target)
    # 先改名：改名是原子操作，成功后引擎必定看不到旧缓存。之后的删除只是
    # 清垃圾，删不干净也不影响这次启动。
    trash = target.with_name(f"shader_cache.trash-{int(time.time() * 1000)}")
    try:
        target.rename(trash)
    except OSError as exc:
        return 0, f"改名失败（可能被占用）：{exc}"
    gone = _rmtree_force(trash)
    # 游戏刚退出时文件句柄往往还没释放，这一份通常删不掉。它已经不影响启动，
    # 但会一直堆着（每次约 1 MB），所以每次清理顺手把旧的垃圾扫一遍。
    # 现场：2026-11-11 连测 4 次，攒了 4 份 shader_cache.trash-*。
    _sweep_cache_trash(target.parent)
    if not gone:
        return total, "已清理（旧缓存已隔离，残余下次启动自动删）"
    return total, "已清理"


def trim_working_set_before_launch():
    """启动前把别的进程的工作集腾空，返回 :class:`memtrim.TrimReport`。

    **默认关闭**（``runtime.trim_working_set``）。开启后玩家能多拿到几百 MB
    到 1 GB 的空闲物理页，代价是被腾走的页下次访问要重新换入 —— 机械硬盘上
    会觉得「切回浏览器卡一下」。原理见 ``app/core/memtrim.py`` 顶部。

    两条排除是硬要求：

    - **游戏进程**（``jjxf*``，含多开的其它实例）：4.7 剧情版是每帧都在读的
      热数据，腾走之后立刻硬缺页拉回，只会更卡；
    - **本进程**：启动器自己马上要用 Qt 画界面，腾它没意义。
    """
    from . import memtrim

    exclude = {os.getpid()}
    try:
        # 多开场景下可能已经有游戏在跑，别动它
        from . import runtime_lock

        exclude.update(int(p) for p in runtime_lock.running_engine_pids())
    except Exception:  # noqa: BLE001 - 拿不到进程表不该阻止启动
        pass
    return memtrim.trim_working_sets(exclude_pids=exclude)


def launch(
    cfg: dict,
    line: str,
    *,
    packset: str | Path,
    main_scene: str = "res://scenes/start_page.tscn",
    smoke: bool = False,
    headless: bool = True,
    profile: str | None = None,
    run_dir: str | Path | None = None,
    timeout: float | None = None,
    extra_args: list[str] | None = None,
    env_extra: dict[str, str] | None = None,
    mods_path: str | Path | None = None,
) -> LaunchResult:
    exe, _base_pck = host_paths(line)
    rd = Path(run_dir) if run_dir else run_dir_for(f"run{paths.line_tag(line)}")
    rd.mkdir(parents=True, exist_ok=True)

    # 清着色器缓存：强杀留下的坏缓存会让下一次启动「角色全白」。
    #
    # 放在**最前面**（基线校验之前）是有意的：即使这次因为「游戏本体没导入」
    # 之类的原因根本起不来，缓存也已经清干净，下次不会带着坏缓存进游戏。
    # 冒烟不清 —— 冒烟每次都要重编译，白白拖慢测试。
    if not smoke:
        freed = purge_shader_cache(cfg, line, profile)
        if freed:
            print(
                f"[runner] 已清着色器缓存 {freed / 1048576:.1f} MB"
                "（防止上次强杀留下的坏缓存导致角色全白）"
            )
        # 可选：启动前把别的进程的工作集腾空，多留点空闲物理页给游戏。
        # 默认关闭。放在清缓存之后 —— 这一步会遍历全系统进程，比清缓存慢，
        # 但不影响正确性；顺序只影响日志的阅读顺序。
        if (cfg.get("runtime") or {}).get("trim_working_set"):
            report = trim_working_set_before_launch()
            if report.ok:
                print(f"[runner] {report.summary()}")
            else:
                print(f"[runner] 内存优化未执行：{report.reason}")

    # 启动前先查「引擎 exe 与底座 pck 是否同名配对」——
    # 对不上时 Godot 的模板 exe 会弹一个模态英文 ALERT（会卡住进程）。
    # 这里先给出中文提示，别让玩家去猜引擎的英文报错。
    pairing = check_host_pairing(line)
    if pairing:
        raise EngineMissing(pairing, line=line, exe=str(exe))

    # 硬门：4.7 的基线指纹不符就不许启动。
    #
    # 为什么放在 runner 而不是只在 readiness：readiness 只决定「按钮能不能点」，
    # 而 CLI、冒烟、双开、自动兜底重试都会绕开按钮直接调这里。只拦 UI 等于
    # 「换个入口就绕过了」。
    #
    # 只对 4.7 生效：4.6 有自制整合包的存量玩家，硬拦会让一批人打不开游戏。
    # 判据与 readiness 的 `_layered_check` 保持同一份实现，避免两处结论不一。
    enforce_baseline_lock(cfg, line)

    mount_report = rd / "mount_report.json"
    smoke_report = rd / "smoke_report.json"
    for stale in (mount_report, smoke_report):
        if stale.exists():
            stale.unlink()

    cmd = build_command(
        cfg,
        line,
        packset=packset,
        main_scene=main_scene,
        smoke=smoke,
        headless=headless,
        log_path=mount_report,
        mods_path=mods_path,
        # 冒烟与无头模式一律不限帧：这两种跑法不渲染给人看，
        # 限帧只会让「跑完 N 帧就退出」的冒烟流程白白多等时间，
        # 严重时直接撞上 smoke.timeout_sec（玩家配的 60 帧不该拖累测试）。
        fps=0 if (smoke or headless) else None,
        vsync=0 if (smoke or headless) else None,
    )
    if extra_args:
        cmd.extend(extra_args)

    version_problems = check_packset_versions(cfg, line, packset)
    if version_problems:
        raise PackVersionMismatch(
            "分片版本与引擎线不匹配：" + "；".join(version_problems),
            path=str(packset),
            line=line,
        )

    env, prof_dir = build_env(cfg, profile, env_extra)

    # 存档保护：先备份再取指纹，退出后比出变更。冒烟跑的是临时 profile 之外
    # 的同一个目录，但 headless 也会写配置，所以一视同仁。
    before_prints: dict[str, dict] = {}
    backup_dir: Path | None = None
    try:
        from . import saves as saves_mod

        previous = saves_mod.load_manifest(prof_dir) or {}
        before_prints = dict(previous.get("files") or {}) or saves_mod.fingerprint(prof_dir)
        backup_dir = saves_mod.backup(prof_dir)
    except Exception as exc:  # noqa: BLE001 - 存档保护失败不该拦住启动
        before_prints = {}

    timeout = timeout if timeout is not None else float((cfg.get("smoke", {}) or {}).get("timeout_sec", 180))
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(exe.parent),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc = -9
        out = (exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        err = (exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        timed_out = True

    elapsed = time.time() - started
    mr = _read_json(mount_report)
    sr = _read_json(smoke_report)
    notes: list[str] = []
    if timed_out:
        notes.append(f"超时（{timeout:.0f}s）被终止")
    if mr is None:
        notes.append("未生成 mount_report.json（底座可能未能启动）")
    elif not mr.get("ok", False):
        notes.append("挂载报告标记失败")
    if smoke and sr is None:
        notes.append("未生成 smoke_report.json")
    elif smoke and sr is not None and sr.get("smoke") != "ok":
        notes.append(f"冒烟未通过：{sr.get('smoke')}")

    ok = rc == 0 and (mr or {}).get("ok", False) and (not smoke or (sr or {}).get("smoke") == "ok")

    # --- 存档变更摘要
    saves_delta: dict = {}
    try:
        from . import saves as saves_mod

        after_prints = saves_mod.fingerprint(prof_dir)
        saves_delta = saves_mod.diff(before_prints, after_prints)
        saves_mod.save_manifest(prof_dir, after_prints, note=f"{line} {'smoke' if smoke else 'run'}")
        atomic_write_json(
            rd / "saves-diff.json",
            {
                "profile": str(prof_dir),
                "backup": str(backup_dir) if backup_dir else "",
                "delta": saves_delta,
                "summary": saves_mod.summary_line(saves_delta),
            },
        )
        if saves_delta.get("total"):
            notes.append(saves_mod.summary_line(saves_delta))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"存档摘要生成失败：{exc}")

    return LaunchResult(
        ok=ok,
        returncode=rc,
        seconds=elapsed,
        command=cmd,
        stdout=out,
        stderr=err,
        mount_report=mr,
        smoke_report=sr,
        run_dir=rd,
        notes=notes,
        saves=saves_delta,
    )


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
