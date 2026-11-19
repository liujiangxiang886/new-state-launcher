"""处置持久化（``config/decisions.json``）。

要点只有一条：**决定必须能失效**。玩家今天选了「兼容成 15」，
明天作者把包升到 2.0、把那个字段改成字符串，旧决定不能再去写 15。
所以每条决定都带内容指纹——扩展 id + 版本 + 清单摘要，任一项变了就认不出来。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.ext import decisions


def manifest(**kwargs) -> dict:
    data = {
        "schema": 3,
        "id": "test.x",
        "name": "t",
        "version": "1.0.0",
        "data_patches": [],
        "text_patches": [],
        "files": [],
        "requires": [],
    }
    data.update(kwargs)
    return data


def meta_of(*items) -> dict[str, dict]:
    return {pid: data for pid, data in items}


def make(line: str, target: str, locator: str, parties, meta, action: str, **kwargs) -> decisions.Decision:
    return decisions.Decision(
        line=line,
        target=target,
        locator=locator,
        parties=tuple(parties),
        action=action,
        fingerprint=decisions.fingerprint(line, target, locator, parties, meta),
        **kwargs,
    )


PAIR = [("a", manifest(id="a")), ("b", manifest(id="b"))]


# ---------------------------------------------------------------- 指纹


def test_fingerprint_is_stable_for_same_content() -> None:
    meta = meta_of(*PAIR)
    one = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], meta)
    two = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["b", "a"], meta)
    assert one == two, "参与方顺序不该影响指纹"
    assert one.startswith("sha256:")


def test_fingerprint_survives_version_bump() -> None:
    """只升版本号、这一处内容没变时，处置必须继续有效。

    2026-11-05 修订：旧口径把 ``id@version`` 算进指纹，导致包一升版本
    （哪怕只改了一行无关内容），该包参与的所有处置全部失配，开发者校验
    把已处置的重叠当成未处置而拒装。实测事故：workbench 0.22.2 -> 0.22.3
    使 13 条记录一起失效。
    """
    meta = meta_of(*PAIR)
    before = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], meta)
    bumped = meta_of(PAIR[0], ("b", manifest(id="b", version="2.0.0")))
    after = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], bumped)
    assert before == after, "只改版本号不该让处置失效"


def test_fingerprint_changes_with_content() -> None:
    """这一处的补丁内容真的改了，处置必须失效。"""
    meta = meta_of(
        ("a", manifest(id="a", data_patches=[
            {"target": "data/e.json", "pointer": "/a/b", "value": 1}])),
        ("b", manifest(id="b", data_patches=[
            {"target": "data/e.json", "pointer": "/a/b", "value": 2}])),
    )
    before = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], meta)
    edited = meta_of(
        ("a", manifest(id="a", data_patches=[
            {"target": "data/e.json", "pointer": "/a/b", "value": 99}])),
        ("b", manifest(id="b", data_patches=[
            {"target": "data/e.json", "pointer": "/a/b", "value": 2}])),
    )
    after = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], edited)
    assert before != after, "这一处的内容变了，旧决定必须失效"


def test_fingerprint_ignores_unrelated_content() -> None:
    """别的文件的补丁增删不该影响这一处的处置。"""
    meta = meta_of(*PAIR)
    before = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], meta)
    bumped = meta_of(
        PAIR[0],
        ("b", manifest(id="b", data_patches=[
            {"target": "data/other.json", "pointer": "/x", "value": 3}])),
    )
    after = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], bumped)
    assert before == after, "无关文件的改动不该让处置失效"


def test_fingerprint_changes_with_location() -> None:
    meta = meta_of(*PAIR)
    one = decisions.fingerprint("4.6", "data/e.json", "/a/b", ["a", "b"], meta)
    two = decisions.fingerprint("4.6", "data/e.json", "/a/c", ["a", "b"], meta)
    assert one != two


# ---------------------------------------------------------------- 存取


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    store = decisions.Store.load(path)
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))
    store.save()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    assert raw["lines"]["4.6"][0]["action"] == "compatible"
    assert raw["lines"]["4.6"][0]["value"] == 15

    again = decisions.Store.load(path)
    hit = again.match("4.6", "data/e.json", "/a/b", ["a", "b"], meta)
    assert hit is not None and hit.value == 15


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    store = decisions.Store.load(tmp_path / "nope.json")
    assert store.items("4.6") == []
    assert store.save() is None, "没改动就不该写盘"


def test_load_broken_file_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    path.write_text("{ 不是 JSON", encoding="utf-8")
    assert decisions.Store.load(path).items("4.6") == []


def test_load_skips_unknown_action(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    path.write_text(
        json.dumps({"schema": 1, "lines": {"4.6": [
            {"target": "t", "locator": "/a", "parties": ["a", "b"], "action": "whatever"}
        ]}}),
        encoding="utf-8",
    )
    assert decisions.Store.load(path).items("4.6") == []


# ---------------------------------------------------------------- 匹配


def test_match_requires_live_fingerprint(tmp_path: Path) -> None:
    store = decisions.Store.load(tmp_path / "d.json")
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))

    hit = store.match("4.6", "data/e.json", "/a/b", ["a", "b"], meta)
    assert hit is not None

    # 只升版本：仍然要能匹配（见 test_fingerprint_survives_version_bump）
    bumped = meta_of(PAIR[0], ("b", manifest(id="b", version="9.9.9")))
    assert store.match("4.6", "data/e.json", "/a/b", ["a", "b"], bumped) is not None

    # 这一处内容变了：必须失配
    edited = meta_of(
        PAIR[0],
        ("b", manifest(id="b", data_patches=[
            {"target": "data/e.json", "pointer": "/a/b", "value": 777}])),
    )
    assert store.match("4.6", "data/e.json", "/a/b", ["a", "b"], edited) is None


def test_match_requires_same_party_set(tmp_path: Path) -> None:
    store = decisions.Store.load(tmp_path / "d.json")
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))
    assert store.match("4.6", "data/e.json", "/a/b", ["a"], meta) is None


def test_match_is_per_line(tmp_path: Path) -> None:
    store = decisions.Store.load(tmp_path / "d.json")
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))
    assert store.match("4.7", "data/e.json", "/a/b", ["a", "b"], meta) is None


# ---------------------------------------------------------------- 覆盖 / 删除


def test_put_replaces_same_location(tmp_path: Path) -> None:
    store = decisions.Store.load(tmp_path / "d.json")
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=16))
    items = store.items("4.6")
    assert len(items) == 1 and items[0].value == 16


def test_drop_removes_location(tmp_path: Path) -> None:
    store = decisions.Store.load(tmp_path / "d.json")
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))
    store.drop("4.6", "data/e.json", "/a/b", ["a", "b"])
    assert store.items("4.6") == []
    assert store.dirty is True


def test_prune_removes_dead_combinations(tmp_path: Path) -> None:
    store = decisions.Store.load(tmp_path / "d.json")
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))
    store.put(make("4.6", "data/f.json", "/x", ["a", "b"], meta, "compatible", value=1))

    live = {("data/e.json", "/a/b", ("a", "b"))}
    removed = store.prune("4.6", live)
    assert removed == 1
    assert [d.target for d in store.items("4.6")] == ["data/e.json"]


# ---------------------------------------------------------------- 序列化


def test_decision_dict_omits_irrelevant_fields() -> None:
    disable = decisions.Decision("4.6", "t", "/a", ("a", "b"), "disable", party="b")
    payload = disable.as_dict()
    assert payload["party"] == "b"
    assert "value" not in payload and "before" not in payload
    assert disable.scope == "package"

    compatible = decisions.Decision("4.6", "t", "/a", ("a", "b"), "compatible", value=3)
    assert compatible.as_dict()["value"] == 3
    assert compatible.scope == "location"


def test_save_only_active_lines(tmp_path: Path) -> None:
    path = tmp_path / "d.json"
    store = decisions.Store.load(path)
    meta = meta_of(*PAIR)
    store.put(make("4.6", "data/e.json", "/a/b", ["a", "b"], meta, "compatible", value=15))
    store.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert list(raw["lines"]) == ["4.6"]
