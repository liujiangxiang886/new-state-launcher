"""轻量 JSON Schema 校验（不引入 jsonschema 依赖）。

为什么自己写：``schemas/*.json`` 要能在**任何环境**校验（包括未来可能没有
pip 的机器），而 top-level 的 jsonschema 包是 300 KB 级的额外依赖。我们只用到
draft-07 的一个子集：type / required / properties / items / enum / const /
pattern / minimum / maximum / minItems / minLength / additionalProperties /
oneOf / anyOf / allOf / if-then-else。够用，且行为可预期。

校验结果是一组 ``(路径, 消息)``，不抛异常——清单里往往同时有多个问题，
一次报全比拼第一个错更有用。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ERRORS: list[tuple[str, str]] = []


def _type_ok(value: Any, expected: str) -> bool:
    table = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
        "null": type(None),
    }
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    target = table.get(expected)
    return isinstance(value, target) if target else True


def _validate(value: Any, schema: dict, path: str, errors: list[tuple[str, str]]) -> None:
    if not isinstance(schema, dict):
        return

    # type（允许数组形式，任一满足即可）
    if "type" in schema:
        expected = schema["type"]
        candidates = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, t) for t in candidates):
            errors.append((path or "$", f"类型应为 {' / '.join(candidates)}，实际 {type(value).__name__}"))
            return

    if "const" in schema and value != schema["const"]:
        errors.append((path or "$", f"应为常量 {schema['const']!r}，实际 {value!r}"))

    if "enum" in schema and value not in schema["enum"]:
        errors.append((path or "$", f"取值 {value!r} 不在 {schema['enum']}"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append((path or "$", f"长度不足 {schema['minLength']}"))
        if "pattern" in schema:
            try:
                if not re.search(str(schema["pattern"]), value):
                    errors.append((path or "$", f"不匹配 {schema['pattern']}"))
            except re.error:
                pass

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append((path or "$", f"小于最小值 {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append((path or "$", f"大于最大值 {schema['maximum']}"))

    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append((path or "$", f"缺少必填字段 {key}"))
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                child = f"{path}.{key}" if path else key
                _validate(value[key], sub, child, errors)
        additional = schema.get("additionalProperties")
        if additional is False:
            for key in value:
                if key not in props:
                    errors.append((f"{path}.{key}" if path else key, "不允许的额外字段"))
        elif isinstance(additional, dict):
            for key, item in value.items():
                if key not in props:
                    _validate(item, additional, f"{path}.{key}" if path else key, errors)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append((path or "$", f"至少需要 {schema['minItems']} 项"))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{index}]", errors)
        elif isinstance(item_schema, list):
            # 元组式 items（draft-07 老写法）
            for index, item in enumerate(value[: len(item_schema)]):
                _validate(item, item_schema[index], f"{path}[{index}]", errors)

    if "oneOf" in schema:
        matched = 0
        for sub in schema["oneOf"]:
            if not _probe(value, sub, path):
                matched += 1
        if matched != 1:
            errors.append((path or "$", f"oneOf 需要恰好命中 1 个，实际 {matched}"))

    # ``allOf`` / ``anyOf`` / ``if-then-else`` 是 schema 4 引入的
    # （「写了补丁或钩子就必须声明 sources」只能用条件必填表达）。
    # 早期实现刻意没做这三样，于是 v4 schema 里的约束不会被任何校验器执行，
    # 等于把规范写在注释里 —— 现在补上，schema 说什么就查什么。
    for sub in schema.get("allOf") or []:
        _validate(value, sub, path, errors)

    if "anyOf" in schema:
        branches = schema["anyOf"]
        if not any(not _probe(value, sub, path) for sub in branches):
            errors.append((path or "$", f"anyOf 至少要命中 1 个（共 {len(branches)} 个分支）"))

    if "if" in schema:
        # ``if`` 只做判定，本身不产生错误。
        condition_met = not _probe(value, schema["if"], path)
        branch = schema.get("then") if condition_met else schema.get("else")
        if isinstance(branch, dict):
            _validate(value, branch, path, errors)


def _probe(value: Any, schema: Any, path: str) -> list[tuple[str, str]]:
    """静默试跑一个子模式：只回错误列表，不写进外层结果。"""
    if not isinstance(schema, dict):
        return []
    found: list[tuple[str, str]] = []
    _validate(value, schema, path, found)
    return found


def validate(instance: Any, schema: dict) -> list[tuple[str, str]]:
    """返回 ``[(字段路径, 问题描述)]``；空列表表示通过。"""
    errors: list[tuple[str, str]] = []
    _validate(instance, schema, "", errors)
    return errors


def load_schema(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_file(instance_path: str | Path, schema_path: str | Path) -> list[tuple[str, str]]:
    instance = json.loads(Path(instance_path).read_text(encoding="utf-8"))
    return validate(instance, load_schema(schema_path))


def format_errors(errors: list[tuple[str, str]], limit: int = 20) -> str:
    if not errors:
        return "校验通过"
    lines = [f"  {path}: {message}" for path, message in errors[:limit]]
    if len(errors) > limit:
        lines.append(f"  …还有 {len(errors) - limit} 条")
    return "\n".join(lines)