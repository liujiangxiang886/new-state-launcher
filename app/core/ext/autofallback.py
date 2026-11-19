"""启动失败自动兜底：让玩家**看不到**「游戏起不来」。

要解决的场景
============

玩家自己装了额外 mod → 游戏起不来 → 他不知道是自己装错了 → 拍照发群说
「启动器是不是坏了」→ 整个群开始怀疑。

**核心诉求不是「告诉他哪里错了」，而是「不要让他看到起不来这件事」。**

为什么不用「启动前预检」
========================

| 方案 | 玩家感受 |
|---|---|
| 启动前跑 headless 冒烟 | **多等 20~60 秒**，界面像卡死 → 换成「启动器怎么这么卡」的截图 |

实测 headless 启动到退出约 23 秒（含 3 GB 包读取），机械盘更久。
这不是优化问题，是量级问题 —— 把「起不来」换成「卡住」，问题没解决。

采用的方案：启动后观察 + 自动降级重启
=====================================

```
玩家点「开始游戏」
  ↓
立即拉起游戏进程（0 额外延迟，与现在完全一致）
  ↓
启动器挂一个 3 秒观察定时器（并行，不阻塞）
  ↓
┌─ 3 秒内进程退出 且 返回码异常 → 判定「mod 导致启动失败」
│    ↓ 自动降级 → 自动重启一次
│    ↓ 界面提示：「检测到扩展冲突，已自动停用 2 个扩展并重启」
│
└─ 3 秒后进程仍在 → 正常，什么也不做（零打扰）
```

**判据依据**：Godot 的脚本解析失败（``Parse Error``）发生在 autoload
阶段，进程会**秒退**，连主场景都进不去。所以「3 秒内退出」是极强信号。

三级降级
========

| 级 | 动作 | 玩家代价 |
|---|---|---|
| **L1** | 停用「非官方组合」的包（无验证记录的） | 只少自己加的 |
| **L2** | 全部停用（回到裸原版） | 所有 mod 不生效，但**能进游戏** |
| **L3** | 仍失败 → 判定为本体/启动器问题 | 弹「一键恢复原版 + 导出诊断包」 |

L1 是重点：玩家自己加的包被停用，官方组合全部保留。这既解决了
「起不来」，又不会让玩家觉得「我的 mod 都没了」。

**同一组合只自动重试一次**，防死循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 观察窗口。Godot 的 Parse Error 在 autoload 阶段秒退，3 秒足够。
#:
#: 为什么不更长：观察期越长，越可能出现「玩家自己关窗口」被误判成失败。
#: 3 秒内退出这件事本身已经很不正常（正常启动 3 秒还在加载资源）。
OBSERVE_SECONDS = 3.0

#: 返回码为 0 的「秒退」不触发降级。
#:
#: 那种情况多半是玩家立刻点了关闭，或者游戏自己优雅退出 —— 不是启动失败。
#: 宁可漏判一次，也不要误伤（误伤的表现是「我明明能玩，启动器却把我的
#: mod 停掉了」，比不处理更让人困惑）。
BENIGN_CODES = (0,)

#: 降级级别。
LEVEL_NONE = "none"
LEVEL_UNOFFICIAL = "L1"   # 只停「玩家自己加的」
LEVEL_ALL = "L2"          # 全停，回到裸原版
LEVEL_MANUAL = "L3"       # 判定为本体/启动器问题，交人工


@dataclass
class Fallback:
    """一次兜底决策。"""

    level: str = LEVEL_NONE
    #: 本次要**临时停用**的扩展 id（不改玩家的启用状态）
    disable: list[str] = field(default_factory=list)
    #: 给玩家看的一句话
    message: str = ""
    #: 是否需要玩家介入（L3）
    manual: bool = False

    @property
    def active(self) -> bool:
        return self.level != LEVEL_NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "disable": list(self.disable),
            "message": self.message,
            "manual": self.manual,
        }


def crashed_early(summary: dict) -> bool:
    """这次退出算不算「启动失败」。

    ``summary`` 来自 ``_on_instance_finished``（含 returncode / seconds / killed）。

    判据三条同时成立：
    1. **3 秒内退出**（``seconds`` 有值且小于窗口）；
    2. **不是手动结束**（``killed`` 为假）；
    3. **返回码不是 0**。

    为什么不含「3 秒后退出的失败」：那多半是游戏内崩溃（玩了一会儿才崩），
    停用 mod 也解决不了，反而会让玩家莫名其妙少东西。
    """
    if summary.get("killed"):
        return False
    try:
        secs = float(summary.get("seconds") or 0)
    except (TypeError, ValueError):
        return False
    if secs <= 0 or secs > OBSERVE_SECONDS:
        return False
    code = summary.get("returncode")
    if code is None:
        return False
    try:
        if int(code) in BENIGN_CODES:
            return False
    except (TypeError, ValueError):
        return False
    return True


def decide(
    *,
    crashes: int,
    unverified: list[str],
    misplaced: list[str] | None = None,
    already_retried: bool = False,
) -> Fallback:
    """算出这一轮该降级到哪一级、停用谁。

    ``crashes`` 是这个组合累计秒退次数（含本次）。
    ``unverified`` 是当前组合里没有有效验证记录的扩展 id。
    ``misplaced`` 是「把别的线的包装到这条线」的 id（可选，优先停这些）。
    ``already_retried`` 表示这个组合已经自动重试过一次。

    决策表：

    | 情况 | 级别 | 停用 |
    |---|---|---|
    | 第一次秒退，有非官方包 | L1 | 非官方 + 装错线的 |
    | 第一次秒退，全是官方包 | L2 | 全部 |
    | 已重试过仍秒退 | L2 → L3 | 先全停；全停还崩就转人工 |
    | 已重试过、全停也崩 | L3 | —（本体问题） |
    """
    misplaced = list(misplaced or [])

    # 已经全停过还崩 → 不是扩展的问题
    if already_retried and crashes >= 2:
        return Fallback(
            level=LEVEL_MANUAL,
            message=(
                "已停用全部扩展但仍无法启动，问题多半出在游戏本体或启动器本身。"
                "请点「修复」重新导入游戏本体；如果仍然不行，请导出诊断包反馈。"
            ),
            manual=True,
        )

    if already_retried:
        return Fallback(
            level=LEVEL_ALL,
            message="上次自动重试仍未成功，这次先停用全部扩展，保证能进游戏。",
        )

    # 第一次：优先只停「玩家自己加的」
    suspects = sorted({*misplaced, *unverified})
    if suspects:
        return Fallback(
            level=LEVEL_UNOFFICIAL,
            disable=suspects,
            message=(
                f"检测到启动失败，已自动停用 {len(suspects)} 个未经验证的扩展并重启。"
                "官方组合内的扩展未受影响。"
            ),
        )

    return Fallback(
        level=LEVEL_ALL,
        message=(
            "检测到启动失败，已停用全部扩展并重启。"
            "进游戏后可在扩展页逐个开启，找出有问题的那个。"
        ),
    )


def should_retry(fallback: Fallback) -> bool:
    """这一级要不要真的重启一次。L3 不重启（那是本体问题，重启也没用）。"""
    return fallback.active and not fallback.manual