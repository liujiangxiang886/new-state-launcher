"""合集包 / 目录注册表 / 档案管理 / 任务队列的单元测试。

四块都是「界面要用的基础设施」，但都不该只有界面能测。
测的时候不碰真实存档：全部在 ``builds/`` 下开临时目录。

    python tests/test_infra.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import catalog, jobs, profiles  # noqa: E402
from app.core.ext.service import detect_bundle  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def make_ext_zip(path: Path, ext_id: str) -> Path:
    manifest = {
        "schema": 1,
        "id": ext_id,
        "name": ext_id,
        "version": "1.0.0",
        "baseline": "jjxf.v1.45.original",
        "files": [],
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("extension.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("payload/placeholder.txt", "x")
    return path


def test_bundle(work: Path) -> None:
    print("=== 合集包检测 ===")
    # 单包：不是合集
    single = make_ext_zip(work / "single.zip", "test.single")
    check("单包不算合集", detect_bundle(single) == [], str(detect_bundle(single)))

    # 合集：内层两个扩展
    inner_a = make_ext_zip(work / "a.zip", "test.a")
    inner_b = make_ext_zip(work / "b.zip", "test.b")
    bundle = work / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.write(inner_a, "mod/a.zip")
        zf.write(inner_b, "mod/b.zip")
        zf.writestr("mod/readme.txt", "说明")
    members = detect_bundle(bundle)
    check("合集检出 2 个扩展", len(members) == 2, str(members))
    check("成员路径保留子目录", all(m.startswith("mod/") for m in members), str(members))

    # 内层有非扩展 zip 时不误判
    other = work / "other.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("data.txt", "not an extension")
    bundle2 = work / "bundle2.zip"
    with zipfile.ZipFile(bundle2, "w") as zf:
        zf.write(inner_a, "a.zip")
        zf.write(other, "noise.zip")
    check("忽略内层非扩展 zip", len(detect_bundle(bundle2)) == 1, str(detect_bundle(bundle2)))

    # 坏 zip 不抛异常
    broken = work / "broken.zip"
    broken.write_bytes(b"not a zip at all")
    check("坏 zip 返回空而不是抛错", detect_bundle(broken) == [])

    # 不存在的文件
    check("不存在的文件返回空", detect_bundle(work / "nope.zip") == [])


def test_catalog() -> None:
    print("\n=== 目录注册表 ===")
    rows = catalog.entries()
    kinds = {r.kind for r in rows}
    check("至少包含基线", "baseline" in kinds, str(kinds))
    check("每条都有 id 与 line", all(r.id and r.line for r in rows))
    check("baseline 条目数取自索引", all(r.entries_count > 0 for r in rows if r.kind == "baseline"))

    only_main = catalog.entries(line="4.6")
    check("按引擎线过滤", all(r.line == "4.6" for r in only_main), str({r.line for r in only_main}))

    summary = catalog.summary()
    check("汇总含 total", "total" in summary and summary["total"] == len(rows))
    check("汇总含 by_kind", isinstance(summary.get("by_kind"), dict))

    record = catalog.record_run("test.entry", "4.6", True, 12.3, note="unit")
    check("运行记录已写入", record["id"] == "test.entry")
    recent = catalog.recent_runs(5)
    check("能读回运行记录", recent and recent[0]["id"] == "test.entry", str(recent[:1]))
    catalog.clear_runs()
    check("清空后为空", catalog.recent_runs(5) == [])


def test_profiles(work: Path) -> None:
    print("\n=== 档案管理 ===")
    # 名字校验
    for bad in ("", "con", "a/b", "x" * 40, "  "):
        try:
            profiles.validate_name(bad)
            check(f"拒绝非法名 {bad!r}", False, "没有拒绝")
        except Exception:  # noqa: BLE001
            check(f"拒绝非法名 {bad!r}", True)

    # 用唯一名字，避免上一次异常退出留下的残留档案干扰
    name = f"单元测试档案-{int(time.time() * 1000) % 100000}"
    for stale in Path(ROOT / "profiles").glob("单元测试档案*"):
        shutil.rmtree(stale, ignore_errors=True)
    root = profiles.ensure(name)
    check("创建档案目录", root.is_dir() and (root / "appdata").is_dir())
    check("写了 manifest", (root / "manifest.json").is_file())

    appdata = root / "appdata" / "Godot"
    appdata.mkdir(parents=True, exist_ok=True)
    (appdata / "save.json").write_text('{"a":1}', encoding="utf-8")

    env = profiles.env_for(name)
    check("env_for 重定向 APPDATA", env.get("APPDATA", "").endswith("appdata"), str(env.get("APPDATA")))

    snap = profiles.snapshot(name)
    check("快照含备份路径", bool(snap["backup"]), str(snap.get("backup")))
    check("快照含指纹", bool(snap["files"]), "指纹为空")

    (appdata / "save.json").write_text('{"a":2}', encoding="utf-8")
    (appdata / "new.json").write_text('{"b":1}', encoding="utf-8")
    done = profiles.finish(snap)
    delta = done["delta"]
    check("识别出变更", delta["counts"]["changed"] == 1, str(delta["counts"]))
    check("识别出新增", delta["counts"]["added"] == 1, str(delta["counts"]))
    check("摘要文字非空", "存档变化" in done["summary"], done["summary"])

    info = profiles.info(name)
    check("info 含文件数", info["file_count"] >= 2, str(info["file_count"]))
    check("info 含备份数", info["history_count"] >= 1, str(info["history_count"]))

    # 改名
    renamed = profiles.rename(name, name + "2")
    check("改名成功", renamed.is_dir() and not (work / name).exists())
    # 还原
    stamps = [h["path"] for h in profiles.history_of(name + "2")]
    check("改名后历史仍在", bool(stamps), str(stamps[:1]))
    if stamps:
        stamp = Path(stamps[0]).name
        profiles.restore_backup(name + "2", stamp)
        check("还原不抛错", True)

    shutil.rmtree(renamed, ignore_errors=True)


def test_jobs() -> None:
    print("\n=== 任务队列 ===")
    q = jobs.JobQueue()
    changes: list[str] = []
    q.on_change = lambda job: changes.append(f"{job.title}:{job.state.value}")
    q.start()

    def slow(value: int, report=None, **_):
        for i in range(3):
            time.sleep(0.03)
            report((i + 1) * 30, f"step{i + 1}")
        return value * 2

    a = q.submit("A", slow, 5)
    b = q.submit("B", slow, 7)
    c = q.submit("C", lambda **_: 1 / 0)
    deadline = time.time() + 30
    while q.busy() and time.time() < deadline:
        time.sleep(0.02)
    q.stop()

    check("三个任务都跑完", all(j.state.value in ("done", "failed") for j in q.jobs()), str([j.state.value for j in q.jobs()]))
    check("A 结果正确", a.result == 10, str(a.result))
    check("B 结果正确", b.result == 14, str(b.result))
    check("C 标记失败", c.state is jobs.State.FAILED and bool(c.error), str(c.error))
    check("失败不影响后续", a.state is jobs.State.DONE and b.state is jobs.State.DONE)
    check("状态流转完整", any("done" in x for x in changes), str(changes[:4]))

    # 取消排队中的任务
    q2 = jobs.JobQueue()
    q2.start()
    blocker = q2.submit("阻塞", lambda **_: time.sleep(0.4))
    queued = q2.submit("排队", lambda **_: 1)
    ok = q2.cancel(queued.id)
    time.sleep(0.8)
    q2.stop()
    check("能取消排队中的任务", ok and queued.state is jobs.State.CANCELLED, str(queued.state))
    check("取消不影响前一个", blocker.state is jobs.State.DONE, str(blocker.state))

    # 同步执行
    job = jobs.run_inline("内联", lambda: 42)
    check("run_inline 可用", job.result == 42 and job.state is jobs.State.DONE, str(job.result))


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="jjxf-infra-", dir=str(ROOT / "builds")))
    try:
        test_bundle(work)
        test_catalog()
        test_profiles(work)
        test_jobs()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())