"""启动前的内存预检：在真的把游戏拉起来之前，判断这台机器撑不撑得住。

为什么需要它
============

4.7 剧情版的实测内存（2026-11-11 采样，`_build_tools/memlog/mem-*.txt`）：

    峰值工作集      6765 MB
    峰值提交        9972 MB
    末段稳态        6270 MB
    系统最低可用    2.93 GB

也就是说它要**提交**约 9.7 GB。8 GB 物理机（8192 MB）光物理就差
1780 MB，靠页面文件顶 —— 机械盘上表现为「选完角色进游戏时整个界面
冻住几十秒」，玩家描述成「卡死」。

启动器过去对此**一句话都不说**：玩家点启动，等两分钟，看到黑屏或
闪退，然后以为启动器坏了。这条检查的唯一目的，是把「撑不住」这个
事实在点下去之前说清楚，并给一条可操作的出路。

设计原则
========

1. **只警告，不阻断。** 4.6 线在 6 GB 机器上确实能跑；而且页面文件
   开大之后 8 GB 也能玩（只是慢）。硬拦会把能玩的人挡在外面 ——
   那是比「卡」更糟的结果。所以这里产出的是**提示**，交给界面弹
   「仍然启动 / 取消」。
2. **拿不到数据就当没问题。** `GlobalMemoryStatusEx` 失败时返回 0，
   此时不做任何判断。宁可少提醒一次，也不要凭猜测拦人。
3. **按线给不同门槛。** 4.7 要 9.7 GB 提交，4.6 实测约 4.5 GB。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MemoryVerdict:
    """一次内存预检的结论。"""

    #: 是否有必要提示玩家。False 表示「不必打扰」。
    warn: bool = False
    #: 一句话结论（给界面标题用）。
    title: str = ""
    #: 展开说明（多行，写清数字与建议）。
    detail: str = ""
    #: 物理内存总量 / 可用量（字节），拿不到为 0。
    total: int = 0
    avail: int = 0
    #: 这条线建议的可用内存下限（字节）。
    need: int = 0

    def as_dict(self) -> dict:
        return {
            "warn": self.warn,
            "title": self.title,
            "detail": self.detail,
            "total": self.total,
            "avail": self.avail,
            "need": self.need,
        }


#: 每条线建议的「可用物理内存」下限。数值来自实测峰值工作集再留一点余量
#: （工作集之外还有驱动/系统缓存要占地方）。
#:
#: - 4.7：峰值工作集 6765 MB → 取 7.0 GiB
#: - 4.6：实测峰值工作集约 4.2 GB → 取 4.5 GiB
NEED_BY_LINE: dict[str, int] = {
    "4.7": int(7.0 * 1024**3),
    "4.6": int(4.5 * 1024**3),
}

#: 默认门槛（未知线）。
DEFAULT_NEED = int(4.5 * 1024**3)


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _total_physical() -> int:
    """物理内存总量（字节）。拿不到返回 0。"""
    import ctypes
    import os

    if os.name != "nt":
        return 0
    try:
        from . import memtrim

        status = memtrim._MEMORYSTATUSEX()          # noqa: SLF001 - 复用同一份结构体定义
        status.dwLength = ctypes.sizeof(status)
        if not memtrim._kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # noqa: SLF001
            return 0
        return int(status.ullTotalPhys)
    except Exception:  # noqa: BLE001 - 探测失败不该影响启动
        return 0


def check(line: str) -> MemoryVerdict:
    """检查这条线现在能不能舒服地跑起来。

    只在「可用物理内存明显不够」时返回 ``warn=True``。
    """
    from . import memtrim

    total = _total_physical()
    avail = memtrim.available_physical()
    need = NEED_BY_LINE.get(str(line), DEFAULT_NEED)

    if not total or not avail:
        # 探测失败：不判断，也不打扰
        return MemoryVerdict(total=total, avail=avail, need=need)

    if avail >= need:
        return MemoryVerdict(total=total, avail=avail, need=need)

    # ---- 需要提示
    gap = need - avail
    title = f"可用内存偏少（{_human(avail)} / 建议 {_human(need)}）"

    lines = [
        f"当前可用物理内存：{_human(avail)}（总 {_human(total)}）",
        f"这条线实测需要：约 {_human(need)}",
        "",
        "能启动，但可能出现：",
        "· 选完角色进图时卡住几十秒（内存换页，机械硬盘更明显）",
        "· 战斗中偶发长停顿、贴图加载慢",
    ]
    if gap > 0:
        text = _shortage_advice(total, avail, need)
        lines += ["", text]

    return MemoryVerdict(
        warn=True,
        title=title,
        detail="\n".join(lines),
        total=total,
        avail=avail,
        need=need,
    )


def _shortage_advice(total: int, avail: int, need: int) -> str:
    """给一条能照着做的建议，而不是只说「内存不够」。"""
    tips = ["建议（任选其一，都能明显缓解）："]

    others = max(0, total - avail)
    if others > total * 0.55:
        tips.append(
            f"· 关掉浏览器/视频等占内存的程序 —— 现在它们占了约 {_human(others)}"
        )
    tips.append("· 调大系统页面文件（虚拟内存）到 12 GB 以上，给换页留出空间")
    tips.append("· 在「设置 → 启动前释放系统内存」勾上，先把别的程序的冷页换出")
    tips.append("· 换 4.6 线游玩（同一存档不通用，但内存约为 4.7 的六成）")
    return "\n".join(tips)