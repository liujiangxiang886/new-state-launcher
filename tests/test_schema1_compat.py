"""P3 验收：schema 1 样例包无损升格，且字段映射与 schemas/schema1_map.json 一致。

计划 §6 的 P3 判据是「3 个 schema1 样例包无损导入并生效」。这里把「无损导入」
拆成可验证的断言：

1. 升格后 schema 变成 2，且原始数据完整保留在 ``raw`` 里（一个字都不丢）；
2. 每个字段映射都按 ``schemas/schema1_map.json`` 落地；
3. 两份映射表（JSON 与代码常量）不会悄悄漂移；
4. 冲突用例全部被解析器拦下。

第 3 条是重点：``schemas/schema1_map.json`` 是给人看的文档，
``manifest.SCHEMA1_MAP`` 是代码。两份不同步时，文档会骗人。

    python tests/test_schema1_compat.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import schemacheck  # noqa: E402
from app.core.ext import manifest as mf  # noqa: E402
from app.core.ext import resolver  # noqa: E402

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


#: 三个 schema1 样例：分别覆盖「纯文件」「带补丁」「带钩子」三种形态
SAMPLES: dict[str, dict] = {
    "files-only": {
        "schema": 1,
        "id": "sample.files",
        "name": "纯文件覆盖",
        "version": "1.0.0",
        "kind": "extension",
        "baseline": "jjxf.v1.45.original",
        "entry": "res://mods/sample/main.gd",
        "files": [
            {"source": "payload/main.gd", "target": "mods/sample/main.gd"},
            {
                "source": "payload/data.json",
                "target": "data/sample.json",
                "expect_sha256": "a" * 64,
            },
        ],
        "script_members": [{"script": "scripts/hud.gd", "member": "x"}],
        "project_settings": {"display/window/size/width": 1280},
        "input_actions": {"sample_action": {"deadzone": 0.5}},
    },
    "patches-only": {
        "schema": 1,
        "id": "sample.patches",
        "name": "补丁包",
        "version": "2.1.0",
        "kind": "extension",
        "baseline": "jjxf.v1.45",
        "data_patches": [
            {
                "target": "data/start_page.json",
                "operations": [
                    {"op": "set", "path": "/fps", "value": 30},
                    {"op": "replace", "path": "/limitations", "value": []},
                ],
                "on_test_fail": "warn",
            }
        ],
        "text_patches": [
            {"target": "scripts/start_page.gd", "find": "func _ready", "replace": "func _ready"}
        ],
    },
    "hooks-only": {
        "schema": 1,
        "id": "sample.hooks",
        "name": "钩子包",
        "version": "0.9.0",
        "kind": "extension",
        "baseline": "jjxf.v1.45.original",
        "requires": {"sample.patches": ">=2.0.0"},
        "method_hooks": [
            {"target": "scripts/game.gd", "method": "character_attributes", "phase": "after"}
        ],
        "conflicts": ["sample.other"],
    },
}


def main() -> int:
    # ---------------------------------------------------------------- 映射表同步
    print("=== 映射表同步 ===")
    map_file = ROOT / "schemas" / "schema1_map.json"
    check("schema1_map.json 存在", map_file.is_file(), str(map_file))
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    doc_keys = set(doc.get("fields") or {})
    code_keys = set(mf.SCHEMA1_MAP)
    missing_in_code = doc_keys - code_keys
    missing_in_doc = code_keys - doc_keys
    check(
        "JSON 与代码的映射项一致",
        not missing_in_code and not missing_in_doc,
        f"JSON 多出 {sorted(missing_in_code)}；代码多出 {sorted(missing_in_doc)}",
    )

    alias_doc = doc.get("baseline_alias") or {}
    alias_code = mf.BASELINE_ALIAS
    check(
        "基线别名表一致",
        set(alias_doc) == set(alias_code),
        f"JSON {sorted(alias_doc)} vs 代码 {sorted(alias_code)}",
    )
    same_values = all(
        (alias_doc[k].get("id"), alias_doc[k].get("engine_line")) == alias_code[k]
        for k in alias_doc
        if k in alias_code
    )
    check("基线别名映射值一致", same_values, "同名别名的 id/engine_line 不一致")

    # ---------------------------------------------------------------- 三个样例
    schema = schemacheck.load_schema(ROOT / "schemas" / "extension.v2.schema.json")
    for name, raw in SAMPLES.items():
        print(f"\n=== 样例 {name} ===")
        m = mf.normalize(raw, entries=[], baseline_paths=None)
        check("升格无 error", m.ok, str([i.message for i in m.issues if i.level == "error"]))
        check("schema 变成 2", m.data["schema"] == 2, str(m.data["schema"]))
        check("记录了 x_origin", "x_origin" in m.data)
        check("原始数据完整保留", m.raw is raw)

        errs = schemacheck.validate(m.data, schema)
        check("通过 v2 schema 校验", not errs, schemacheck.format_errors(errs, 5))

        # 逐条核对映射
        if raw.get("baseline"):
            expected = alias_doc[raw["baseline"]]
            got = m.data["baseline"]
            check(
                "baseline 别名升格正确",
                got["id"] == expected["id"] and got["engine"]["line"] == expected["engine_line"],
                f"{got} != {expected}",
            )
        if raw.get("entry"):
            check("entry → scripts.entry", m.data["scripts"].get("entry") == raw["entry"])
        for index, rec in enumerate(raw.get("files") or []):
            if rec.get("expect_sha256"):
                got = m.data["files"][index].get("expect", {}).get("sha256")
                check("expect_sha256 → expect.sha256", got == rec["expect_sha256"], str(got))
        for index, patch in enumerate(raw.get("data_patches") or []):
            ops = m.data["data_patches"][index]["ops"]
            kinds = [op["op"] for op in ops]
            check("data_patches.operations → ops", bool(ops), "ops 为空")
            check("set → replace", "set" not in kinds, str(kinds))
        for index, patch in enumerate(raw.get("text_patches") or []):
            got = m.data["text_patches"][index]
            check("text_patches 补 mode=literal", got["mode"] == "literal", got["mode"])
            check("text_patches 补 expect_count=1", got["expect_count"] == 1, str(got["expect_count"]))
        if raw.get("script_members"):
            check("script_members → scripts.members", bool(m.data["scripts"].get("members")))
        if raw.get("project_settings"):
            check("project_settings → settings.project", bool(m.data["settings"].get("project")))
        if raw.get("input_actions"):
            check("input_actions → inputs.actions", bool(m.data["inputs"].get("actions")))
        if isinstance(raw.get("requires"), dict):
            req = m.data["requires"]
            check("requires 字典 → 列表", isinstance(req, list) and bool(req), str(req))
            check(
                "requires 保留了版本范围",
                req and req[0].get("version") == list(raw["requires"].values())[0],
                str(req),
            )
        for index, hook in enumerate(raw.get("method_hooks") or []):
            got = m.data["method_hooks"][index]
            check("method_hooks 补 priority=50", got["priority"] == 50, str(got["priority"]))
            if not hook.get("phase"):
                check("method_hooks 补 phase=after", got["phase"] == "after", got["phase"])

    # ---------------------------------------------------------------- 冲突用例
    print("\n=== 冲突拦截 ===")
    cases = [
        (
            "成环",
            [("a", {"kind": "extension", "load_after": ["b"]}), ("b", {"kind": "extension", "load_after": ["a"]})],
        ),
        (
            "声明互斥",
            [("a", {"kind": "extension", "conflicts": ["b"]}), ("b", {"kind": "extension"})],
        ),
        (
            "依赖缺失",
            [("a", {"kind": "extension", "requires": [{"id": "ghost"}]})],
        ),
        (
            "claims 重叠",
            [
                ("a", {"kind": "extension", "claims": ["scripts/game.gd"]}),
                ("b", {"kind": "extension", "claims": ["scripts/game.gd#m"]}),
            ],
        ),
        (
            "同方法 replace",
            [
                ("a", {"kind": "extension", "method_hooks": [{"target": "s.gd", "method": "m", "phase": "replace"}]}),
                ("b", {"kind": "extension", "method_hooks": [{"target": "s.gd", "method": "m", "phase": "replace"}]}),
            ],
        ),
        (
            "同一条文本补丁",
            [
                ("a", {"kind": "extension", "text_patches": [{"target": "d.json", "find": "x", "mode": "literal"}]}),
                ("b", {"kind": "extension", "text_patches": [{"target": "d.json", "find": "x", "mode": "literal"}]}),
            ],
        ),
    ]
    for label, items in cases:
        resolution = resolver.resolve(items)
        check(f"{label} 被拦下", not resolution.ok, "未拦下")

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())