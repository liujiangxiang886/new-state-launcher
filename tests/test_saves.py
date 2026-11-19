"""存档档案的单元测试：指纹、diff、备份轮转。

跑法：``python tests/test_saves.py``
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import saves  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="jjxf-saves-", dir=str(ROOT / "builds")))
    profile = base / "prof"
    appdata = profile / "appdata" / "Godot" / "app_userdata" / "game"
    appdata.mkdir(parents=True)
    save = appdata / "offline_roles.json"
    save.write_text(json.dumps({"roles": [{"name": "甲", "job": 400}]}, ensure_ascii=False), encoding="utf-8")

    print("=== 指纹 ===")
    fp1 = saves.fingerprint(profile)
    check("扫到一个存档文件", len(fp1) == 1, str(list(fp1)))
    check("记录了 sha256", bool(next(iter(fp1.values()))["sha256"]))

    print("=== 备份 ===")
    b1 = saves.backup(profile)
    check("备份目录已生成", b1 is not None and b1.is_dir(), str(b1))
    copied = list(b1.rglob("offline_roles.json")) if b1 else []
    check("存档被复制", len(copied) == 1, str(copied))
    check("备份元数据存在", (b1 / "_backup.json").exists() if b1 else False)

    print("=== diff：改内容 ===")
    save.write_text(json.dumps({"roles": [{"name": "甲", "job": 100}]}, ensure_ascii=False), encoding="utf-8")
    fp2 = saves.fingerprint(profile)
    delta = saves.diff(fp1, fp2)
    check("识别为变更", delta["counts"]["changed"] == 1, str(delta["counts"]))
    check("摘要文案正确", "变更 1" in saves.summary_line(delta), saves.summary_line(delta))

    print("=== diff：新增 + 删除 ===")
    extra = appdata / "settings.cfg"
    extra.write_text("volume=3\n", encoding="utf-8")
    save.unlink()
    fp3 = saves.fingerprint(profile)
    delta = saves.diff(fp2, fp3)
    check("新增 1", delta["counts"]["added"] == 1, str(delta["counts"]))
    check("删除 1", delta["counts"]["removed"] == 1, str(delta["counts"]))

    print("=== 备份轮转 ===")
    for index in range(12):
        (appdata / "settings.cfg").write_text(f"volume={index}\n", encoding="utf-8")
        saves.backup(profile, keep=saves.HISTORY_KEEP)
    history = saves.history(profile)
    check(f"最多保留 {saves.HISTORY_KEEP} 份", len(history) <= saves.HISTORY_KEEP, f"{len(history)} 份")
    check("新的在前", history[0]["created_at"] >= history[-1]["created_at"] if len(history) > 1 else True)

    print("=== manifest 原子写 ===")
    path = saves.save_manifest(profile, fp3, note="test")
    data = json.loads(path.read_text(encoding="utf-8"))
    check("manifest 可读", data["file_count"] == len(fp3), str(data["file_count"]))
    check("无临时文件残留", not list(profile.glob(".tmp-*")), str(list(profile.glob(".tmp-*"))))

    shutil.rmtree(base, ignore_errors=True)

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
