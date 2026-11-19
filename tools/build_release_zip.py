"""打发布 zip：启动器程序 + 运行时 + 文档 + 一份修复过的剧情覆盖层。

为什么要有脚本而不是手工压缩
----------------------------

``dist/机甲旋风兼容启动器/`` 是**运行目录**，里面混着玩家自己的东西：

| 目录 | 是什么 | 能不能进包 |
|---|---|---|
| ``_internal`` / ``*.exe`` | 程序本体 | ✅ |
| ``runtime`` / ``engines`` | 底座引擎与 pck | ✅ |
| ``baseline`` / ``schemas`` | 基线索引、校验 schema | ✅ |
| ``docs`` / 说明文件 | 文档（在仓库根） | ✅ |
| ``config`` | 配置 | ⚠️ 只带出厂值，见下 |
| ``packs`` | 玩家装的扩展覆盖层 | ❌（唯一例外见下） |
| ``profiles`` | 玩家存档 | ❌ |
| ``builds`` / ``cache`` / ``logs`` | 运行产物与日志 | ❌ |

**1.4 的唯一例外**：随包附带一个修复过的剧情覆盖层
``packs/story-官方分区-轻量/story.pck``。理由与约束见 ``docs/开发者须知.md`` §5.1：
修复做在成品字节上，用素材重建会丢；发布脚本对它做白名单 + 哈希双重校验。

``config/`` 的出厂态由 :func:`release_config` 逐项生成，不直接拷运行目录：

- ``extensions.json`` 必须**清空**——里面每条都指向 ``packs/<id>/overlay-XX.pck``，
  而扩展覆盖层不随包发，带过去就是新用户一屏「覆盖层缺失」；
- ``stories.json`` 置空——剧情功能已移除，出厂不预登记任何条目；
- ``minigames.json`` / ``runtime-state.json`` / ``recent-runs.json`` 是运行期状态，
  里面还有本机桌面路径和 pid，一律重置；
- ``launcher.json`` 的引擎路径收成包内相对路径，基线路径换成中性占位
  （``game/…``），不把开发者机器的目录结构发给别人。

    python tools/build_release_zip.py                 # 打到仓库根
    python tools/build_release_zip.py --dry-run       # 只列清单
    python tools/build_release_zip.py -o D:/x.zip     # 指定路径
"""

from __future__ import annotations

import argparse
import gzip
import os
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 发布目录（程序 + 运行时）。JJXF_DIST 可指向别的构建目录
#: （如 ``dist_16p_fix/新起点启动器``），省去来回拷贝。
#:
#: 目录名与 exe 名一致（spec 的 ``_EXE_NAME``）。改名之后如果这里不同步，
#: 脚本会报「缺少发布文件」—— 那是在提醒你 dist 里还是旧构建。
DIST = Path(os.environ.get("JJXF_DIST") or (ROOT / "dist" / "New state launcher"))

#: 产品名（2026-11-08 统一命名）。与 ``app/edition.py`` 的 ``APP_NAME``
#: 保持一致 —— 这里不 import 那个模块，因为打包脚本要能在不加载
#: Qt 的环境下跑（``python tools/build_release_zip.py`` 直接执行）。
PRODUCT_NAME = "New state launcher"

#: zip 内部的顶层目录名。玩家解压后看到的就是它。
#: 实际值在 :func:`build` 里按 edition 重写（dev/player 带后缀防混装）。
ARC_ROOT = PRODUCT_NAME
#: 当前正在打的版本（``player`` / ``dev``）。由 :func:`build` 设置，
#: 供 :func:`collect` 里的出厂配置生成读取——那里拿不到 edition 参数。
_CURRENT_EDITION = "player"

#: 从 dist 里带走的顶层条目
INCLUDE_FROM_DIST = (
    "New state launcher.exe",
    "_internal",
    "runtime",
    "engines",
    "baseline",
    "schemas",
    "config",
)

#: 从仓库根**整目录**带走的开发资源包（相对 ROOT 的目录名）。
#:
#: ``扩展开发文档`` 是给扩展作者的独立开发包：4.6 / 4.7 两套规范与排障
#: 手册分开放、打包工具、基线指纹。它自包含（工具只用标准库），解压即用，
#: 不依赖启动器源码。
#:
#: **为什么整个目录带**：里面 ``tools/`` 和 ``baseline/`` 的相对位置有语义
#: （``jjm_pack.py`` 靠向上找 ``baseline/`` 算 code_digest），拆开平铺会坏掉。
DEVKIT_DIRS = ("扩展开发文档",)

#: 从仓库根带走的文档（单个文件）。``(源, zip 内相对 ARC_ROOT 的路径)``
INCLUDE_FROM_REPO: tuple[tuple[str, str], ...] = (
    # 「先看这里」放最前面：解压后第一眼就能分清
    # 「玩家双击 exe」和「创作者打开扩展开发文档」，不用猜。
    ("先看这里.md", "先看这里.md"),
    ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
    ("运行要求.md", "运行要求.md"),
    # 玩家须知放**根目录**：解压后第一眼能看到，不用先摸进 docs/。
    # 仓库里仍只有 docs/ 这一份，避免两个副本各自漂移。
    ("docs/玩家须知.md", "玩家须知.md"),
)

#: ``docs/`` 里**允许**进包的文档 —— **白名单，不是黑名单**。
#:
#: 早先这里写的是「整个 docs/ 都带走」，结果把开发期文件一起发了出去：
#: 交接文档、工作进展、未完成任务清单、``evidence/`` 下的比对脚本……
#: 这些是给协作者看的内部材料，进了玩家包既无用又泄露内部细节。
#: 用白名单是因为「新增一个内部文档」比「新增一个玩家文档」常见得多，
#: 黑名单会持续漏。
#: 与开发包重复的文档**不再单独发**：它们已经在
#: ``<ARC_ROOT>/扩展开发文档/公共文档/`` 里了，再放一份到 ``docs/``
#: 会让玩家看到两个版本，而且两边各自更新迟早漂移。
#:
#: 1.7 重整（2026-11-13）：旧文档整体归档到 ``_backup/docs-20261113/``，
#: 这一版按新的文件清单重写。**每一条都必须真实存在**——缺了直接中止打包
#: （见 :func:`collect`），比打完包再被 ``verify`` 报「条目数不符」好排查。
#:
#: 刻意**不发**的两份：
#: - ``玩家须知.md``：走 :data:`INCLUDE_FROM_REPO` 放到**根目录**，让玩家
#:   解压后第一眼看到，不再在 ``docs/`` 里放第二份；
#: - ``内部-分线隔离与源码兜底.md``：实现说明，含内部取舍与测试文件名，
#:   只对协作者有意义。
DOC_ALLOWLIST = (
    "README.md",
    "claims能力占用约定.md",
    "使用手册.md",
    "发布公告-玩家版.md",
    "权属归档与第三方许可清单.md",
    "规范手册.md",
)

#: 无论如何都不该出现在发布包里的文件名特征（校验用）
BANNED_DOC_TOKENS = ("交接文档", "工作进展", "未完成任务", "实战分析", "evidence")

#: config 里不进包的子项（玩家数据）
CONFIG_EXCLUDE = ("backups",)

#: 1.4 起剧情功能整体移除（它只服务已下线的引擎线），发布包不再携带
#: 任何剧情覆盖层——``packs/`` 必须为空，校验阶段会对此断言。

#: 已被压缩过的条目：直接 STORED，省掉几分钟无收益的 deflate。
#: ``.pck`` 里主要是 ``.ctex`` 压缩纹理与已打包二进制，实测压不动。
STORE_SUFFIXES = (".pck",)

#: 无论在哪里都不要的（打包残留 / 运行残留）
#:
#: ``mount_report.json`` / ``smoke_report.json`` 是跑引擎时写下的报告：
#: 正常情况下 ``--log=`` 会把它们指到本次运行目录，但手动跑 host exe
#: 时 bootstrap 会退回写到 exe 旁边（``runtime/host46/``）。那是**开发机
#: 的一次运行结果**，发给别人会让诊断页读到过期数据。
SKIP_NAMES = ("_tmp_", ".bak", "mount_report", "smoke_report")


def _skip(path: Path) -> bool:
    name = path.name
    return any(token in name for token in SKIP_NAMES)


# ---------------------------------------------------------------- 出厂配置


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def release_stories() -> str:
    """``config/stories.json`` 的出厂态：空注册表。

    1.4 起剧情功能整体移除（剧情包只服务已下线的引擎线），
    出厂不该再预登记任何条目。
    """
    return _dump({"schema": 1, "items": []})


def release_launcher(edition: str = "player") -> str:
    """``config/launcher.json`` 的出厂态：包内相对路径 + 中性基线占位。

    基线路径换成 ``game/…`` 这种明显"还没导入"的占位，而不是把开发机的
    ``D:\\迅雷2盘\\…`` 发给玩家。行为上等价：两种情况都是"文件不存在"，
    界面都会提示「基线不可用」，引导去「导入游戏本体」。

    ``edition`` 决定扩展校验档：**两个版本不能混用同一档**。

    - 开发者版出厂即 ``strict``（完全校验）：导入即准入，锚点会失配的包
      直接拒装，作者在自己机器上就能看到问题，而不是发给玩家后才灰屏；
    - 玩家版出厂 ``loose``（宽松校验）：装包不被技术细节打断，
      导入本体后可自行切到完全校验。

    历史教训（2026-11-05）：这里原先对所有版本都硬写 ``normal``，导致开发者版
    拿不到严格校验，「开发者校验」形同虚设——旧版 better_experience 的技能优化
    与战斗工作台锚点冲突，在开发者版里也没被拦住，最后主城灰屏。
    """
    # 种子取「仓库里那份配置」，**不能取 dist 里的**：PyInstaller 会重建
    # dist 目录，种子文件常常不在，退化成 {} 后出厂配置就没有 baselines 了
    # （玩家打开根本看不到内置版本，必须先导入才能用）。
    seed = ROOT / "config" / "launcher.json"
    if not seed.is_file():
        seed = DIST / "config" / "launcher.json"
    cfg = _read_json(seed, {})
    if not cfg:
        # 两边都读不到时用代码里的默认值兜底，保证出厂配置结构完整
        import copy as _copy

        from app.core import config as _cfg_mod

        cfg = _copy.deepcopy(_cfg_mod.DEFAULT_CONFIG)
    ui = cfg.setdefault("ui", {})
    # 本机装过哪些扩展、按什么顺序挂——与玩家无关，还会误导排序。
    ui.pop("extension_order", None)
    # 首次打开落在内置线（4.6）。1.4 起这是唯一的版本入口。
    ui["last_version"] = "4.6"
    # 展示模式必须回出厂值（简洁）。
    #
    # 这个键是「开发时打开、发版时忘了关」的典型：作者在自己机器上开了
    # 详细展示排查问题，构造出厂配置时如果不重置，所有玩家拿到的都是
    # 11 行技术明细 —— 正是要避免的「玩家觉得复杂」。
    ui["show_detail"] = False
    # 自定义背景图是开发者本机路径，带出去会指向不存在的文件。
    ui.pop("background_image", None)
    ui["background_dim"] = ui.get("background_dim", 40)

    # 机甲风外壳的新开关：显式写进出厂配置（2026-11-09 用户要求）。
    #
    # 为什么不靠代码默认值：这些键缺省时走代码默认（纯净模式开、
    # HUB 三区全隐藏），行为一样；但玩家想关掉某个区域时得先知道有这选项。
    # 显式写出来，配置文件和设置页一一对应。
    #
    # 值即「出厂观感」：只留常用导航、HUB 三块信息全收起 ——
    # 首次打开就是最干净的立绘 + 启动按钮。
    ui["pure_mode"] = True            # 只留 游戏库/扩展管理/小游戏/设置
    ui["status_panel_hidden"] = True  # 默认落在 HUB，不展开功能页
    ui["hub_hide_stats"] = True       # 隐藏「扩展与状态」面板
    ui["hub_hide_config"] = True      # 隐藏「当前配置」条
    ui["hub_hide_statusbar"] = True   # 隐藏底部扩展状态条

    # 使用前须知必须**每次全新安装都弹一次**。
    #
    # notice_ack 是「已同意的条款版本」，只在用户点同意时由运行时写入。
    # 若它跟着出厂配置发出去，所有玩家首次打开都会直接跳过弹窗 ——
    # 免费声明与免责条款等于没做。这里强制清除。
    ui.pop("notice_ack", None)

    # 启动优化开关必须回出厂值（默认关闭，2026-11-11 用户要求）。
    # 它是系统级操作（EmptyWorkingSet 换页），免责声明也按「默认关闭、
    # 开启风险自担」写的——开发机测试时开着、带着发出去，等于替所有玩家
    # 做了决定。必须由玩家自己在设置页打开。
    cfg.setdefault("runtime", {})["trim_working_set"] = False

    # 存档档位必须回出厂值。
    #
    # 2026-11-16 的玩家日志（``launcher-20261116-113827.log``）暴露的真问题：
    # 出厂配置直接沿用了开发机的 ``profiles.default = "smoke46"``，于是每个
    # 玩家第一次启动都跑在一个叫「smoke46」的档位里 —— 那个名字来自开发期
    # 的挂载冒烟测试，与玩家毫无关系；更麻烦的是 4.6 与 4.7 的 ``user://``
    # 目录都落进同一个 ``appdata``，排查存档问题时极易看错是哪条线的数据。
    #
    # 只重置「用哪个档位」和两个策略项，与 ``config.DEFAULT_CONFIG`` 对齐。
    # **不动 profiles 目录本身**：那是玩家数据，发布包也不该带。
    prof = cfg.setdefault("profiles", {})
    prof["default"] = "default"
    prof["keep_history"] = 10
    prof["redirect_appdata"] = True

    # 帧率策略必须回出厂值（默认 60 帧 / 关 vsync）。
    #
    # 这两项是**开发机最容易带着跑出去**的那一类：作者为了排查卡顿
    # 把 max_fps 调成 0（不限制）或把 vsync 打开，忘了改回来，所有玩家
    # 拿到的就是「作者本机那套设置」。出厂态显式写死，与
    # ``config.DEFAULT_CONFIG`` 保持一致。
    cfg["runtime"]["max_fps"] = 60
    cfg["runtime"]["vsync"] = 0

    # 隔离模式回到出厂档。两个版本档位不同：
    # 开发者版要 strict（导入即准入），玩家版要 loose（不打扰，可自选）。
    # 运行目录里可能是开发/测试时切到的其它档，带出去会造成档位错配。
    ext = cfg.setdefault("extensions", {})
    ext["extension_mode"] = "strict" if edition == "dev" else "loose"
    ext["strict_exempt"] = []

    engines = cfg.setdefault("engines", {})
    engines["4.6"] = {
        "path": "runtime/host46/jjxf46.exe",
        "version": str((engines.get("4.6") or {}).get("version") or "4.6.0"),
        "kind": "editor",
    }
    # 4.7 恢复为正式引擎线（1.6 出厂时被剥掉，现在双线启动器要它回来）。
    # 4.7 是**模板构建**：模板 exe 不支持 --main-pack，靠「与 exe 同名的 .pck」
    # 自动配对，所以 kind 必须是 template。
    engines["4.7"] = {
        "path": "runtime/host47/jjxf47.exe",
        "version": str((engines.get("4.7") or {}).get("version") or "4.7.2"),
        "kind": "template",
    }
    # 其它已下线线别的残留键才需要清
    for retired in ("4.5", "4.4"):
        engines.pop(retired, None)

    # 出厂只带内置本体那一条。开发机配置里可能残留已下线线别的本体登记，
    # 只改白名单条目的路径会把它们**原样发出去**——里面还带着开发机的盘符路径。
    placeholders = {
        "jjxf.v1.45": "game/mchGame.exe",
        # 4.7 是独立 .pck + 模板 exe 两层，出厂都留占位（玩家导入后覆盖）
        "jjxf.v4.7": "game/jjxf47.pck",
    }
    #: 4.7 额外的层路径，出厂同样置空由导入写回
    LAYER_PATHS = {"jjxf.v4.7": ("exe_path", "story_overlay")}
    baselines = cfg.get("baselines") or {}
    #: 这些字段是**导入时探测出来的运行时信息**（文件大小、哈希、条目数……），
    #: 出厂态必须清零：它们属于开发机那份本体，留着会显示成「已关联 2 条」
    #: 这种假数据，还会让 ``baseline_exists`` 的尺寸比对误判。
    PROBED_FIELDS = (
        "size",
        "sha256",
        "entry_count",
        "pack_version",
        "pck_size",
        "sha256_stale",
        "exe_size",
        "exe_sha256",
        "project_name",
        "label",
        "kind",
        # 本体校验状态（2026-11-16 新增）。开发机上验过就会写成 verified，
        # 若原样发出去，玩家**没导入本体**也会被判「已校验」，
        # 新加的那道「导入后必须校验」的门禁直接失效。
        "verify_state",
        "verify_at",
        "verify_by",
        "verify_note",
    )
    for bid in list(baselines):
        if bid in placeholders:
            meta = baselines[bid]
            if isinstance(meta, dict):
                meta["path"] = placeholders[bid]
                meta["exe_path"] = ""
                for extra in LAYER_PATHS.get(bid, ()):
                    meta[extra] = ""
                for field in PROBED_FIELDS:
                    meta.pop(field, None)
        else:
            del baselines[bid]
    cfg["baselines"] = baselines

    # 反解工程与剧情源包都在开发者机器上，发布包里没有也不该有。
    # 置 None 而不是 ""：空串会让 ``Path("").parent`` 变成当前目录，
    # 源码根判定从「不存在」误翻成「存在」；None 在所有读取点都走 ``or {}`` 兜底。
    cfg["runtime_sources"] = {"4.6": None, "4.7": None}
    cfg["sources"] = {"reversed_project": ""}
    # 老字段：配置加载时会自动并入 sources，出厂态不再写它
    cfg.pop("story47", None)
    cfg.setdefault("build", {})["out_dir"] = "builds"

    return _dump(cfg)


def release_engines() -> str:
    """``engines/engines.json`` 只是给工具读的清单，把个人路径换成包内相对。

    已下线线别的条目必须**删掉**——开发机上的 engines.json 里可能还留着它们
    （运行期积累的），只改路径会让发布包里带着一条指向不存在文件的记录。
    """
    raw = _read_json(DIST / "engines" / "engines.json", {"engines": {}})
    rel = {
        "4.6": "runtime/host46/jjxf46.exe",
        "4.7": "runtime/host47/jjxf47.exe",
    }
    engines = raw.get("engines") or {}
    for line in list(engines):
        if line not in rel:
            del engines[line]
    for line, meta in engines.items():
        if isinstance(meta, dict):
            meta["path"] = rel[line]
            meta["exists"] = (ROOT / rel[line]).is_file()
    raw["engines"] = engines
    return _dump(raw)


def release_config(edition: str = "player") -> dict[str, str | bytes]:
    """``zip 内路径 -> 替换后的内容``。"""
    base = f"{ARC_ROOT}/config"
    return {
        f"{base}/extensions.json": _dump({"schema": 1, "items": []}),
        f"{base}/stories.json": release_stories(),
        f"{base}/launcher.json": release_launcher(edition),
        f"{base}/minigames.json": _dump({"schema": 1, "items": []}),
        f"{base}/runtime-state.json": _dump({"schema": 1, "slots": {}}),
        # ``recent-runs.json`` 不进包：dist 里未必有，缺失时读侧按空表兜底
        # （``catalog.recent_runs``），带着反倒是开发机的运行历史。
        f"{ARC_ROOT}/engines/engines.json": release_engines(),
        # 基线探测结果里有开发机绝对路径与本机哈希，出厂只留描述性字段
        f"{ARC_ROOT}/baseline/identity-1.45.json": release_identity(),
        # 4.7 的身份指纹必须随包发：验证标志绑的就是它的 code_layer.digest，
        # 没有它启动器算不出 code_digest，所有扩展都会被判「无法验证」。
        f"{ARC_ROOT}/baseline/identity-4.7.json": release_identity("identity-4.7.json"),
        # 索引要随包发（导入后做路径比对用），但 source 字段带本机路径
        f"{ARC_ROOT}/baseline/index-1.45.json.gz": release_index(),
    }


# ---------------------------------------------------------------- 清单


def collect() -> list[tuple[Path, str]]:
    """返回 ``[(源文件, zip 内路径), ...]``。"""
    out: list[tuple[Path, str]] = []

    for name in INCLUDE_FROM_DIST:
        src = DIST / name
        if not src.exists():
            raise SystemExit(f"缺少发布文件：{src}\n（先跑 PyInstaller 并把产物同步进 dist）")
        if src.is_file():
            out.append((src, f"{ARC_ROOT}/{name}"))
            continue
        for path in sorted(src.rglob("*")):
            if not path.is_file() or _skip(path):
                continue
            rel = path.relative_to(DIST).as_posix()
            parts = rel.split("/")
            if name == "config" and len(parts) > 1 and parts[1] in CONFIG_EXCLUDE:
                continue
            out.append((path, f"{ARC_ROOT}/{rel}"))

    for src_name, arc_rel in INCLUDE_FROM_REPO:
        src = ROOT / src_name
        if not src.is_file():
            print(f"  [warn] 缺少文档：{src}（跳过）")
            continue
        out.append((src, f"{ARC_ROOT}/{arc_rel}"))

    # 开发资源包：整目录带，保持内部结构不变
    for kit in DEVKIT_DIRS:
        kit_root = ROOT / kit
        if not kit_root.is_dir():
            print(f"  [warn] 缺少开发包目录：{kit_root}（跳过）")
            continue
        for path in sorted(kit_root.rglob("*")):
            if not path.is_file():
                continue
            # 跳过 Python 缓存与临时产物
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            rel = path.relative_to(ROOT).as_posix()
            out.append((path, f"{ARC_ROOT}/{rel}"))

    docs = ROOT / "docs"
    missing: list[str] = []
    for name in DOC_ALLOWLIST:
        src = docs / name
        if not src.is_file():
            missing.append(name)
            continue
        out.append((src, f"{ARC_ROOT}/docs/{name}"))
    if missing:
        # 不做「跳过继续」：白名单与目录不一致时打包出来的包是**残缺文档包**，
        # 而 :func:`verify` 只会报一句「docs 条目数不符」，看不出缺了哪份。
        # 在这里直接中止，并指名道姓。
        raise SystemExit(
            "docs 白名单里的文档不存在：\n"
            + "\n".join(f"  - docs/{n}" for n in missing)
            + "\n（要么补文件，要么从 DOC_ALLOWLIST 里删掉）"
        )

    # 出厂配置文件**必须每条都在**，哪怕 dist 里没有源文件。
    # PyInstaller 重打包会重建 dist，``config/`` 常常是空目录——这时
    # ``release_config()`` 仍会生成内容，但没有对应的 zip 条目可写，
    # 玩家解压后就没有 launcher.json（启动器会退回全默认值）。
    # 这里用「打包脚本自己」当源占位：``build()`` 走的是重写分支，
    # 只认 arc 名字，不读 src 内容。
    have = {arc for _src, arc in out}
    for arc in release_config(_CURRENT_EDITION):
        if arc not in have:
            out.append((ROOT / "tools" / "build_release_zip.py", arc))

    return out


# ---------------------------------------------------------------- 打包


def build(out_zip: Path, *, dry_run: bool = False, edition: str = "player") -> int:
    global ARC_ROOT, _CURRENT_EDITION
    # 包内顶层目录名。
    #
    # 2026-11-08 起对外只发 ``unified`` 一个包，目录名就是产品名。
    # 开发者版仍带后缀 —— 那是内部调试产物，两包解压到同一目录时
    # 不能互相覆盖（否则会出来「一半新一半旧」的混合安装）。
    if edition == "dev":
        ARC_ROOT = f"{PRODUCT_NAME}-dev"
    elif edition == "player":
        ARC_ROOT = f"{PRODUCT_NAME}-player"
    else:
        ARC_ROOT = PRODUCT_NAME
    _CURRENT_EDITION = edition

    # 发行版本标记：程序启动与打包校验都以它为准。写在 dist 里再进包，
    # 与其它 _internal 文件走同一条路径。必须在 collect() 之前写。
    marker = DIST / "_internal" / "edition.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(f"{edition}\r\n".encode("utf-8"))

    files = collect()
    rewrites = release_config(edition)
    total = sum(p.stat().st_size for p, _ in files)
    print(f"待打包：{len(files)} 个文件，解压后 {total / 1024 / 1024:.1f} MB")
    for src, arc in files[:5]:
        print(f"   {arc}")
    print(f"   … 共 {len(files)} 条（其中 {len(rewrites)} 个配置文件按出厂态重写）")

    if dry_run:
        return 0

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    written = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for src, arc in files:
            if arc in rewrites:
                zf.writestr(arc, rewrites[arc])
            elif arc.endswith(STORE_SUFFIXES):
                # 走 STORED 要显式给 compress_type，否则会继承 ZipFile 的默认。
                info = zipfile.ZipInfo.from_file(src, arc)
                info.compress_type = zipfile.ZIP_STORED
                with src.open("rb") as fh, zf.open(info, "w") as dst:
                    while True:
                        chunk = fh.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
            else:
                zf.write(src, arc)
            written += 1
            if written % 50 == 0:
                pct = written * 100 // len(files)
                print(f"   {pct}%  {arc}")

    elapsed = time.time() - started
    size = out_zip.stat().st_size
    print(f"\n完成：{out_zip}")
    print(f"   {written} 条，{size / 1024 / 1024:.1f} MB，用时 {elapsed:.0f}s")
    return verify(out_zip, files, rewrites)


def release_identity(name: str = "identity-1.45.json") -> str:
    """``baseline/identity-*.json`` 的出厂态。

    这个文件是**导入时探测结果**的落盘（path / size / sha256 / 条目统计），
    属于开发机那份本体。原样带走有两个问题：

    1. 泄露开发机绝对路径（``D:\\迅雷2盘\\…``）；
    2. 配置加载会把 identity 并回 ``baselines``，玩家一打开就看到
       「已关联 · 380,619 条 / 3.3 GB」这种**假状态**，而本机根本没有那个文件。

    出厂只保留描述性字段（id / label / engine_line），运行时信息一律不给，
    等玩家导入时由 ``baseline_ops.adopt`` 重新探测写入。

    **但 ``code_layer`` 必须保留**（这条是 4.7 专有，也是最容易搞错的）：

    验证标志绑定的 ``code_digest`` 就是 ``code_layer.digest``。如果把它剥掉：

    - 启动器算不出当前代码层指纹 → ``verify_mark.judge`` 一律判「无法判定」；
    - ``jjm_pack seal`` 也拒绝出记录（拿不到 digest）；
    - 结果就是**所有 4.7 扩展都无法被验证**，完全校验档形同虚设。

    代码层指纹不是「本机信息」——它描述的是**那份发布包的内容**，
    每台机器上都该是同一个值。所以它该随包走。
    """
    raw = _read_json(DIST / "baseline" / name, {})
    is47 = "4.7" in name
    keep = {
        "schema": 1,
        "id": str(raw.get("id") or ("jjxf.v4.7" if is47 else "jjxf.v1.45")),
        "label": str(
            raw.get("label")
            or ("机甲旋风 4.7（剧情版 · Godot 4.7.2）" if is47 else "机甲旋风 1.45（原版 · Godot 4.6）")
        ),
        "engine_line": str(raw.get("engine_line") or ("4.7" if is47 else "4.6")),
    }

    code_layer = raw.get("code_layer") or {}
    if isinstance(code_layer, dict) and code_layer.get("digest"):
        # 只保留判定需要的：digest + 逐文件表。**不保留**任何带路径的字段。
        files = code_layer.get("files") or {}
        keep["code_layer"] = {
            "source": str(code_layer.get("source") or "body"),
            "entry_count": int(code_layer.get("entry_count") or len(files)),
            "digest": str(code_layer["digest"]),
            "files": {
                str(k): {"sha256": str((v or {}).get("sha256") or ""), "size": int((v or {}).get("size") or 0)}
                for k, v in files.items()
            },
        }
        # 兼容 busy 情况：文件表太大时可以只发 digest（判定仍可用）。
        # 4.7 实测 252 个条目约 41 KB，直接发没问题。
    return _dump(keep)


def release_index() -> bytes:
    """``baseline/index-1.45.json.gz`` 的出厂态：抹掉本机来源路径。

    索引本身（header + 全部条目表）是**要随包发**的：玩家导入本体后
    靠它做路径比对、扩展兼容性探测，没它就得自己跑一遍 make_index。

    但 ``source`` 字段记的是开发机那份 exe 的绝对路径
    （``D:\\迅雷2盘\\…``），属于不该发出去的本机信息。该字段只写不读
    （见 ``baseline_ops.rebuild_index`` / ``tools/make_index.py`` 的写入点，
    ``baseline.load_index`` 从不引用它），所以换成中性占位即可。
    """
    src = DIST / "baseline" / "index-1.45.json.gz"
    with gzip.open(src, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "source" in data:
        data["source"] = "game/mchGame.exe"
    buf = io.BytesIO()
    # mtime=0：让同样的输入产出同样的字节，避免每次打包都变哈希
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return buf.getvalue()


def verify(out_zip: Path, files: list[tuple[Path, str]], rewrites: dict[str, str | bytes]) -> int:
    """回读校验：条目数、清空的清单、剧情 pck、exe 哈希、无玩家数据。"""
    problems: list[str] = []
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
        if len(names) != len(files):
            problems.append(f"条目数不符：{len(names)} != {len(files)}")

        try:
            reg = json.loads(zf.read(f"{ARC_ROOT}/config/extensions.json"))
            if reg.get("items"):
                problems.append(f"extensions.json 没清空：{len(reg['items'])} 条")
        except KeyError:
            problems.append("缺少 config/extensions.json")

        # 随包反解源码必须带上（**每一条内置线各一份**）。
        #
        # 缺了它，基线 pck 里只有 .gdc 没有 .gd，所有 text_patches、合并层与
        # 钩子插桩都会拿不到原文——1.6 首发包就是这么让 19 个扩展集体失效的。
        # 4.7 线过去只带了 sources-4.7.zip 却没人读，现在两条线都必须有。
        for line in ("4.6", "4.7"):
            rel = f"{ARC_ROOT}/baseline/sources-{line}.zip"
            if rel not in names:
                # 4.6 兼容旧命名：只要有 canonical_sources.zip 也算有源码，
                # 但新包必须带线专属名（多线共存时不会互相顶掉）。
                legacy = f"{ARC_ROOT}/baseline/canonical_sources.zip"
                if line == "4.6" and legacy in names:
                    problems.append(
                        "建议把 baseline/canonical_sources.zip 改名为 sources-4.6.zip"
                        "（新代码优先读线专属名；旧名仍能读，但会和多线的 4.7 混淆）"
                    )
                    rel = legacy
                else:
                    problems.append(
                        f"缺少 baseline/sources-{line}.zip"
                        f"（该线的脚本补丁与钩子插桩会全部失效）"
                    )
                    continue
            try:
                with zipfile.ZipFile(io.BytesIO(zf.read(rel))) as cz:
                    scripts = [
                        n
                        for n in cz.namelist()
                        if n.startswith("scripts/") and n.endswith(".gd")
                    ]
                if len(scripts) < 100:
                    problems.append(
                        f"{rel.rsplit('/', 1)[-1]} 里只有 {len(scripts)} 个脚本（预期 ≥100 个）"
                    )
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{rel.rsplit('/', 1)[-1]} 无法读取：{exc}")

        # 发布包里不许出现开发机的绝对路径（盘符）—— 以前 identity.json 里
        # 就带着 ``D:\迅雷2盘\…``，那是明确不该发出去的东西。
        for arc_name in (
            f"{ARC_ROOT}/config/launcher.json",
            f"{ARC_ROOT}/baseline/identity-1.45.json",
            f"{ARC_ROOT}/engines/engines.json",
        ):
            try:
                text = zf.read(arc_name).decode("utf-8")
            except KeyError:
                problems.append(f"缺少 {arc_name}")
                continue
            if re.search(r"[A-Za-z]:[\\/]", text):
                problems.append(f"{arc_name} 里带着本机绝对路径（盘符）")

        # 基线探测结果（size / sha256 / entry_count）是开发机那份本体的，
        # 出厂态留着会让玩家一打开就看到「已关联 380,619 条」的假状态。
        try:
            ident = json.loads(zf.read(f"{ARC_ROOT}/baseline/identity-1.45.json"))
            leaked = [k for k in ("path", "size", "sha256", "entry_count") if k in ident]
            if leaked:
                problems.append(
                    "identity-1.45.json 带着开发机探测结果：" + "、".join(leaked)
                )
        except KeyError:
            pass

        # 索引的 ``source`` 也不能带本机路径（这个字段只写不读，但会泄露盘符）
        try:
            raw = zf.read(f"{ARC_ROOT}/baseline/index-1.45.json.gz")
            idx = json.loads(gzip.decompress(raw).decode("utf-8"))
            src = str(idx.get("source") or "")
            if re.search(r"[A-Za-z]:[\\/]", src):
                problems.append(f"索引里的 source 带着本机路径：{src}")
            if not idx.get("entries"):
                problems.append("索引为空（玩家导入后无法做路径比对）")
        except KeyError:
            problems.append("缺少 baseline/index-1.45.json.gz")
        except (OSError, ValueError) as exc:
            problems.append(f"索引读取失败：{exc}")

        # 玩家数据：这几类**一个条目都不该有**
        for banned in ("profiles/", "builds/", "cache/", "logs/"):
            hits = [n for n in names if f"/{banned}" in n]
            if hits:
                problems.append(f"混进了玩家数据：{banned}（{len(hits)} 条）")

        # packs/：剧情与扩展覆盖层都不随包发，这里必须一条都没有
        pack_hits = [n for n in names if "/packs/" in n]
        if pack_hits:
            problems.append(
                f"packs/ 下不该有条目（{len(pack_hits)} 条）："
                + "、".join(x.replace(f"{ARC_ROOT}/", "") for x in pack_hits[:4])
            )

        # 剧情注册表：剧情功能已移除，出厂必须为空
        try:
            stories = json.loads(zf.read(f"{ARC_ROOT}/config/stories.json"))
            items = stories.get("items") or []
            if items:
                problems.append(f"stories.json 应为空，实际 {len(items)} 条")
        except KeyError:
            problems.append("缺少 config/stories.json")

        # 出厂 launcher：不能出现开发机盘符路径
        try:
            launcher = json.loads(zf.read(f"{ARC_ROOT}/config/launcher.json"))
            text = json.dumps(launcher, ensure_ascii=False)
            for token, label in (("D:\\\\", "D 盘路径"), ("C:\\\\", "C 盘路径"), ("Desktop", "桌面路径")):
                if token in text:
                    problems.append(f"launcher.json 仍含本机{label}")
                    break
            for line in ("4.6",):
                path = str(((launcher.get("engines") or {}).get(line) or {}).get("path") or "")
                if not path or ":" in path:
                    problems.append(f"engines.{line}.path 不是包内相对路径：{path!r}")
            for bid, meta in (launcher.get("baselines") or {}).items():
                raw = str((meta or {}).get("path") or "")
                if raw and ":" in raw:
                    problems.append(f"baselines.{bid}.path 仍是本机绝对路径：{raw!r}")
            mode = str((launcher.get("extensions") or {}).get("extension_mode") or "")
            # 两个版本的出厂档不同，这里按 edition 核对：
            # 开发者版必须是 strict，否则「完全校验」等于没开。
            expect = "strict" if _CURRENT_EDITION == "dev" else "loose"
            if mode != expect:
                problems.append(
                    f"扩展隔离模式与版本不符：{mode!r}"
                    f"（{_CURRENT_EDITION} 版应为 {expect}）"
                )
        except KeyError:
            problems.append("缺少 config/launcher.json")

        leaked = [
            n for n in names
            if any(token in n for token in BANNED_DOC_TOKENS)
        ]
        if leaked:
            problems.append(
                f"混进了内部文档（{len(leaked)} 条）：" + "、".join(x.split("/")[-1] for x in leaked[:4])
            )

        # 只数**顶层** ``<ARC_ROOT>/docs/``，不能把
        # ``<ARC_ROOT>/扩展开发文档/公共文档/`` 也算进来 ——
        # 开发包自带一套文档，混进来会让条目数永远对不上。
        top_docs_prefix = f"{ARC_ROOT}/docs/"
        doc_names = {
            n[len(top_docs_prefix):]
            for n in names
            if n.startswith(top_docs_prefix)
        }
        if len(doc_names) != len(DOC_ALLOWLIST):
            problems.append(
                f"docs 条目数不符：{len(doc_names)} != 白名单 {len(DOC_ALLOWLIST)}"
            )
        # 开发者须知已移入开发包（`扩展开发文档/公共文档/`），这里按新位置校验。
        # 它必须随包发：里面有工程约定与问题台账，缺了开发者无从下手。
        for required in ("玩家须知.md", "扩展开发文档/公共文档/免责声明.md"):
            if f"{ARC_ROOT}/{required}" not in names:
                problems.append(f"缺少文档：{required}")

        # exe 名用常量派生，别写死 —— 改名后这里漏改会让校验永远报
        # 「缺少启动器 exe」，而实际包是好的，排查起来很绕。
        exe_file = f"{PRODUCT_NAME}.exe"
        exe_arc = f"{ARC_ROOT}/{exe_file}"
        if exe_arc not in names:
            problems.append(f"缺少启动器 exe（找的是 {exe_file}）")
        else:
            local = hashlib.sha256((DIST / exe_file).read_bytes()).hexdigest()
            packed = hashlib.sha256(zf.read(exe_arc)).hexdigest()
            if local != packed:
                problems.append("exe 与 dist 里的不一致")

        # 重写过的配置：回读比对，确保真的按出厂态写进去了
        for arc, expected in rewrites.items():
            try:
                actual = zf.read(arc)
            except KeyError:
                problems.append(f"缺少重写配置：{arc}")
                continue
            if isinstance(expected, bytes):
                # 二进制重写（如 gzip 索引）：逐字节比对
                if actual != expected:
                    problems.append(f"二进制重写内容不一致：{arc}")
                continue
            if json.loads(actual.decode("utf-8")) != json.loads(expected):
                problems.append(f"配置未按出厂态写入：{arc}")

    if problems:
        print("\n校验未通过：")
        for p in problems:
            print("  ✗", p)
        return 1
    print("\n校验通过：条目数一致、扩展清单为空、packs/ 为空、无玩家数据、文档齐全")
    return 0


#: 产物文件名。版本号集中在这里，改版本只改这一处。
#:
#: 1.7 = 双线启动器（4.6 原版 + 4.7 剧情版）。1.6 是单线版本，
#: 发布名继续叫 1.6 会让玩家分不清拿到的是不是新版。
RELEASE_VERSION = "1.5"

#: 对外**只发一个包**（2026-11-08 决策）。
#:
#: 原来按 edition 分 player / dev 两个产物，玩家要选「我该下哪个」，
#: 作者要维护两份配置。现在合并成一个：同一份 exe，差别只在
#: 「设置 → 开发者模式」开不开。
#:
#: 保留 dict 结构与 ``--edition`` 参数，是因为这个脚本还要支持
#: 「只想打个能跑的包快速验证」的临时场景；但**对外发布只认 unified**。
EDITIONS = {
    # 对外发布名：就是产品名，不带版本号（版本在程序里和文档里）。
    "unified": f"{PRODUCT_NAME}.zip",
    # 下面两个是内部调试用的临时产物，不对外发布
    "player": f"{PRODUCT_NAME}{RELEASE_VERSION}-player.zip",
    "dev": f"{PRODUCT_NAME}{RELEASE_VERSION}-dev.zip",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="打启动器发布 zip")
    ap.add_argument("--edition", choices=sorted(EDITIONS), default="player",
                    help="player=玩家版（默认），dev=开发者版")
    ap.add_argument("-o", "--out", default=None,
                    help="输出路径；缺省按 --edition 命名到仓库根")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 核对 dist 真正烧进去的版本常量（不是打包脚本自己写的标记）。
    # spec 会把生成的 _build_edition.py 同时放进 _internal/app/，就为这一步。
    baked_file = DIST / "_internal" / "app" / "_build_edition.py"
    if not baked_file.exists():
        print(
            "✗ dist 缺少 _internal/app/_build_edition.py：\n"
            "  这份 dist 不是带版本烧入的新流程构建的，无法确认是玩家版还是开发者版。\n"
            f"  请先用 JJXF_EDITION={args.edition} 重新跑 PyInstaller。"
        )
        return 2
    baked = ""
    for line in baked_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("EDITION"):
            baked = line.split("=", 1)[-1].strip().strip('"').strip("'")
            break
    if baked != args.edition:
        print(f"✗ dist 烧入的是 {baked or '未知'} 版（{baked_file}），与 --edition {args.edition} 不符")
        return 2

    out = Path(args.out) if args.out else ROOT / EDITIONS[args.edition]
    return build(out, dry_run=args.dry_run, edition=args.edition)


if __name__ == "__main__":
    raise SystemExit(main())
