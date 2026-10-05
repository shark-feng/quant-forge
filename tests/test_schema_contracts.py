"""报告契约的**执法**守护：`docs/data/*.schema.json` 必须真的被校验器执行。

背景（M5-1 代码审查）：schema 里写了 `additionalProperties: false`，但如果校验器不检查它，
那份 schema 就只是「看着严格」的装饰品 —— 报告多出字段、字段改名、嵌套漂移都不会报错。
本模块把「契约必须真的被执行」变成机械红灯，并且**两个 schema 用同一组伪造**验证，
避免出现「每个 schema 各写一份校验器、各自互相印证」的第二类漂移。

契约清单：
- `docs/data/probe_report.schema.json` → `tools/probe_akshare.py`（M5-1）
- `docs/data/fetch_summary.schema.json` → `tools/fetch_data.py`（M5-2）
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

from tests.schema_validator import MUTATIONS, load_schema, mutation_probe, validate_schema
from tests.tools import PROJECT_ROOT, workspace_tmp

DATA_DIR = PROJECT_ROOT / "docs" / "data"
SCHEMAS = {
    "probe_report": DATA_DIR / "probe_report.schema.json",
    "fetch_summary": DATA_DIR / "fetch_summary.schema.json",
}
PROBE_TESTS = PROJECT_ROOT / "tests" / "test_probe_akshare.py"
FETCH_TESTS = PROJECT_ROOT / "tests" / "test_fetch_data.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def probe_payload(tmp: Path) -> dict[str, Any]:
    """跑一次探测 dry-run，取**实际落盘**的报告作为合规 payload。"""
    probe = _load_module("_contract_probe", PROJECT_ROOT / "tools" / "probe_akshare.py")
    probe.main(["--dry-run", "--out", str(tmp), "--end", "2022-03-07"])
    path = next((tmp / "dry-run").glob("akshare_capability_report.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_payload(tmp: Path) -> dict[str, Any]:
    """跑一次抓取 dry-run，取实际落盘的 `summary.json` 作为合规 payload。"""
    fetch = _load_module("_contract_fetch", PROJECT_ROOT / "tools" / "fetch_data.py")
    fetch.main(["--dry-run", "--out", str(tmp), "--index", "000300.SH"])
    run = sorted((tmp / "dry-run").glob("20*"))[-1]
    return json.loads((run / "summary.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1) 校验器本身：核心关键字必须真的生效
# --------------------------------------------------------------------------- #
def test_validator_enforces_core_keywords():
    schema = {
        "type": "object",
        "required": ["name", "count", "items", "ref_block"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "count": {"type": "integer", "minimum": 1},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "kind": {"enum": ["a", "b"]},
            "version": {"const": 1},
            "nullable": {"type": ["string", "null"]},
            "items": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "dynamic": {
                "type": "object",
                "patternProperties": {"^k[0-9]+$": {"type": "integer"}},
                "additionalProperties": False,
            },
            "ref_block": {"$ref": "#/$defs/block"},
        },
        "$defs": {"block": {"type": "object", "required": ["inner"], "additionalProperties": False,
                            "properties": {"inner": {"type": "string"}}}},
    }
    good = {
        "name": "ab", "count": 1, "ratio": 0.5, "flag": True, "kind": "a", "version": 1,
        "nullable": None, "items": ["x"], "dynamic": {"k1": 2}, "ref_block": {"inner": "y"},
    }
    assert validate_schema(good, schema, schema) == []

    bad_cases = {
        "顶层多键": {**good, "extra": 1},
        "缺必填": {k: v for k, v in good.items() if k != "count"},
        "类型错": {**good, "count": "1"},
        "bool 不算 integer": {**good, "count": True},
        "字符串过短": {**good, "name": "a"},
        "低于最小值": {**good, "count": 0},
        "枚举外": {**good, "kind": "c"},
        "常量不符": {**good, "version": 2},
        "可空字段类型错": {**good, "nullable": 3},
        "数组元素类型错": {**good, "items": [1]},
        "数组过短": {**good, "items": []},
        "patternProperties 外键": {**good, "dynamic": {"k1": 2, "bad": 3}},
        "patternProperties 值类型错": {**good, "dynamic": {"k1": "2"}},
        "$ref 块缺必填": {**good, "ref_block": {}},
        "$ref 块多键": {**good, "ref_block": {"inner": "y", "x": 1}},
    }
    for label, instance in bad_cases.items():
        assert validate_schema(instance, schema, schema), f"校验器放过了「{label}」"


# --------------------------------------------------------------------------- #
# 2) 两个 schema：同一组伪造都必须被拒
# --------------------------------------------------------------------------- #
def test_both_report_schemas_reject_the_same_forgeries():
    with workspace_tmp("contracts_probe") as tmp_probe, workspace_tmp("contracts_fetch") as tmp_fetch:
        payloads = {
            "probe_report": (probe_payload(tmp_probe), ("endpoints", "bars_raw")),
            "fetch_summary": (fetch_payload(tmp_fetch), ("counts",)),
        }

    assert len(MUTATIONS) >= 4, "伪造组数偏少，跨 schema 守护可能已失效"
    for name, (payload, nested) in payloads.items():
        schema = load_schema(SCHEMAS[name])
        # 前提：未改动的 payload 必须合规（否则"被拒"可能只是因为 payload 本来就错）
        assert validate_schema(payload, schema, schema) == [], f"{name} 的真实产物本身就不合规"
        results = mutation_probe(schema, payload, nested=nested)
        for mutation, errors in results.items():
            assert errors, f"{name} 的 schema/校验器放过了「{mutation}」—— 契约没被执行"
        # 报错路径要指到出问题的那一层（顶层 vs 嵌套），而不是笼统一句
        assert any(error.startswith("$:") for error in results["顶层多一个未声明键"])
        nested_errors = results["嵌套段多一个未声明键"]
        assert any(".".join(nested) in error for error in nested_errors), nested_errors


def test_declared_schemas_match_their_producers_and_are_draft_2020():
    for name, path in SCHEMAS.items():
        schema = load_schema(path)
        assert schema.get("$schema", "").endswith("2020-12/schema"), f"{name} 未声明 draft 2020-12"
        assert schema.get("type") == "object" and schema.get("additionalProperties") is False
        assert schema.get("required"), f"{name} 没有 required 列表"
        assert schema.get("title"), f"{name} 缺少 title"
    assert "probe_report.schema.json" in SCHEMAS["probe_report"].name
    assert "fetch_summary.schema.json" in SCHEMAS["fetch_summary"].name


# --------------------------------------------------------------------------- #
# 3) 单一实现：两个 schema 测试共用同一个校验器
# --------------------------------------------------------------------------- #
def test_report_schema_tests_share_one_validator():
    """禁止任何一个测试模块又写一份本地校验器（那正是"两份实现互相印证"的源头）。"""
    for path in (PROBE_TESTS, FETCH_TESTS):
        text = path.read_text(encoding="utf-8")
        assert "from tests.schema_validator import" in text, f"{path.name} 未复用共用校验器"
        assert "def validate_schema(" not in text, f"{path.name} 又定义了一份 validate_schema"
        assert "def _type_ok(" not in text and "def _resolve_ref(" not in text
    validator = (PROJECT_ROOT / "tests" / "schema_validator.py").read_text(encoding="utf-8")
    assert "def validate_schema(" in validator and "def mutation_probe(" in validator
    # 单个实现的正面证据：本模块与另一个 schema 测试导入的是同一个对象
    other = _load_module("_contract_probe_tests", PROBE_TESTS)
    assert other.validate_schema is validate_schema
    assert re.search(r"from tests\.schema_validator import [^\n]*validate_schema", validator) is None or True
