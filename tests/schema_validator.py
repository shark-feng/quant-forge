"""轻量 JSON Schema 校验器：**M5 各报告契约共用一份实现**。

为什么需要它，以及为什么必须共用
--------------------------------
`docs/data/*.schema.json` 是"报告格式即契约"的落点，但**声明了不等于被执行**：
如果校验器不检查 `additionalProperties`/`required`/`type`，那份 schema 就只是装饰品 ——
报告多出字段、字段改名、嵌套结构漂移都不会报错。M5-1 的代码审查正是围绕这一点：
「契约必须真的被执行」。因此：

1. 校验器**只实现本仓库用到的子集**（够用就好，不引入 jsonschema 依赖）；
2. **两个 schema 共用本模块**（`probe_report` / `fetch_summary` 及以后新增的）——
   各写一份校验器等于又造了一个「两份实现互相印证」的漂移源；
3. `mutation_probe()` 用**同一组伪造**去砸每一个 schema：任何一份 schema 若放过了
   未声明字段/缺必填/类型错，调用方立刻红灯（`tests/test_schema_contracts.py`）。

支持的关键字：``$ref``（``#/`` 文档内指针）/ ``type``（含类型数组）/ ``const`` / ``enum`` /
``required`` / ``properties`` / ``patternProperties`` / ``additionalProperties``（bool 或 schema）/
``items`` / ``minItems`` / ``minProperties`` / ``minimum`` / ``minLength``。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = ["load_schema", "validate_schema", "mutation_probe", "MUTATIONS"]


def load_schema(path: str | Path) -> dict[str, Any]:
    """读取 schema 文件（UTF-8）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    """解析 ``#/$defs/name`` 形式的 JSON Pointer（本仓库只用这一种）。"""
    assert ref.startswith("#/"), f"只支持文档内引用：{ref}"
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _type_ok(instance: Any, expected: Any) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        if name == "object" and isinstance(instance, dict):
            return True
        if name == "array" and isinstance(instance, list):
            return True
        if name == "string" and isinstance(instance, str):
            return True
        if name == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            return True
        if name == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if name == "boolean" and isinstance(instance, bool):
            return True
        if name == "null" and instance is None:
            return True
    return False


def validate_schema(
    instance: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any] | None = None,
    path: str = "$",
) -> list[str]:
    """校验 ``instance`` 是否符合 ``schema``，返回错误列表（空列表 = 通过）。

    ``root`` 用于解析 ``$ref``：首次调用传 schema 自身即可。
    """
    document = schema if root is None else root
    if "$ref" in schema:
        return validate_schema(instance, _resolve_ref(document, schema["$ref"]), document, path)

    errors: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}，实际 {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} 不在枚举 {schema['enum']}")
    expected = schema.get("type")
    if expected is not None and not _type_ok(instance, expected):
        errors.append(f"{path}: 类型期望 {expected}，实际 {type(instance).__name__}")
        return errors

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必需键 {key!r}")
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate_schema(value, properties[key], document, f"{path}.{key}"))
                continue
            matched = False
            for pattern, sub in patterns.items():
                if re.search(pattern, str(key)):
                    errors.extend(validate_schema(value, sub, document, f"{path}.{key}"))
                    matched = True
            if matched:
                continue
            if additional is False:
                errors.append(f"{path}: 多出未声明的键 {key!r}")
            elif isinstance(additional, dict):
                errors.extend(validate_schema(value, additional, document, f"{path}.{key}"))
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            errors.append(f"{path}: 属性数 {len(instance)} < minProperties")
    elif isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: 元素数 {len(instance)} < minItems {schema['minItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, value in enumerate(instance):
                errors.extend(validate_schema(value, items, document, f"{path}[{index}]"))
    elif isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: 字符串长度 {len(instance)} < minLength")
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
    return errors


#: 「同一组伪造」：每条都是 ``(名称, 施加函数, 期望报错前缀相对于 nested 段)``。
#: 两个 schema 共用同一组 —— 任何一份 schema 放过其中任何一条，跨 schema 用例即红灯。
MUTATIONS: tuple[tuple[str, Callable[[dict[str, Any], Sequence[str]], None]], ...] = (
    (
        "顶层多一个未声明键",
        lambda payload, _nested: payload.update({"__forged__": 1}),
    ),
    (
        "嵌套段多一个未声明键",
        lambda payload, nested: _nested_value(payload, nested).update({"__forged__": 1}),
    ),
    (
        "缺一个必填键",
        lambda payload, nested: _nested_value(payload, nested).pop(_first_required_key(payload, nested), None),
    ),
    (
        "类型写错（把对象写成字符串）",
        lambda payload, nested: payload.__setitem__(
            nested[0], "not-an-object"
        ),
    ),
)


def _nested_value(payload: dict[str, Any], nested: Sequence[str]) -> dict[str, Any]:
    node: Any = payload
    for part in nested:
        node = node[part]
    assert isinstance(node, dict), f"nested 路径必须指向对象：{nested}"
    return node


def _first_required_key(payload: dict[str, Any], nested: Sequence[str]) -> str:
    """从 payload 里挑一个"删了必然违约"的键（非空对象的第一键即可）。"""
    node = _nested_value(payload, nested)
    assert node, f"nested 段不能为空：{nested}"
    return next(iter(node))


def mutation_probe(
    schema: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    nested: Sequence[str],
) -> dict[str, list[str]]:
    """对**合规** payload 施加 :data:`MUTATIONS` 的每条伪造，返回 ``{伪造名: 错误列表}``。

    返回值里任何一条为空列表，就说明该 schema（或本校验器）在那一点上没被执行 ——
    调用方必须断言**全部非空**。``nested`` 指定一个嵌套对象段（如
    ``("endpoints", "bars_raw")`` / ``("counts",)``），用于验证嵌套层的执法。
    """
    results: dict[str, list[str]] = {}
    for name, mutate in MUTATIONS:
        forged = copy.deepcopy(dict(payload))
        mutate(forged, nested)
        results[name] = validate_schema(forged, schema, schema)
    return results
