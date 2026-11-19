"""界面能力开关：哪些东西只给开发者看。

判断只有一处，避免每个页面各写一套 ``if``。

2026-11-08 起：**单包发布 + 设置项开关**。

原来按 ``edition`` 分三档（player / dev / publisher），各自隐藏不同的
入口。改成一个包之后按 edition 分档没有意义了 —— 同一份 exe 里，
玩家和开发者只差「要不要看到技术细节」。

所以改成配置开关：

============================  ============================================
开关                          默认
============================  ============================================
``ui.developer_mode``         关闭。开启后露出反解工程、详细诊断等技术项
``ui.show_detail``            关闭。诊断页只显示「每个版本一行」
``双开测试``                  **已移除**（2026-11-08 用户决策）
============================  ============================================

为什么不用按 edition 判断：开发者版也要能当玩家用（自测时看玩家视角），
玩家偶尔也要看细节（报问题时截图）。用开关，两种身份可以随时切换。
"""

from __future__ import annotations

from ... import edition
from ...core import config as cfg_mod


def developer_only(cfg: dict) -> bool:
    """当前是否处于「可以看见开发者工具」的状态。

    **改为读设置项**（``ui.developer_mode``），默认关闭。

    历史：这个方法原来按 ``edition`` 判断。单包化之后 edition 恒为
    同一个值，按它判断等于写死 —— 要么所有玩家都看到「反解工程」，
    要么开发者永远看不到。设置项才是对的粒度。

    保留 edition 兜底：玩家版（如果以后还要单独出）恒为 False，
    避免玩家手动改配置解锁开发者工具。
    """
    if edition.is_player():
        # 玩家版恒 False —— 配置里写什么都不算数。
        # 与 netplay_open 同一个思路：能被手改解锁的「限制」等于没有。
        return False
    raw = (cfg.get("ui") or {}).get("developer_mode")
    if raw is not None:
        return bool(raw)
    return False


def show_dual_instance(cfg: dict) -> bool:
    """是否显示「双开测试」面板。

    **恒 False —— 双开功能已移除（2026-11-08 用户决策）。**

    保留这个函数而不是删掉，是因为调用点分布在
    ``library.py``（2 处）和 ``features.max_instances()`` 里。
    删函数要同时改所有调用点，容易漏；恒 False 是等价且更安全的做法。

    为什么砍：双开是「本机对照测试」用的，属于作者自用能力。
    单包发布后所有玩家都会看到这个入口，而它对玩家没有意义 ——
    反而要理解「什么是槽位」「为什么有两个存档」。
    """
    return False


def show_source_project(cfg: dict) -> bool:
    return developer_only(cfg)


def show_detail(cfg: dict) -> bool:
    """是否显示**技术细节**（诊断页展开全部检查项、路径、字段名等）。

    为什么做成设置项而不是写死
    --------------------------

    这两类人要在同一份代码上共存：

    | 谁 | 想要 | 看到 11 行技术明细的反应 |
    |---|---|---|
    | 玩家 | 知道「这两个版本能不能玩」 | 「这软件好复杂，我是不是搞坏了什么」 |
    | 开发者/维护者 | 知道「索引在不在、指纹对不对」 | 「怎么只有两行，什么都看不到」 |

    按 edition 写死做不到这件事：**开发者版也要能当玩家用**
    （自测时想看玩家视角），**玩家版偶尔也要看细节**（报问题时截图）。

    所以默认值按身份给（两种都默认简洁，因为绝大多数时间是在「玩」），
    要给细节的人自己去设置里开。

    配置键：``ui.show_detail``。显式写了就尊重，没写按下面的默认。
    """
    raw = (cfg.get("ui") or {}).get("show_detail")
    if raw is not None:
        return bool(raw)
    # 默认：简洁。玩家和开发者都先看简洁版 ——
    # 需要细节的是少数时刻，主动去开比每次都被淹没要好。
    return False


def max_instances(cfg: dict) -> int:
    """同时能开几个游戏实例。

    恒为 1 —— 双开已移除。保留函数是因为 ``main_window`` 用它初始化
    ``self.instances`` 槽位表；返回 1 就是「单开」，逻辑照常工作。
    """
    return 1


def available_modes(line: str | None = None) -> tuple[str, ...]:
    """某条引擎线可选的校验档位。

    规则由 :func:`app.core.config.available_modes` 单点决定：

    - **4.7 线**：只有完全校验。剧情会随版本更新，代码层一变旧 mod 就
      不再适配，宽松放行等于让玩家装一堆注定坏的东西。
    - **4.6 线**：宽松 / 完全两档，玩家自选（导入本体后弹窗询问）。

    玩家版同样放开两档 —— 4.6 的老玩家习惯「装错了也能进游戏」，
    不能一上线就把这个退路掐掉。
    """
    return cfg_mod.available_modes(line)