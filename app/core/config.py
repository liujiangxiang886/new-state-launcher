"""启动器配置：默认值、读写、基线解析。

配置文件是 ``config/launcher.json``，纯 JSON，缺失字段自动补默认值。

**路径约定**：配置里允许两种写法——

- 绝对路径：指向包外的资源（用户机器上的游戏本体、外部反解工程）；
- 包内相对路径（如 ``runtime/host46/jjxf46.exe``）：指向启动器自带的资源，
  整个目录搬到别的机器/盘符后仍然有效。

读的时候一律用 :func:`resolve_path` 按项目根展开，写的时候用
:func:`stored_path` 把包内路径收成相对。直接用 ``Path(raw)`` 会按**当前工作
目录**解析，换个目录启动就漂移——这是必须走这两个函数的唯一原因。
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from .. import edition
from . import paths
from .fsutil import atomic_write_json, quarantine_file, read_json

CONFIG_PATH = paths.CONFIG_DIR / "launcher.json"

#: 唯一内置引擎线。1.4 起只保留 4.6 作为**唯一基线**（对应原版 1.45）。
#:
#: 这个常量是「哪条线是内置线」的单一口径：需要硬编码内置线名的地方
#: 一律引用它，不要再写字面量 ``"4.6"``，避免以后又要全局替换一遍。
BUILTIN_LINE = "4.6"

#: 内置线对应的基线 id（原版 1.45）。
BUILTIN_BASELINE = "jjxf.v1.45"

#: 引擎线 → 默认基线 id。**唯一真源**：所有「按 line 找 baseline」的地方
#: 都从这里取，不要在别处再写一份 ``{"4.6": "jjxf.v1.45", ...}``——
#: 1.4 之前正是散在 8 个文件里的同款字典，移除一条线要改 8 处。
#:
#: 发布者版（publisher）恢复了 4.7 剧情线：4.6 仍是默认线，4.7 作为可选线。
#: 两条线的基线 id 不同、补丁锚点也不同，不能混用。
BASELINE_OF: dict[str, str] = {
    BUILTIN_LINE: BUILTIN_BASELINE,
    "4.7": "jjxf.v4.7",
}

#: 已下线的引擎线标识。读老配置时用来自动清理，见 :func:`migrate_legacy`。
#:
#: **这是数据不是文案**：它必须保留旧版本的字面标识，否则老玩家的配置
#: 里残留的对应条目就识别不出来，升级后界面会显示一个已经不存在的版本。
#:
#: 发布者版把 4.7 重新启用，所以这里**不能再列 4.7**——否则每次读配置都会
#: 把刚登记的 4.7 引擎/基线删掉（1.5/1.6 单线化时留下的行为）。
RETIRED_LINES: tuple[str, ...] = ()

#: 已下线的基线 id。
RETIRED_BASELINES: tuple[str, ...] = ()


def known_lines(cfg: dict[str, Any] | None = None) -> tuple[str, ...]:
    """当前可用的引擎线（按配置里登记的 engines 键，排序返回）。

    ``cfg`` 为空时按默认配置算。界面与 CLI 的「可选手」都从这里取，
    避免再出现 ``["4.6"]`` 这种写死的列表。
    """
    src = cfg if isinstance(cfg, dict) else DEFAULT_CONFIG
    lines = [str(k) for k in (src.get("engines") or {}) if str(k)]
    return tuple(sorted(lines)) or (BUILTIN_LINE,)

#: 剧情版工程名（决定 user:// 目录），实测自游戏 project.binary。
#:
#: 这个名字是**存档目录名**的历史约定，老玩家的存档就在这个目录里；
#: 改名字等于把人家的存档弄丢，所以保持不变。
STORY_PROJECT_NAME = "机甲旋风 · 单机"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema": 1,
    "log_level": "INFO",
    "ui": {
        "backend": "auto",          # auto | pyside | cli
        "theme": "dark",
        "language": "zh_CN",
        "background_image": "",     # 自定义界面背景（绝对路径，空=纯色）
        "background_dim": 40,       # 背景压暗强度 0-100，保证文字可读
        "last_version": BUILTIN_LINE,   # 上次选中的版本，下次打开时恢复
        # 使用前须知已同意的版本号（空 = 尚未同意，启动时要弹窗）。
        #
        # 用**版本字符串**而非 bool：条款改版时把 notice_dialog.NOTICE_VERSION
        # 递增，老用户会自动重新看到新条款，不用手改配置。
        #
        # 这个键**只由运行时写入**，绝不进发布包的出厂配置
        # （否则所有玩家首次打开都跳过弹窗）。见 build_release_zip。
        "notice_ack": "",
    },
    "packs": {
        "shard_limit_bytes": 1932735283,   # 1.8 GiB
        "data_align": 4096,
        "verify_after_write": True,
    },
    "smoke": {
        "timeout_sec": 180,
        "quit_after_frames": 600,
        "min_instantiated_nodes": 3,
    },
    "runtime": {
        # 每次**正式启动**前清掉 `user://shader_cache`（Godot 的 GLSL 二进制缓存）。
        #
        # 为什么必须清：进程被强杀（关窗口/任务管理器/启动器超时）时，这份缓存
        # 可能只写了一半。下次启动优先读它，`ShaderGLES3::initialize()` 失败，
        # 所有材质回退成**白色** —— 表现为「角色立绘全白、UI 正常」。
        # 2026-11-11 实测：删掉缓存后立刻恢复；不删直接重启必白（复现两次）。
        #
        # 代价：每次启动多花 1–3 秒重新编译。想保留缓存（排障）设为 false。
        "clear_shader_cache": True,
        # 启动前把**别的进程**的工作集腾空（EmptyWorkingSet），给游戏多留
        # 一些空闲物理页。原理与 PCL「百宝箱 → 内存优化」一致，见
        # ``app/core/memtrim.py`` 顶部注释。
        #
        # 默认关闭，三条理由：
        # 1. 它不减少任何进程的提交内存，只是把物理页换成页面文件。机械硬盘
        #    上玩家会立刻感觉到「切回浏览器卡一下」，属于会挨骂的体验；
        # 2. 收益随机器状态波动很大（实测同机 0.3~1.1 GB），没法承诺效果；
        # 3. 它**不会**让 4.7 剧情版的 7 GB 占用变小 —— 那是真实数据量。
        #
        # 绝对不碰游戏进程（jjxf*）：热数据腾走等于每帧硬缺页，只会更卡。
        "trim_working_set": False,
        # 帧率上限（0 = 不限制）。默认 60。
        #
        # 为什么要限帧：底座原版没有任何限帧或 vsync 设置，高刷屏（120/144 Hz）
        # 上引擎会跑满刷新率。这游戏是 2D、动画本来就只有 60 帧，超出部分
        # 全是空转 —— 白白吃掉内存带宽与页面文件配额。8 GB 机器上这就是
        # 换页抖动的来源之一。
        #
        # 为什么可调而不是写死 60：玩家机器差异太大。
        # - 高配玩家想要更高帧（减少输入延迟）时能自己放开；
        # - 更弱的机器（集显 / 单通道内存）需要 30 来换取稳定性。
        #
        # 取值范围由 :func:`fps_choices` 给出；超出范围的值在 :func:`fps_limit`
        # 里夹到合法区间，不让一个手改的坏值把游戏卡成幻灯片。
        "max_fps": 60,
        # 垂直同步：0=关、1=开、2=Mailbox、3=自适应。
        # 默认关：限帧已经能压住画面撕裂，再开 vsync 会额外占一帧缓冲。
        # 提供出来是因为少数驱动上「关 vsync 反而撕裂明显」，玩家可自选。
        "vsync": 0,
    },
    "profiles": {
        "default": "default",
        "keep_history": 10,
        "redirect_appdata": True,
    },
    "extensions": {
        # 扩展隔离模式：loose | strict
    #
    # loose  —— 四项检查照跑，结果只提示不阻断（默认，玩家可自选）
    # strict —— 验证标志 + 四项检查都过才装，问题包剔除
    #
    # 放在**顶层**（不在 ui 下）：它影响安装、启动、冒烟、报告，
    # 是运行期行为契约，不是界面偏好。
    "extension_mode": "loose",
        # 用户侧豁免名单。扩展作者**不能**自我豁免 ——
        # 否则「完全校验」等于「作者说算就算」。
        "strict_exempt": [],
        # 是否往钩子 context 里多注入 instance / arguments。
        #
        # 这两个键是扩展普遍在读的（实测 ai_act 有 65 处读 instance、
        # 45 处读 arguments），而原契约里根本没有 → 那些代码是**死代码**
        # （is_instance_valid(null) 恒假，回调第一行就 return）。
        # 默认开：现状是缺陷不是特性。用户可关掉回退。
        "hook_ctx_extended": True,
    },
    "engines": {
        # 4.6：编辑器构建（MIT），以 --main-pack 加载底座 pck 运行。
        # 这是**默认**引擎线；打包后 path 会被改写成包内相对路径
        # （runtime/host46/jjxf46.exe），绝对路径换台机器就失效了。
        "4.6": {
            # 出厂留空：路径因机器而异，由用户在「设置 / 游戏库」里指定，
            # 或在打包时由 release_launcher() 重写成包内相对路径。
            "path": "",
            "version": "4.6.0",
            "kind": "editor",
        },
        # 4.7：剧情版线（模板构建）。模板 exe 不支持 --main-pack，
        # 靠「与 exe 同名的 .pck」自动配对，所以 kind 必须是 template。
        "4.7": {
            # 同上：出厂留空，避免把开发机的绝对路径带进仓库。
            "path": "",
            "version": "4.7.2",
            "kind": "template",
        },
    },
    # 兼容底座的 project.godot 来源：取反解工程里那份真实配置做最小改动。
    # 出厂留空——反解工程来自游戏本体，不随仓库分发；构建底座时自行指定。
    "runtime_sources": {
        "4.6": {
            "project_godot": "",
            "main_scene": "res://scenes/start_page.tscn",
        },
        # 4.7 的反解工程不随包发；留 None，走 baseline/index-4.7 + 剧情包源码。
        "4.7": None,
    },
    "baselines": {
        "jjxf.v1.45": {
            "label": "机甲旋风 1.45（原版 · Godot 4.6）",
            "engine": "4.6",
            "kind": "exe",           # 1.45 是内嵌 pck 的可执行文件（导入后被探测覆盖）
            # 路径与探测结果（size / sha256 / entry_count）都必须留空：
            # 这些是**某台机器上那份本体**的属性，写死在默认值里会让新玩家
            # 一打开就看到「已关联 380,619 条」，而本机根本没有那个文件。
            # 玩家点「导入游戏本体」时由 baseline_ops.adopt 重新探测写入。
            "path": "",
            "shard_packset": "",   # 填了就用分片启动，忽略原 exe
        },
        # 4.7 剧情版：独立 .pck（pack_version=4），必须配 4.7 模板 exe。
        # ``story_overlay`` 是剧情覆盖层：它既是 4.7 线的源码层（138 个 .gd），
        # 也是内容层（data/scenes）。**必须在 mod 之前挂载**——后挂覆盖先挂，
        # 否则剧情包会把 mod 的覆盖层整片盖掉（1.4 的默认波次就是 story 在
        # extension 之后，所以 4.7 的 mod「装了没效果」）。
        "jjxf.v4.7": {
            "label": "机甲旋风 4.7（剧情版 · Godot 4.7.2）",
            "engine": "4.7",
            "kind": "pck",
            "path": "",
            "pack_version": 4,
            "shard_packset": "",
            "story_overlay": "",
            "exe_path": "",
        },
    },
    # 反解工程目录（源码漂移检测、覆盖层基线内容读取都用它）。
    # 同样是本机路径，默认留空；需要的人在「设置」里指一下。
    "sources": {
        "reversed_project": "",
    },
    "build": {
        "stub46_exe_bytes": 104599040,   # 1.45 exe 中 pck 之前的 PE stub 大小（实测）
        "out_dir": "builds",
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并，``override`` 优先；不修改入参。"""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def default_extension_mode() -> str:
    """各版本开箱默认的扩展校验档。

    | 版本 | 默认档 | 理由 |
    |---|---|---|
    | 玩家版 | ``loose`` | 装包不被技术细节打断 |
    | 统一版 | ``loose`` | **对外唯一发布版**，玩家拿到就是它 |
    | 发布者版 | ``loose`` | 作者自己的整合发行要能直接启动 |
    | 开发者版 | ``strict`` | 导入即准入，确定性优先 |

    **4.6 线**：玩家可在这两档之间自选（导入本体后弹窗询问），默认宽松。
    **4.7 线**：固定 ``strict``，见 :func:`available_modes`。

    为什么统一版给 ``loose`` 而不是 ``strict``：它是对外发布的那一份，
    面向的是玩家。``strict`` 的问题不是「判得准」，而是**每次都拦**——
    装 25 个包时任何一处锚点失配都会弹窗，玩家看不懂也该不关心。
    需要严格判定的人（作者、排查问题）在设置里自己切。
    """
    from .. import edition

    if edition.is_publisher():
        return "loose"
    if edition.is_player():
        return "loose"
    # 统一版：对外发布的那份，按玩家视角给默认值
    if str(getattr(edition, "EDITION", "")) == getattr(edition, "UNIFIED", "unified"):
        return "loose"
    return "strict"


def available_modes(line: str | None = None) -> tuple[str, ...]:
    """某条引擎线可选的档位。

    | 线 | 可选档位 |
    |---|---|
    | 4.6 | ``loose`` / ``strict``（玩家自选） |
    | 4.7 | 只有 ``strict``（剧情会更新，必须严格） |
    | 其它 | 两条都放开，交给调用方 |
    """
    if str(line or "") == "4.7":
        return ("strict",)
    return EXTENSION_MODES


def load(path: Path | None = None) -> dict[str, Any]:
    """读配置并与默认值合并。文件不存在时返回默认值。

    读进来的老配置会先过一遍 :func:`migrate_legacy`：早期版本有多条引擎线，
    现在只剩一条。老配置里指向已下线线的键如果不清理，界面会显示一个
    已经不存在的版本，启动时还会去加载已被删掉的底座。

    配置里**没有显式写** ``extensions.extension_mode`` 时，按当前 edition
    注入默认档：开发者版是 ``strict_verify``（开箱即确定性），玩家版是
    ``strict_warn``。显式写过的值一律尊重，不覆盖。

    **损坏时不抛异常**（2026-11-14 修）
    ----------------------------------
    这里过去直接 ``raise``，而调用链的最上游是 ``app.ui.pyside.app.run()``
    —— 那里没有任何 try。打包成 windowed exe 后 ``console=False``、
    ``sys.stderr`` 是 ``None``，于是：

        json.decoder.JSONDecodeError → 进程直接退出 → 界面上什么都没有

    玩家看到的就是「昨天还能开，今天双击没反应」。实测复现：把
    ``config/launcher.json`` 写坏（模拟断电/磁盘满/杀软截断留下的半截文件），
    打包版 ``rc=1``，无窗口、无提示、无日志。

    现在的处置：坏文件改名留档 → 返回默认配置 → 记一条 WARNING。
    玩家的扩展列表存在 ``config/extensions.json``，与这里分开，不会连带丢；
    而「能打开界面」远比「保住上次的窗口大小」重要。
    """
    target = path or CONFIG_PATH
    raw = _read_raw(target)
    if raw is _UNREADABLE:
        # 坏文件已隔离，走默认值。这条 WARNING 会进日志文件，玩家报问题
        # 时能直接看出「配置被重置过」。
        _log_corrupt(target)
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.setdefault("extensions", {})["extension_mode"] = default_extension_mode()
        return cfg
    if raw is None:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.setdefault("extensions", {})["extension_mode"] = default_extension_mode()
        return cfg
    if not isinstance(raw, dict):
        # 合法 JSON 但顶层不是对象（例如被写成了一个数组或数字）。
        # 同样按「无法使用」处理，不当成致命错误。
        _log_corrupt(target, reason=f"顶层不是对象（是 {type(raw).__name__}）")
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.setdefault("extensions", {})["extension_mode"] = default_extension_mode()
        return cfg
    merged = migrate_legacy(deep_merge(DEFAULT_CONFIG, raw))
    raw_ext = raw.get("extensions")
    if not isinstance(raw_ext, dict) or "extension_mode" not in raw_ext:
        merged.setdefault("extensions", {})["extension_mode"] = default_extension_mode()
    else:
        # 老配置里的档位名（normal / strict_warn / strict_verify / developer）
        # 在这里一次性转成新名并写进内存配置。**转换是单向的**，不再保留
        # 旧名兼容层——这样「界面下拉里没有的档位，内部也一定不会出现」。
        ext = merged.setdefault("extensions", {})
        ext["extension_mode"] = normalize_mode(ext.get("extension_mode"))
    return merged


#: :func:`_read_raw` 的哨兵：文件存在但读不出来（损坏 / 权限 / 编码错）。
_UNREADABLE = object()


def _read_raw(target: Path) -> Any:
    """把配置文件读成 Python 对象；读不出来时返回 :data:`_UNREADABLE`。

    与 :func:`fsutil.read_json` 的差别只有一条：**不抛异常**。
    配置文件是「可能被外力破坏」的数据（断电、磁盘写满、杀软截断、
    用户手工编辑），不是程序内部状态——它坏掉时该降级，不该让整个程序
    死在启动第一行。
    """
    try:
        return read_json(target, default=None)
    except (OSError, ValueError, UnicodeDecodeError):
        return _UNREADABLE


def _log_corrupt(target: Path, *, reason: str = "") -> None:
    """把坏配置文件改名留档，并记一条 WARNING。

    改名失败（文件被占用、目录只读）也不阻断：此时下次启动还会再撞一次，
    但至少这一次能进界面，用户可以自己去删文件。
    """
    moved = quarantine_file(target)
    detail = reason or "内容不是合法 JSON"
    note = f"已隔离为 {moved.name}" if moved else "无法隔离（文件被占用或目录只读）"
    logging.getLogger("config").warning(
        "配置文件损坏（%s）：%s；%s，本次使用默认配置", detail, target, note
    )


def migrate_legacy(cfg: dict[str, Any]) -> dict[str, Any]:
    """把老版本（多引擎线）的配置就地归一（原地修改并返回）。

    做四件事，全部是**幂等**的：

    1. 删掉已下线引擎线的 ``engines`` / ``runtime_sources`` 条目；
    2. 删掉已下线基线的 ``baselines`` 条目；
    3. 把 ``ui.last_version`` 指向已下线线/已删基线时，归位到内置线；
    4. 老的 ``story47`` 段并入新的 ``sources.reversed_project``。

    第 4 条不是纯删除：老玩家的反解工程路径是**有效信息**，源码漂移检测
    还要用。各条线的反解工程不是同一份，不能顺手搬过去——只在当前线
    那边确实为空时才补。
    """
    engines = cfg.get("engines")
    if isinstance(engines, dict):
        for line in RETIRED_LINES:
            engines.pop(line, None)

    sources = cfg.get("runtime_sources")
    if isinstance(sources, dict):
        for line in RETIRED_LINES:
            sources.pop(line, None)

    baselines = cfg.get("baselines")
    if isinstance(baselines, dict):
        for bid in RETIRED_BASELINES:
            baselines.pop(bid, None)

    ui = cfg.get("ui")
    if isinstance(ui, dict):
        last = str(ui.get("last_version") or "")
        if last in RETIRED_LINES or not last:
            ui["last_version"] = BUILTIN_LINE

    legacy_story = cfg.pop("story47", None)
    if isinstance(legacy_story, dict):
        reversed_project = str(legacy_story.get("reversed_project") or "").strip()
        slot = cfg.setdefault("sources", {})
        if isinstance(slot, dict) and reversed_project and not slot.get("reversed_project"):
            slot["reversed_project"] = reversed_project

    return cfg


def save(cfg: dict[str, Any], path: Path | None = None) -> Path:
    return atomic_write_json(path or CONFIG_PATH, cfg)


# ---------------------------------------------------------------- 扩展隔离模式

#: 扩展隔离模式的**两个**档位。
#:
#: - ``loose``  宽松校验：四项检查照跑，但只做提示，不阻断安装 / 启动
#: - ``strict`` 完全校验：验证标志与四项检查都过才装；单包问题剔除该包，
#:              依赖断链（被依赖的包被剔除）则整组装不上
#:
#: 两档**跑的检查完全相同**（锚点 / units / 钩子 / 成员），差别只在「结果怎么
#: 处置」。这条设计是为了让宽松档不是「什么都不查」，而是「查了但放行并告知」。
#:
#: 旧名（``normal`` / ``strict_warn`` / ``strict_verify``）已全部废弃，只经
#: :data:`MODE_ALIASES` 单向转换，转换后立刻落盘，不再回写旧值。
EXTENSION_MODES = ("loose", "strict")

#: 非法值一律回落这个档 —— 配置坏了不该让启动器起不来。
DEFAULT_EXTENSION_MODE = "loose"

#: 旧档位名 → 新档位名。**单向**，只为读老配置，不参与任何判定。
#:
#: - ``normal``        原本什么都不查，语义并入宽松（宽松也做全套检查）
#: - ``strict_warn``   原本「查了只记录」，现在升为「查了 + 弹窗告知」
#: - ``strict_verify`` 原本「整组拒绝」，现在改为「剔除单包 + 依赖闭包」
#: - ``developer``     早期界面别名，同 ``strict_verify``
MODE_ALIASES: dict[str, str] = {
    "normal": "loose",
    "strict_warn": "loose",
    "strict_verify": "strict",
    "developer": "strict",
}

#: 界面显示名（单一来源，避免 UI 与 core 两处文案漂移）
MODE_LABELS: dict[str, str] = {
    "loose": "宽松校验",
    "strict": "完全校验",
}

#: 每档的一句话说明，给下拉框 tooltip 用。
#:
#: 文案是**给玩家看的**，不用「锚点」「插桩」这类内部词：玩家只关心
#: 「能不能进游戏」和「出了问题谁能帮我」。
MODE_HELP: dict[str, str] = {
    "loose": (
        "游戏可能无法启动或者启动黑屏。"
        "装了没有适配过的扩展时，启动前会提醒你，但你仍然可以继续。"
    ),
    "strict": (
        "一个不符合就不会安装，但是稳定。"
        "扩展必须经过验证才能启用，装不上的会明确告诉你原因。"
    ),
}


#: 帧率上限的可选项（界面下拉用）。``0`` 表示不限制。
#:
#: 为什么是这几个值而不是连续滑条：
#:
#: - 30 / 45 / 60 / 90 / 120 覆盖了实际有意义的档位。中间值（如 47）
#:   对观感没有可辨识的提升，只会让玩家以为「调了却没用」；
#: - ``0``（不限制）保留给高配玩家 —— 他们可能开着高刷显示器；
#: - 默认 60 与游戏动画帧率一致，再高只是空转。
#:
#: 数值不是随便取的：30 是弱机的保命档（能显著减少换页），45 是 60 与 30
#: 之间的折中，90/120 只在确实有余力时才有意义。
FPS_CHOICES: tuple[int, ...] = (0, 30, 45, 60, 90, 120)

#: 出厂帧率上限。与 :data:`DEFAULT_CONFIG` 的 ``runtime.max_fps`` 一致。
DEFAULT_MAX_FPS = 60

#: 帧率上限的合法区间。超出者夹到边界而不是回落默认值 ——
#: 玩家手改成 144 时，给 120 比给 60 更接近他的意图。
FPS_MIN = 30
FPS_MAX = 240


def fps_limit(cfg: dict[str, Any]) -> int:
    """取帧率上限，并把任意坏值夹到合法区间。

    ``0`` 是**合法的**「不限制」，不能当成无效值处理 —— 那会让
    「我想不限帧」被悄悄改回 60。
    """
    raw = (cfg.get("runtime") or {}).get("max_fps", DEFAULT_MAX_FPS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_FPS
    if value == 0:
        return 0
    return max(FPS_MIN, min(FPS_MAX, value))


def vsync_mode(cfg: dict[str, Any]) -> int:
    """取垂直同步模式（Godot 4 的 ``vsync_mode`` 枚举，0 关 1 开 2 Mailbox 3 自适应）。"""
    raw = (cfg.get("runtime") or {}).get("vsync", 0)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value in (0, 1, 2, 3) else 0


def fps_label(value: int) -> str:
    """帧率档位的界面文案。"""
    return "不限制" if int(value) == 0 else f"{int(value)} 帧"


def normalize_mode(value: str | None) -> str:
    """把任意输入归一成 ``EXTENSION_MODES`` 里的内部值。

    旧名经 :data:`MODE_ALIASES` 单向映射到新名。新名直接透传。
    认不出来的值回落 ``loose``：宁可放行也不要让玩家打不开启动器。
    """
    text = str(value or "").strip()
    text = MODE_ALIASES.get(text, text)
    return text if text in EXTENSION_MODES else DEFAULT_EXTENSION_MODE


def extension_mode(cfg: dict[str, Any], *overrides: str | None) -> str:
    """解析扩展隔离模式。

    优先级（先到先得）：``overrides``（子命令级 > 全局级）> 配置文件 > 默认。

    ``overrides`` 用 ``*args`` 而不是具名参数，是因为要区分「子命令级」与
    「全局级」——argparse 的全局选项必须写在子命令**之前**，所以同一个
    ``--ext-mode`` 会被解析成两处，需要按顺序传进来。

    非法值回落 ``loose`` 而不是报错：玩家手改配置写错一个词，
    不该导致启动器起不来。
    """
    for value in overrides:
        if value:
            return normalize_mode(value)
    raw = str((cfg.get("extensions") or {}).get("extension_mode") or DEFAULT_EXTENSION_MODE)
    return normalize_mode(raw)


def is_strict(mode: str) -> bool:
    """是不是**会阻断**的档位（只有 ``strict``）。

    这是判定「要不要剔除 / 要不要拒绝安装」的唯一入口。``loose`` 虽然也跑
    同一套检查，但结果只用于提示。
    """
    return normalize_mode(mode) == "strict"


def is_verify(mode: str) -> bool:
    """``is_strict`` 的旧名，语义完全相同。

    保留是为了不动 30 余处调用点；新代码请用 :func:`is_strict`。
    """
    return is_strict(mode)


def is_developer(mode: str) -> bool:
    """``is_strict`` 的旧名，语义完全相同。

    「开发者档」这个概念已经没有独立语义了——完全校验就是完全校验，
    不区分作者自测还是玩家使用。新代码请用 :func:`is_strict`。
    """
    return is_strict(mode)


def netplay_open(cfg: dict[str, Any] | None) -> bool:
    """扩展的联机协议闸门是否放开。

    1.5.5 的 R2 规范默认「扩展携带 P2P / 直连协议一律拒装」。

    **这个能力只属于发布者版**。玩家版 / 开发者版 / **统一版**恒为 False
    ——配置里写什么都不算数。否则用户手改 ``extensions.netplay_open``
    或历史配置残留就能把闸门打开，而界面上根本没有对应的关闭入口。

    **统一版（2026-11-08 对外唯一发布版）明确不接入联机**：
    它不落进 PUBLISHER 分支，所以天然返回 False。这是有意设计，
    别为了「给作者方便」把 UNIFIED 加进白名单。

    发布者版默认放开，配置里显式写了就以它为准（便于临时关回去）。
    """
    if edition.EDITION != edition.PUBLISHER:
        return False
    ext = (cfg or {}).get("extensions") if isinstance(cfg, dict) else None
    if isinstance(ext, dict) and "netplay_open" in ext:
        return bool(ext.get("netplay_open"))
    return True


def extension_compose(cfg: dict[str, Any]) -> bool:
    """多个扩展改同一份内容时，运行时是否把它们按顺序叠加（默认开）。

    关掉就回到「各打一个覆盖层、后挂覆盖先挂」的旧行为：冲突判定里
    ``overlap.*`` 重新变成 warn，开发者模式也会照旧整组剔除。
    留这个开关是为了出问题时能一行配置回退，不必回滚版本。
    """
    return bool((cfg.get("extensions") or {}).get("compose", True))


def ensure_config() -> Path:
    """首次运行时落一份默认配置，方便用户手改。"""
    paths.ensure_dirs()
    if not CONFIG_PATH.exists():
        save(copy.deepcopy(DEFAULT_CONFIG))
    return CONFIG_PATH


# ---------------------------------------------------------------- 路径解析


def resolve_path(raw: str | Path | None) -> Path | None:
    """把配置里的路径解析成绝对路径，兼容相对路径。

    为什么需要：打包分发时，配置里的 ``engines.4.6.path`` 会被改写成包内
    相对路径（``runtime/host46/jjxf46.exe``）——绝对路径换台机器就失效了。
    而 ``Path(raw)`` 对相对路径是按**当前工作目录**解析的：从别的目录启动
    就会指错地方。所以统一按 ``paths.ROOT``（冻结后 = exe 所在目录）解析。

    绝对路径原样返回，不改变既有行为。
    """
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p
    # 延迟导入避免循环依赖（paths 不依赖 config）
    from . import paths

    return (paths.ROOT / p).resolve()


def stored_path(path: str | Path | None) -> str:
    """写配置/清单用：包内的存相对路径，包外保持绝对。

    与 :func:`resolve_path` 配对——读时展开、写时收回，整个目录搬走仍然有效。
    落盘一律用 POSIX 分隔符，避免 JSON 里到处是转义反斜杠。
    """
    if not path:
        return ""
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = (paths.ROOT / p).resolve()
    try:
        return p.resolve().relative_to(paths.ROOT.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------- 基线


def baseline(cfg: dict[str, Any], baseline_id: str) -> dict[str, Any]:
    try:
        return cfg["baselines"][baseline_id]
    except KeyError as exc:
        raise KeyError(f"未配置的基线：{baseline_id!r}") from exc


def baseline_path(cfg: dict[str, Any], baseline_id: str) -> Path:
    resolved = resolve_path(baseline(cfg, baseline_id).get("path"))
    if resolved is None:
        raise KeyError(f"基线 {baseline_id!r} 未配置路径")
    return resolved


def baseline_exists(cfg: dict[str, Any], baseline_id: str) -> bool:
    p = baseline_path(cfg, baseline_id)
    if not p.exists():
        return False
    expect = baseline(cfg, baseline_id).get("size")
    return expect is None or p.stat().st_size == expect


def baseline_health(cfg: dict[str, Any], baseline_id: str) -> tuple[str, str]:
    """基线的健康状态：``(状态, 说明)``。

    为什么不能只返回 bool：``baseline_exists`` 按 size 判存在，玩家换了
    一个同路径、不同版本的 exe 时会被判成「文件缺失」，界面只能说
    「基线不可用」，说不清到底是文件没了还是版本换了。

    状态：
    - ``ok``           路径存在且大小与登记一致；
    - ``size_mismatch`` 文件在、大小不符（多半是换了版本，仍可尝试启动）；
    - ``missing``      路径没配置，或文件不存在；
    - ``unregistered`` 配置里根本没有这个基线。
    """
    meta = (cfg.get("baselines") or {}).get(baseline_id)
    if not isinstance(meta, dict) or not meta.get("path"):
        return "unregistered", f"未配置基线：{baseline_id}"
    resolved = resolve_path(str(meta.get("path") or ""))
    if resolved is None:
        return "unregistered", f"基线 {baseline_id} 的路径无效"
    if not resolved.is_file():
        return "missing", f"基线文件不存在：{resolved}"
    expect = meta.get("size")
    if expect is not None:
        try:
            actual = resolved.stat().st_size
        except OSError as exc:
            return "missing", f"基线文件无法读取：{exc}"
        if actual != int(expect):
            return (
                "size_mismatch",
                f"基线文件大小与登记不符（本机 {actual:,}，登记 {int(expect):,}）；"
                "可能是换了版本，点「游戏库 → 导入游戏本体」可重新识别",
            )
    return "ok", ""


def engine_path(cfg: dict[str, Any], line: str) -> Path | None:
    raw = (cfg.get("engines", {}).get(line) or {}).get("path") or ""
    if not raw:
        return None
    p = resolve_path(raw)
    return p if p and p.exists() else None
