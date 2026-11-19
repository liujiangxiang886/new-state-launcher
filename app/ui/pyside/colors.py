"""当前主题的取色入口。

为什么要有这一层
----------------

界面里原先散落着一批 GitHub 深色系的十六进制颜色（``#3fb950``、``#d29922``、
``#f85149``、``#9aa3ae``……）。它们和「柳江酱紫」主色不是一套：整体看是紫底
上突然冒出来的纯绿/纯橙，既刺眼，也不跟主题走——切到浅色主题时这些颜色
依旧是深色系，对比度全错。

改成统一从这里取色后：

- 颜色跟着 :data:`app.ui.pyside.theme.Palette` 走，深/浅主题都成立；
- 状态色只有一处定义，不会出现「同一个绿三种色号」。

:func:`set_palette` 由 ``app.py`` 在选定主题后调用一次。
"""

from __future__ import annotations

from . import theme

#: 当前生效的配色（默认深色，与 app.py 的默认一致）
_active: theme.Palette = theme.DARK


def set_palette(palette: theme.Palette) -> None:
    global _active
    _active = palette


def palette() -> theme.Palette:
    return _active


# ---------------------------------------------------------------- 单色


def text() -> str:
    return _active.text


def dim() -> str:
    return _active.text_dim


def faint() -> str:
    return _active.text_faint


def accent() -> str:
    return _active.accent


def accent_hover() -> str:
    return _active.accent_hover


def ok() -> str:
    return _active.success


def warn() -> str:
    return _active.warn


def err() -> str:
    return _active.danger


# ---------------------------------------------------------------- 组合


def tint(color: str, alpha: float) -> str:
    """把 ``#rrggbb`` 变成半透明 ``rgba(...)``，用作徽标/提示条的底色。"""
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except (ValueError, IndexError):
        return color
    return f"rgba({r},{g},{b},{alpha:.2f})"


def tone_colors(tone: str) -> tuple[str, str]:
    """状态名 → (底色, 文字色)。底色是状态色的低透明度版本。"""
    fg = {
        "ok": ok(),
        "warn": warn(),
        "err": err(),
        "info": accent(),
        "muted": faint(),
    }.get(tone, faint())
    return tint(fg, 0.16), fg
