"""发行版本标记（构建期常量）。

玩家版与开发者版**同源**，差异只在打包时烧进来的这一行：

- ``dev``    ：三档校验、双开测试、反解工程、完整诊断包；
- ``player`` ：普通 + 宽松校验、单实例、无开发者入口。

打包时 ``机甲旋风兼容启动器.spec`` 会读环境变量 ``JJXF_EDITION``，
生成 ``app/_build_edition.py``（内容就是 ``EDITION = "player"`` / ``"dev"``），
冻结后从这里导入。**源码态永远按 dev 跑**——否则在仓库里跑一次玩家版
构建，后续所有源码调试都会莫名其妙少掉开发者功能。

不要改成从配置文件读——玩家改一行配置就能解锁，那就不叫「不开放」了。
"""

from __future__ import annotations

import sys

if getattr(sys, "frozen", False):
    # 冻结态：读 spec 生成的模块。生成失败时回落到 dev（宁可多给功能，
    # 也不要把开发者版错标成玩家版——后者会让作者以为自己的功能丢了）。
    try:
        from ._build_edition import EDITION  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 生成文件缺失不该让启动器起不来
        EDITION = "dev"
    # BUILD_ID 是后加的：老包没有这个常量，单独兜底，不能连累 EDITION。
    try:
        from ._build_edition import BUILD_ID  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        BUILD_ID = "unknown"
else:
    EDITION = "dev"
    BUILD_ID = "dev"

PLAYER = "player"
DEV = "dev"
#: 发布者版：功能集与开发者版一致（三档校验/完整诊断/反解工程），
#: 但版本标记独立，界面显示「发布者版」。用于作者自己的整合发行。
PUBLISHER = "publisher"
#: 统一版（2026-11-08 起的**对外唯一发布版本**）。
#:
#: 不再按身份分玩家版/开发者版：同一份 exe，开发者工具靠设置项开
#: （``ui.developer_mode``）。这样玩家拿到的就是全部功能，作者也不用
#: 维护两份构建配置。
#:
#: **不是** PLAYER —— ``config.netplay_open()`` 里 ``is_player()`` 是一道
#: 硬闸门（联机协议永不开），统一版不能落进那一支。
UNIFIED = "unified"

#: 产品名（2026-11-08 统一命名）。
#:
#: **改名字只改这一行。** 原来「机甲旋风兼容启动器」散落在窗口标题、
#: 打包脚本、文档十几处，改名时容易漏。
#:
#: 2026-11-11 按需求改回中文「新起点启动器」。注意这里只管**界面显示名**：
#: ``EXE_STEM`` / spec 的 ``_EXE_NAME`` / 发布包文件名仍保持英文，改文件名会
#: 牵动卸载判定、快捷方式与已装目录，属于另一次独立变更。
#:
#: 对外只发这一个产品名，不再区分「玩家版 / 开发者版」——
#: 同一份 exe，差别只在「设置 → 开发者模式」开不开。
APP_NAME = "新起点启动器"

#: 程序文件名（不带 .exe）。
#:
#: 与 ``APP_NAME`` 保持一致，但**单独定义**：改名时可能只想改显示名
#: 而不改文件名（比如老版本的快捷方式还指向旧 exe）。
#: 冻结态下 ``sys.executable`` 才是准的，这里只用于「期望值」比对。
EXE_STEM = "New state launcher"


def app_name() -> str:
    """界面与打包用的产品名。"""
    return APP_NAME


def exe_name() -> str:
    """程序文件名（含 .exe）。

    卸载逻辑要判断「哪些文件属于程序本体」，CLI 用法提示要写命令示例，
    两处都得跟真实文件名一致。集中在这里，改名只改 :data:`EXE_STEM`。
    """
    return f"{EXE_STEM}.exe"


def exe_names() -> tuple[str, ...]:
    """程序文件名的**全部历史别名**（新名在前）。

    卸载与目录清理要认老名字：玩家从 1.x 升到 1.7 时，旧的
    ``机甲旋风兼容启动器.exe`` 可能还留在目录里，不认它就清不掉。
    """
    return (f"{EXE_STEM}.exe", "机甲旋风兼容启动器.exe")


def is_player() -> bool:
    return EDITION == PLAYER


def is_dev() -> bool:
    return EDITION != PLAYER


def is_publisher() -> bool:
    """发布者版：功能集与开发者版一致，但独有一些「作者自用」开关。

    目前只有联机协议闸门（``extensions.netplay_open``）属于这一类——
    玩家版 / 开发者版不允许开放，见 :func:`app.core.config.netplay_open`。
    """
    return EDITION == PUBLISHER


def label() -> str:
    """版本标签。

    2026-11-08 起统一命名：「新起点启动器」，不再区分玩家版/开发者版
    —— 同一份包，差别只在设置里的「开发者模式」开不开。

    保留旧的三档返回值是因为还有代码按它判断能力边界
    （``is_player`` / ``is_publisher``），那些判断本身没变。
    """
    return APP_NAME


def build_id() -> str:
    """本次构建的标识（打包时烧入）。

    用途只有一个：区分「这一份 exe」和「包根说明文件」是不是同一次构建。
    解压时覆盖旧目录会造成新旧文件混装，靠版本号看不出来，靠它才行。
    """
    return BUILD_ID
