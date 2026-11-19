"""端到端实证：自定义 pck 形态能不能真的把游戏跑起来。

用本机那份剧情 pck 当「别人做的资源包」，走 custom_lines 的 pck 路径：
生成 packset → 底座引擎挂载 → 看 mount_report 是否 ok。

不写进 tests/，因为依赖本机那份 2.9GB 的剧情包（不是所有环境都有）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import custom_lines as cl  # noqa: E402

#: 剧情版 pck 是玩家自备资源，不入库；用环境变量指定路径。
#: 未设置时指向一个不存在的占位路径，下面的 ``is_file()`` 判定会走「跳过」分支。
_story_env = os.environ.get("JJXF_STORY_PCK", "").strip()
STORY_PCK = Path(_story_env) if _story_env else Path("__none__")
WORK = ROOT / "builds" / "custom-pck-e2e"


def main() -> int:
    if not STORY_PCK.is_file():
        print(f"跳过：剧情包不在 {STORY_PCK}")
        return 0

    cfg = cfg_mod.load()
    # 用一份临时配置副本，绝不写回玩家配置
    cfg = json.loads(json.dumps(cfg))
    profiles = Path(tempfile.mkdtemp(prefix="custom-pck-"))

    line = cl.add_line(
        cfg,
        "剧情包（自定义路径验证）",
        str(STORY_PCK),
        kind=cl.KIND_PCK,
        engine_line="4.6",
        profile="e2e-pck",
    )
    print(f"登记成功：kind={line.kind} engine={line.engine_line}")
    print(f"  packset 将写到：profiles/{line.profile_name()}/custom/packset.json")

    plan = cl.prepare_launch(cfg, line, profiles)
    print("\n启动命令：")
    for part in plan.command:
        print("   ", part)
    for warning in plan.warnings:
        print("   [warn]", warning)

    assert plan.packset and plan.packset.is_file(), "packset 没生成"
    data = json.loads(plan.packset.read_text(encoding="utf-8"))
    print(f"\npackset: line={data['line']} packs={len(data['packs'])} role={data['packs'][0]['role']}")

    # 真跑一次（headless，60 秒上限）。--smoke 让底座只实例化主场景就退出。
    env = {
        **os.environ,
        "APPDATA": str(profiles / line.profile_name() / "appdata"),
        "LOCALAPPDATA": str(profiles / line.profile_name() / "appdata"),
    }
    (profiles / line.profile_name() / "appdata").mkdir(parents=True, exist_ok=True)
    cmd = list(plan.command)
    # 自定义线不传 --smoke（正常启动）；这里加一个 --headless 并限时，只为验证挂载
    cmd.insert(1, "--headless")

    print("\n实跑（60 秒上限）…")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path(cmd[0]).parent),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        # 超时说明游戏起来了（在跑），不是失败 —— 我们要看的是 mount_report
        out = ((exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")

    report_path = profiles / line.profile_name() / "custom" / "mount_report.json"
    print(f"\nmount_report: {report_path}")
    if not report_path.is_file():
        print("  ✗ 没有生成挂载报告")
        print(out[:1500])
        return 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(f"  ok={report.get('ok')} mounted={len(report.get('mounted') or [])} "
          f"failed={len(report.get('failed') or [])}")
    for item in (report.get("mounted") or []):
        print("   mounted:", item)
    for err in (report.get("errors") or [])[:5]:
        print("   error:", err)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
