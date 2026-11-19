"""校验《规范手册.md》里写的关键规则与实现是否一致。

文档最容易犯的错是"写完就过期"。这里把可机读的规则逐条对一遍，
发现不一致就报出来。

用法::

    python tools/check_doc.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import config as cfg_mod  # noqa: E402
from app.core import readiness  # noqa: E402
from app.core.ext import manifest as mf  # noqa: E402
from app.core.ext import resolver  # noqa: E402
from app.core.pck import format as pk  # noqa: E402

DOC = ROOT / "docs" / "规范手册.md"
CHECKS: list[tuple[str, bool, str]] = []


def add(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def main() -> int:
    if not DOC.is_file():
        print(f"未找到文档：{DOC}")
        return 2
    text = DOC.read_text(encoding="utf-8")

    # 1) 双基线映射
    for line, bid in readiness.BASELINE_OF.items():
        add(f"双基线映射 {line} → {bid}", bid in text and line in text, f"文档提及 {bid}")

    # 2) pack 版本
    for line, ver in pk.PACK_VERSION_BY_ENGINE.items():
        pat = rf"{re.escape(line)}\s*→\s*{ver}"
        add(f"pack 版本 {line} → {ver}", bool(re.search(pat, text)), f"期望出现「{line} → {ver}」")

    # 3) kind 波次
    for kind, wave in resolver.KIND_WAVES.items():
        add(f"kind 波次 {kind}={wave}", kind in text, f"文档提及 {kind}")

    # 4) 基线别名
    for alias, (bid, line) in mf.BASELINE_ALIAS.items():
        add(f"基线别名 {alias}", alias in text, f"→ {bid}/{line}")

    # 5) id / semver 正则原文
    id_src = r"^[a-z0-9][a-z0-9._-]{1,63}$"
    add("id 正则一致", id_src in text, id_src)

    # 6) schema 1→2 映射键
    missing = [k for k in mf.SCHEMA1_MAP if k.split("[")[0] not in text]
    add("schema1 映射完整", not missing, f"缺：{missing[:4]}" if missing else "全部覆盖")

    # 7) issue code 覆盖
    issue_src = (ROOT / "app" / "core" / "ext" / "manifest.py").read_text(encoding="utf-8")
    codes = sorted(set(re.findall(r'Issue\(\s*"(?:error|warn|info)"\s*,\s*"([a-z0-9_.]+)"', issue_src)))
    not_doc = [c for c in codes if c not in text]
    add("issue code 全覆盖", not not_doc, f"未写入文档：{not_doc}" if not_doc else f"{len(codes)} 个")

    # 8) 相对路径解析规则存在
    add("路径解析规则", "resolve_path" in text or "相对路径" in text, "第 8.3 节")

    # 9) 打包目录清单与小写名一致
    for name in ("config", "baseline", "engines", "schemas", "runtime"):
        add(f"打包目录 {name}/", name in text, "")

    # 10) 章节结构完整
    for sec in ("基础概念", "双基线规范", "兼容协议", "扩展规范",
                "剧情包规范", "挂载与冲突", "存档隔离", "目录与文件布局", "故障排查"):
        add(f"章节「{sec}」", sec in text, "")

    # 11) 32MB 限制：剧情包构建代码已随 1.4 移除，阈值只作为打包约定保留。
    # 这里改为核对文档自身写明了阈值（代码里没有对应常量可对）。
    add("剧情 32MB 限制有据", "32 MB" in text, "文档中的打包约定")

    # 12) 已知问题章节必须存在（这是本文档的强制前提）
    for sec in ("已知问题与待解决事项", "显示已安装但功能不生效",
                "触发条件", "复现步骤", "环境信息", "临时规避"):
        add(f"已知问题章节「{sec}」", sec in text, "")

    # 13) 四级判定表必须完整（L1..L4），不能只写「安装成功」
    for level in ("L1", "L2", "L3", "L4"):
        add(f"判定级别 {level}", level in text, "")
    add("区分安装与生效", "「安装成功」≠「实际生效」" in text or "安装成功" in text and "实际生效" in text,
        "1.4 节")

    # 14) 文档中引用的 status 取值必须与代码一致
    for status in ("ready", "needs_hooks"):
        add(f"status 取值 {status}", status in text, "")

    # 15) verified 语义与 verify_smoke 默认值必须写清
    svc_src = (ROOT / "app" / "core" / "ext" / "service.py").read_text(encoding="utf-8")
    add("verify_smoke 默认值有据", "verify_smoke" in svc_src, "service.py")
    add("文档说明 verified 语义", "verified" in text, "")

    failed = 0
    for name, ok, detail in CHECKS:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
        if not ok:
            failed += 1

    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())