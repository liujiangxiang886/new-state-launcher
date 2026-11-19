"""面向玩家的人话文案。

原则：界面上不出现实现词。异常文本、堆栈、文件名只进日志与诊断包，
玩家只看到「发生了什么、要不要紧、下一步做什么」。

完全校验档（``strict``）可以直接用 ``technical=True`` 拿原始信息。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_TEXT: dict[str, str] = {
    "install.not_found": "找不到这个扩展包。",
    "install.manifest": "扩展包的信息不完整或格式不支持。",
    "install.baseline": "这个扩展不是给当前游戏版本做的。",
    "install.verify": "扩展与当前游戏版本对不上。",
    "install.unpack": "扩展包解压失败，文件可能损坏。",
    "install.corrupt": "安装包已损坏，请重新下载。",
    "install.longpath": "安装路径过长（Windows 限制）。请把启动器移到更浅的目录，或升级启动器版本。",
    "install.netguard": "这个扩展包含联机功能，当前版本不支持。",
    "install.isolation": "扩展与游戏其它部分互相干扰，已被拦下。",
    "install.overlay": "扩展与当前启动器版本不兼容。",
    "install.pck": "扩展安装后校验没通过，文件可能损坏。",
    "install.smoke": "扩展装上后游戏无法正常启动，已自动撤销。",
    "install.register": "扩展安装记录写入失败，请检查磁盘空间。",
    "install.unknown": "扩展安装失败。",
    "launch.baseline": "没有找到对应的游戏版本，请先在设置里指定游戏。",
    "launch.runtime": "启动器缺少运行文件，请重新解压启动器。",
    "launch.conflict": "扩展组合有冲突，请先处理冲突再启动。",
    "ext.disabled": "这个扩展已被停用。",
    "ext.unknown": "扩展状态异常。",
}

_HINTS: dict[str, str] = {
    "install.manifest": "可以让作者用 1.6 打包工具重新导出一份。",
    "install.longpath": "包本身没问题，换个目录或升级启动器即可。",
    "install.baseline": "等作者发布对应版本，或把游戏切到该版本。",
    "install.verify": "等作者适配当前版本后再装。",
    "install.overlay": "这个包是按旧启动器做的，等作者用 1.6 重新打包。",
    "install.smoke": "已跳过这个扩展，其它扩展不受影响。",
}


def human(code: str, *, hint: bool = False, **fmt: Any) -> str:
    text = _TEXT.get(code, _TEXT["install.unknown"])
    if fmt:
        try:
            text = text.format(**fmt)
        except (KeyError, IndexError):
            pass
    if hint and code in _HINTS:
        text = f"{text}{_HINTS[code]}"
    return text


def technical(code: str, detail: str) -> str:
    return f"{code}: {detail}" if detail else code


def diagnostic_id(detail: str) -> str:
    """给玩家一个短码，作者据此在日志里定位同一问题。"""
    digest = hashlib.sha1(str(detail or "").encode("utf-8", "replace")).hexdigest()[:6].upper()
    return f"JJ-{digest}"


def safe_detail(exc: object) -> str:
    """异常 → 单行技术描述（只进日志）。"""
    text = str(exc)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]
