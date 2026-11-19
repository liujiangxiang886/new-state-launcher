"""整线组合校验：把某条线上**全部已启用**扩展一起挂起来跑一次。

为什么放在 ``app/core/ext`` 而不是 ``tools/``（踩过的坑）：

    这个函数最初只有 ``tools/verify_mods.py`` 一份实现，界面用
    ``importlib`` 按路径动态加载它。开发态下 ``paths.TOOLS`` 指向仓库的
    ``tools/``，能跑通；但打包成 onedir exe 后 ``paths.project_root()``
    变成「exe 所在目录」，而 ``tools/`` **从来不随发布复制**（spec 的
    ``datas`` 只有 icon）。于是任何已发布的启动器点「整体校验」都会
    抛 ``FileNotFoundError``，功能打包即失效。

    修法不是「把 tools/ 也塞进发布包」（多一份可被用户删掉的运行时
    依赖，且 UI 反向依赖命令行工具本身就是错的分层），而是把逻辑
    下沉到核心层：``app.core.ext`` 已被 PyInstaller 的
    ``collect_submodules`` 覆盖，冻结后天然可用。``tools/verify_mods.py``
    退化为一个只做参数解析的薄包装。

和 ``ExtensionService.install`` 的区别：

* 安装期只验「这一个包能不能装起来」（``mods.json`` 里只有一个成员），
  这样坏包不会连累别人，也能定位到具体是谁；
* 本模块验「这一整套组合能不能一起装起来」——顺序、钩子冲突、
  两个包改同一个文件、A 的补丁覆盖了 B 的覆盖层，只有放到一起才会暴露。

启动器真正要跑的是后者，所以装完还得跑一遍这个，两条判据都过才算可用。
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import paths
from .. import smoke as smoke_mod
from . import enable as enable_mod


def verify_line(cfg: dict, line: str, timeout: float = 240.0) -> dict:
    """组装该线全部已启用扩展 → 冒烟 → 逐项核对装载结果。

    返回可直接序列化 / 直接喂给界面的字典。``ok`` 是唯一判据，
    ``problems`` 是给人看的原因行。
    """
    started = time.time()
    mods = enable_mod.enabled_mods(cfg, line)
    out: dict = {
        "line": line,
        "enabled": [str(m.get("id")) for m in mods],
        "expected": [],
        "loaded": [],
        "missing": [],
        "unexpected": [],
        "failed": [],
        "warnings": [],
        "problems": [],
        "ok": False,
        "reason": "",
        "seconds": 0.0,
        "run_dir": "",
        "packs": [],
        "nodes": 0,
        "game_events": [],
    }
    if not mods:
        # 没装任何扩展不算失败：这条线本来就该裸跑。
        probe = smoke_mod.run(line, timeout=timeout)
        out.update(
            ok=probe.ok,
            reason=probe.summary(),
            seconds=round(time.time() - started, 1),
            run_dir=probe.run_dir,
            loaded=probe.mods_loaded,
            nodes=probe.nodes,
            problems=list(probe.reasons),
        )
        return out

    run_dir = paths.BUILDS / f"_verify_{line.replace('.', '')}"
    launch = enable_mod.build_for_launch(cfg, line, run_dir)
    out["warnings"] = list(launch.warnings)
    out["packs"] = [Path(str(p.get("path"))).name for p in launch.packs]
    out["expected"] = [str(m.get("id")) for m in launch.mods]

    result = smoke_mod.run(
        line,
        packs=launch.packs,
        mods_json=launch.mods_json,
        timeout=timeout,
    )
    out["loaded"] = list(result.mods_loaded)
    out["failed"] = list(result.mods_failed)
    out["missing"] = [i for i in out["expected"] if i not in result.mods_loaded]
    out["unexpected"] = [i for i in result.mods_loaded if i not in out["expected"]]
    out["seconds"] = round(time.time() - started, 1)
    out["run_dir"] = result.run_dir
    out["nodes"] = result.nodes
    out["game_events"] = list(result.game_events)
    out["reason"] = result.summary()

    problems = list(result.reasons)
    if out["missing"]:
        problems.append("未装载：" + "、".join(out["missing"]))
    if out["unexpected"]:
        problems.append("多出未声明的扩展：" + "、".join(out["unexpected"]))
    out["ok"] = result.ok and not out["missing"] and not out["unexpected"]
    out["problems"] = problems
    return out


def format_report(data: dict) -> list[str]:
    """把校验结果转成一组适合直接写进日志的文本行。

    界面和 CLI 共用同一套排版，避免「命令行看到的」和「界面看到的」
    结论措辞不一致。
    """
    lines = [
        f"引擎线 {data.get('line')}：已启用 {len(data.get('enabled') or [])} 个，"
        f"覆盖层 {len(data.get('packs') or [])} 个，"
        f"声明入口 {len(data.get('expected') or [])} 个，"
        f"实际装载 {len(data.get('loaded') or [])} 个"
    ]
    events = data.get("game_events") or []
    if events:
        lines.append("已订阅游戏事件：" + "、".join(events))
    for ext_id in data.get("missing") or []:
        lines.append(f"未装载：{ext_id}")
    for ext_id in data.get("unexpected") or []:
        lines.append(f"未声明却被装载：{ext_id}")
    for item in data.get("failed") or []:
        lines.append(f"装载失败：{item.get('id')} — {item.get('error')}")
    if not data.get("ok"):
        for problem in data.get("problems") or []:
            lines.append(str(problem))
    return lines