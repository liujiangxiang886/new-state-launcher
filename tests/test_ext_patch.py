"""补丁引擎与解析器的单元测试。

覆盖两条新链路：

- ``ext/patcher.py``：字面量/正则替换、JSON Pointer 操作、命中数校验、
  ``on_test_fail`` 的 abort/warn 行为；
- ``ext/resolver.py``：依赖排序、成环检测、claims 重叠、replace 撞车、
  lock.json 指纹。

跑法：``python tests/test_ext_patch.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ext import patcher as P  # noqa: E402
from app.core.ext.resolver import build_lock, claims_overlap, resolve  # noqa: E402

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


def manifest(**kwargs) -> dict:
    base = {
        "kind": "extension",
        "priority": 50,
        "requires": [],
        "conflicts": [],
        "load_after": [],
        "load_before": [],
        "claims": [],
        "method_hooks": [],
        "text_patches": [],
    }
    base.update(kwargs)
    return base


def main() -> int:
    print("=== 文本补丁 ===")
    text = 'limit = 3\nname = "x"\n'
    out, count, issues = P.apply_text(text, {"find": "limit = 3", "replace": "limit = 10"})
    check("字面量替换生效", out.startswith("limit = 10") and count == 1, repr(out))
    check("无问题", not issues, str(issues))

    _out, count, issues = P.apply_text(text, {"find": "nope", "replace": "x", "expect_count": 1})
    check("命中数不符被拦", count == 0 and bool(issues), str(issues))

    _out, count, issues = P.apply_text(
        "hp=100\nhp=120\n", {"find": r"hp=(\d+)", "replace": r"hp=999", "mode": "regex", "expect_count": 2}
    )
    check("正则替换并校验次数", count == 2 and not issues, f"{count} {issues}")

    _out, _count, issues = P.apply_text("a\n", {"find": "(", "mode": "regex"})
    check("非法正则报错", bool(issues), str(issues))

    print("=== JSON 补丁 ===")
    doc = {"a": {"b": [1, 2, 3]}, "keep": True}
    out_doc, applied, _ = P.apply_json(
        doc,
        [
            {"op": "replace", "path": "/a/b/1", "value": 20},
            {"op": "add", "path": "/a/b/-", "value": 4},
            {"op": "merge", "path": "/a", "value": {"c": 5}},
            {"op": "test", "path": "/keep", "value": True},
            {"op": "remove", "path": "/a/b/0"},
        ],
    )
    check("五个操作全部应用", applied == 5, str(applied))
    check("结果正确", out_doc == {"a": {"b": [20, 3, 4], "c": 5}, "keep": True}, str(out_doc))
    check("原文档未被修改", doc == {"a": {"b": [1, 2, 3]}, "keep": True}, str(doc))

    try:
        P.apply_json({"v": 1}, [{"op": "test", "path": "/v", "value": 2}])
        check("test 失败会抛错", False, "没有抛 PatchError")
    except P.PatchError:
        check("test 失败会抛错", True)

    try:
        P.apply_json({}, [{"op": "replace", "path": "/missing/deep", "value": 1}])
        check("路径不存在会抛错", False, "没有抛 PatchError")
    except P.PatchError:
        check("路径不存在会抛错", True)

    merged = P.deep_merge({"a": 1, "b": {"c": 2}}, {"b": {"d": 3}, "e": None})
    check("merge 支持 null 删键", merged == {"a": 1, "b": {"c": 2, "d": 3}}, str(merged))

    print("=== patch_bytes 组合 ===")
    raw = json.dumps({"daily_limit": 3, "note": "hello"}).encode()
    new, reports = P.patch_bytes(
        "data/x.json",
        raw,
        [{"find": "hello", "replace": "world", "mode": "literal"}],
        [{"ops": [{"op": "replace", "path": "/daily_limit", "value": 10}]}],
    )
    data = json.loads(new)
    check("数据补丁先生效", data["daily_limit"] == 10, str(data))
    check("文本补丁在其后生效", data["note"] == "world", str(data))
    check("两条报告都产出", len(reports) == 2, str([r.kind for r in reports]))

    print("=== on_test_fail=warn 不中断 ===")
    new, reports = P.patch_bytes(
        "data/x.json", raw, [], [{"ops": [{"op": "test", "path": "/daily_limit", "value": 99}], "on_test_fail": "warn"}]
    )
    check("warn 模式下不抛错", new == raw, "内容被改了")
    check("问题被记录", bool(reports and reports[0].issues), str(reports[0].as_dict() if reports else []))

    print("=== 解析器：排序 ===")
    r = resolve([("a", manifest()), ("b", manifest(load_after=["a"]))])
    check("load_after 生效", r.order == ["a", "b"], str(r.order))
    r = resolve([("low", manifest(priority=90)), ("high", manifest(priority=10))])
    check("priority 小的先加载", r.order == ["high", "low"], str(r.order))
    r = resolve([("story", manifest(kind="story")), ("ext", manifest())])
    check("kind 分波：扩展在剧情之前", r.order == ["ext", "story"], str(r.order))

    print("=== 解析器：冲突 ===")
    r = resolve([("a", manifest(load_after=["b"])), ("b", manifest(load_after=["a"]))])
    check("成环被拦", not r.ok and any(p.code == "order.cycle" for p in r.problems), str(r.order))
    r = resolve([("a", manifest(conflicts=["b"])), ("b", manifest())])
    check("声明互斥被拦", not r.ok, "未拦住")
    r = resolve([("a", manifest(requires=[{"id": "ghost"}])), ("b", manifest())])
    check("依赖缺失被拦", not r.ok, "未拦住")
    r = resolve(
        [
            ("a", manifest(claims=["scripts/game.gd"])),
            ("b", manifest(claims=["scripts/game.gd#character_attributes"])),
        ]
    )
    check("claims 重叠被拦", not r.ok, "未拦住")
    check("同文件不同方法不算重叠", not claims_overlap("a.gd#m1", "a.gd#m2"))
    check("文件与其 remap 不算重叠", not claims_overlap("a.gd", "a.gd.remap"))
    r = resolve(
        [
            ("a", manifest(method_hooks=[{"target": "s.gd", "method": "m", "phase": "replace"}])),
            ("b", manifest(method_hooks=[{"target": "res://s.gd", "method": "m", "phase": "replace"}])),
        ]
    )
    check("同方法 replace 撞车被拦", not r.ok, "未拦住")
    r = resolve(
        [
            ("a", manifest(text_patches=[{"target": "d.json", "find": "abc", "mode": "literal"}])),
            ("b", manifest(text_patches=[{"target": "d.json", "find": "abc", "mode": "literal"}])),
        ]
    )
    check("同一条文本补丁被拦", not r.ok, "未拦住")

    print("=== lock.json ===")
    r = resolve([("a", manifest()), ("b", manifest(load_after=["a"]))])
    lock = build_lock(
        r,
        {
            "a": {"version": "1.0.0", "sha256": "aaa", "overlay": "a.pck", "entries": 3},
            "b": {"version": "2.0.0", "sha256": "bbb", "overlay": "b.pck", "entries": 5},
        },
        line="4.6",
        baseline={"id": "jjxf.v1.45", "sha256": "base"},
    )
    check("顺序记录正确", [e["id"] for e in lock["resolution_order"]] == ["a", "b"])
    check("有指纹", bool(lock.get("fingerprint")), "缺 fingerprint")
    check("记录了基线指纹", lock["baseline"]["sha256"] == "base")

    r2 = resolve([("a", manifest()), ("b", manifest(load_after=["a"]))])
    lock2 = build_lock(r2, {"a": {"sha256": "aaa"}, "b": {"sha256": "bbb"}}, line="4.6")
    check("相同组合指纹一致", lock["fingerprint"] == lock2["fingerprint"], "指纹不稳定")

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())