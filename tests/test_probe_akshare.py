"""M5-1：AKShare 端点探测工具（`tools/probe_akshare.py`）的**离线**用例。

设计依据：M5-1 确认稿（接口 / JSON schema / 测试清单）+ Q1~Q5 与补充 1~8 的修正。

覆盖的**核心纪律**（每条都对应一处「不报错的错」）：

- 单位判定必须敢报警：实测成交量是「股」时要**明确要求**把 `_DEFAULT_VOLUME_TO_SHARES`
  改成 1.0（把「手」当「股」会让成交额/参与率/冲击成本整体错 100 倍且不报错）；
- 公告日缺失必须说「M6 的 PIT 财务因子无法做」，不能用报告期顶替；
- `mapper_check` 是**只读**调用（不改入参、不发请求、不写盘）—— 声明与用例必须同步；
- 探测构造的调用参数必须与 provider **完全一致**（单一实现：`client_kwargs_for`）；
- dry-run 产物必须落在 `dry-run/` 子目录，且报告页首写明「手工构造、非真实抓取」。

**不联网**：dry-run 走 `FakeAKShareClient` + `tests/fixtures/akshare/`；
「akshare 未安装」路径注入失败的 importer，也不会真的 import。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from tests.compat import raises
from tests.fake_akshare import FIXTURES, FakeAKShareClient
from tests.tools import PROJECT_ROOT, workspace_tmp

from aqs.config.schema import DataConfig
from aqs.data.akshare_provider import ENDPOINTS, AKShareProvider
from aqs.data.cache import DataCache
from aqs.data.ratelimit import RateLimiter

TOOL_PATH = PROJECT_ROOT / "tools" / "probe_akshare.py"
SCHEMA_PATH = PROJECT_ROOT / "docs" / "data" / "probe_report.schema.json"
REPORT_STEM = "akshare_capability_report"

#: dry-run 的探测窗口必须落在 fixture 覆盖的日期里（否则「空返回」会掩盖真实结论）
PROBE_END = "2022-03-07"
PROBE_DAYS = 6  # end - 6 天 = 2022-03-01，与 provider 用例的 START 对齐

BARS_RAW_FIXTURE = "stock_zh_a_hist_600000_raw.csv"
FUNDAMENTALS_FIXTURE = "stock_yjbb_em_20211231.csv"


# --------------------------------------------------------------------------- #
# 工具加载 / 报告读取 / 极简 JSON Schema 校验器
# --------------------------------------------------------------------------- #
def load_probe_module() -> Any:
    """按路径加载 `tools/probe_akshare.py`（工具不在包内，只能按文件加载）。"""
    assert TOOL_PATH.exists(), f"缺少 {TOOL_PATH}"
    spec = importlib.util.spec_from_file_location("_probe_akshare_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_probe_akshare_under_test"] = module
    spec.loader.exec_module(module)
    return module


def run_dry(
    tmp: Path,
    probe: Any,
    *extra: str,
    client: Any | None = None,
    end: str = PROBE_END,
    days: int = PROBE_DAYS,
) -> tuple[int, Path]:
    """跑一次 dry-run，返回 ``(退出码, 报告目录)``。"""
    args = ["--dry-run", "--out", str(tmp), "--end", end, "--days", str(days), *extra]
    code = probe.main(args, client=client)
    return code, tmp / "dry-run"


def read_report(out_dir: Path) -> tuple[dict[str, Any], str]:
    """从**实际落盘的文件**读回报告（不读内存对象：报告数字必须可追溯到产物）。"""
    json_path = out_dir / f"{REPORT_STEM}.json"
    md_path = out_dir / f"{REPORT_STEM}.md"
    assert json_path.exists(), f"缺少 JSON 报告：{json_path}"
    assert md_path.exists(), f"缺少 Markdown 报告：{md_path}"
    return json.loads(json_path.read_text(encoding="utf-8")), md_path.read_text(encoding="utf-8")


def fixture_root_copy(tmp: Path) -> Path:
    """复制一份 fixture 目录，供「反例」用例改造（绝不改仓库里的原件）。"""
    root = tmp / "fixtures"
    shutil.copytree(FIXTURES, root)
    return root


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
    instance: Any, schema: Mapping[str, Any], root: Mapping[str, Any] | None = None, path: str = "$"
) -> list[str]:
    """极简 JSON Schema 校验器（**不引入 jsonschema 依赖**）。

    只实现本仓库 schema 用到的子集：``$ref``（``#/`` 指针）/ ``type``（含类型数组）/
    ``const`` / ``enum`` / ``required`` / ``properties`` / ``patternProperties`` /
    ``additionalProperties``（bool 或 schema）/ ``items`` / ``minItems`` / ``minProperties`` /
    ``minimum`` / ``minLength``。返回错误列表（空列表 = 通过）。
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


def patch_fixture(root: Path, filename: str, mutate: Any) -> None:
    """就地改造一份 fixture CSV（只作用于临时副本）。"""
    path = root / filename
    frame = pd.read_csv(path, dtype={"股票代码": str, "代码": str, "成分券代码": str})
    mutate(frame)
    frame.to_csv(path, index=False, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1~2：dry-run 目录规范 / 报告结构 / schema
# --------------------------------------------------------------------------- #
def test_dry_run_writes_reports_into_dry_run_subdir():
    probe = load_probe_module()
    with workspace_tmp("probe_dry") as tmp:
        code, out_dir = run_dry(tmp, probe)
        assert code == 0, "dry-run 的退出码恒为 0（脚本自身跑通即通过，可当 CI smoke test）"
        assert out_dir.name == "dry-run", "dry-run 必须落在 <out>/dry-run/，不得与真实报告同名混放"
        assert not (tmp / f"{REPORT_STEM}.json").exists(), "dry-run 不得写进真实报告的位置"
        report, markdown = read_report(out_dir)
        assert report["mode"] == "dry-run"
        assert report["akshare_version"] == "dry-run(FakeAKShareClient)"
        # 补充 8：页首必须写明 mode=dry-run + fixture 是手工构造
        assert "dry-run" in markdown.splitlines()[2]
        assert "手工构造" in markdown and "不是真实抓取" in markdown


def test_report_satisfies_declared_schema_and_covers_every_endpoint():
    probe = load_probe_module()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with workspace_tmp("probe_schema") as tmp:
        _, out_dir = run_dry(tmp, probe)
        report, _ = read_report(out_dir)
        raw_text = (out_dir / f"{REPORT_STEM}.json").read_text(encoding="utf-8")

    errors = validate_schema(report, schema, schema)
    assert not errors, "报告不符合 docs/data/probe_report.schema.json：\n" + "\n".join(errors)

    assert set(report["endpoints"]) == set(ENDPOINTS), "探测必须覆盖 ENDPOINTS 的每一个端点"
    assert list(report["endpoints"]) == list(probe.PROBE_ORDER), "端点顺序必须与 PROBE_ORDER 一致"
    assert set(probe.PROBE_ORDER) == set(ENDPOINTS)
    for endpoint_id, record in report["endpoints"].items():
        for key in ("columns", "dtypes", "rows", "elapsed_ms", "unit", "mapper_check", "sample"):
            assert key in record, f"{endpoint_id} 缺少字段 {key}"
    # 补充 4：样例只取前 2 行、所有值字符串化、中文不转义（ensure_ascii=False）
    bars = report["endpoints"]["bars_raw"]["sample"]
    assert len(bars["rows"]) == 2
    assert all(isinstance(value, str) for row in bars["rows"] for value in row)
    assert bars["columns"][0] == "日期"
    assert '"成交量"' in raw_text, "JSON 必须用 ensure_ascii=False 写出中文列名"
    assert "\\u" not in raw_text, "中文列名不应被转义成 \\uXXXX"


# --------------------------------------------------------------------------- #
# 3~4：单位判定（Q3 陷阱）
# --------------------------------------------------------------------------- #
def test_units_classified_as_lots_and_yuan_from_raw_bars():
    probe = load_probe_module()
    with workspace_tmp("probe_unit_ok") as tmp:
        _, out_dir = run_dry(tmp, probe)
        report, _ = read_report(out_dir)
    unit = report["endpoints"]["bars_raw"]["unit"]
    assert unit["volume"] == "手", "fixture 与真实 AKShare 同构：成交量以「手」计"
    assert unit["amount"] == "元"
    assert unit["ratio"] is not None and 40 < unit["ratio"] < 260
    advice = "\n".join(report["recommendations"])
    assert "_DEFAULT_VOLUME_TO_SHARES" in advice and "正确" in advice


def test_units_alarm_when_volume_is_shares_and_unknown_when_column_missing():
    probe = load_probe_module()
    with workspace_tmp("probe_unit_bad") as tmp:
        # 反例 A：成交量单位变成「股」（真实数据源若如此，映射里的 ×100 就是错的）
        shares_root = fixture_root_copy(tmp / "shares")
        patch_fixture(
            shares_root, BARS_RAW_FIXTURE, lambda frame: frame.__setitem__("成交量", frame["成交量"] * 100)
        )
        _, out_dir = run_dry(tmp / "shares", probe, "--fixture-root", str(shares_root))
        report, _ = read_report(out_dir)
        unit = report["endpoints"]["bars_raw"]["unit"]
        assert unit["volume"] == "股" and unit["amount"] == "元"
        advice = "\n".join(report["recommendations"])
        assert "改为 1.0" in advice, "实测为「股」时必须明确要求把换算系数改成 1.0"
        assert "100 倍" in advice, "必须写明不改的后果（错 100 倍且不报错）"

        # 反例 B：没有成交额列 → 判定不了单位，必须报「未知」并禁止上线
        unknown_root = fixture_root_copy(tmp / "unknown")
        patch_fixture(unknown_root, BARS_RAW_FIXTURE, lambda frame: frame.drop(columns=["成交额"], inplace=True))
        _, out_dir2 = run_dry(tmp / "unknown", probe, "--fixture-root", str(unknown_root))
        report2, _ = read_report(out_dir2)
        unit2 = report2["endpoints"]["bars_raw"]["unit"]
        assert unit2["volume"] == "未知" and unit2["ratio"] is None
        assert "禁止上线" in "\n".join(report2["recommendations"])


# --------------------------------------------------------------------------- #
# 5~6：映射器兼容性（单一实现：候选列只在 MAPPERS 里维护）
# --------------------------------------------------------------------------- #
def test_mapper_check_consumes_every_endpoint():
    probe = load_probe_module()
    with workspace_tmp("probe_mapper_ok") as tmp:
        _, out_dir = run_dry(tmp, probe)
        report, _ = read_report(out_dir)
    for endpoint_id, record in report["endpoints"].items():
        check = record["mapper_check"]
        assert check["ok"] is True, f"{endpoint_id} 的映射器无法消费实测返回：{check['error']}"
        assert (check["rows"] or 0) > 0
    fundamentals = report["endpoints"]["fundamentals"]["mapper_check"]
    assert fundamentals["rows"] == 3, "缺公告日的那一行必须被丢弃（不得用报告期顶替）"
    assert any("公告日期" in warning for warning in fundamentals["warnings"])


def test_mapper_check_reports_missing_columns_with_candidates():
    probe = load_probe_module()
    with workspace_tmp("probe_mapper_bad") as tmp:
        root = fixture_root_copy(tmp / "fx")
        patch_fixture(root, BARS_RAW_FIXTURE, lambda frame: frame.drop(columns=["收盘"], inplace=True))
        _, out_dir = run_dry(tmp, probe, "--fixture-root", str(root))
        report, _ = read_report(out_dir)
    check = report["endpoints"]["bars_raw"]["mapper_check"]
    assert check["ok"] is False
    assert "候选" in (check["error"] or ""), "错误信息必须带上候选列清单（供 M5-5 单点修正）"
    assert "收盘" in (check["error"] or "")
    assert any("MAPPERS" in item for item in report["recommendations"])


# --------------------------------------------------------------------------- #
# 7：公告日（PIT 财务因子的前提）
# --------------------------------------------------------------------------- #
def test_announce_date_available_and_missing_cases():
    probe = load_probe_module()
    with workspace_tmp("probe_announce_ok") as tmp:
        _, out_dir = run_dry(tmp, probe)
        report, _ = read_report(out_dir)
    check = report["endpoints"]["fundamentals"]["announce_date_check"]
    assert check["pit_ok"] is True and check["column"] == "最新公告日期"
    assert report["capabilities_observed"]["fundamentals_announce_date"] is True

    with workspace_tmp("probe_announce_bad") as tmp2:
        root = fixture_root_copy(tmp2 / "fx")
        patch_fixture(
            root, FUNDAMENTALS_FIXTURE, lambda frame: frame.drop(columns=["最新公告日期"], inplace=True)
        )
        _, out_dir2 = run_dry(tmp2, probe, "--fixture-root", str(root))
        report2, _ = read_report(out_dir2)
        check2 = report2["endpoints"]["fundamentals"]["announce_date_check"]
        assert check2["present"] is False and check2["pit_ok"] is False
        assert report2["capabilities_observed"]["fundamentals_announce_date"] is False
        advice = "\n".join(report2["recommendations"])
        assert "PIT" in advice and "无法做" in advice, "缺公告日必须明说 M6 的 PIT 因子做不了"


# --------------------------------------------------------------------------- #
# 8~9：失败处理与退出码
# --------------------------------------------------------------------------- #
def test_single_endpoint_failure_does_not_abort_and_exit_code_is_zero():
    probe = load_probe_module()
    client = FakeAKShareClient(empty_calls={"stock_zh_a_hist"})
    with workspace_tmp("probe_one_fail") as tmp:
        code, out_dir = run_dry(tmp, probe, client=client)
        report, _ = read_report(out_dir)
    assert code == 0
    assert set(report["endpoints"]) == set(ENDPOINTS), "单端点失败不得中止其余端点的探测"
    assert report["endpoints"]["bars_raw"]["status"] == "empty"
    assert report["endpoints"]["bars_hfq"]["status"] == "empty"
    assert report["endpoints"]["calendar"]["status"] == "ok"
    assert "0 行" in report["endpoints"]["bars_raw"]["note"]
    assert report["capabilities_observed"]["daily_bars"] is False


def test_missing_akshare_yields_all_not_found_and_exit_code_two():
    probe = load_probe_module()

    def failing_importer(name: str) -> Any:
        raise ImportError(f"No module named {name!r}")

    client, version, error = probe.load_akshare(importer=failing_importer)
    assert version is None
    assert isinstance(error, str) and "import akshare 失败" in error

    with workspace_tmp("probe_no_akshare") as tmp:
        code = probe.main(["--out", str(tmp), "--end", PROBE_END], client=client)
        report, markdown = read_report(tmp)  # live 模式：直接写 out 目录
    assert code == 2, "live 模式全部端点失败 → 退出码 2"
    assert report["mode"] == "live" and report["akshare_version"] is None
    statuses = {endpoint_id: record["status"] for endpoint_id, record in report["endpoints"].items()}
    # industry 的依赖（board_list）也失败了 → 它必然是 dependency_missing（未发请求），
    # 但那不是「静默跳过」：备注里必须带上上游错误原文。
    assert statuses["industry"] == "dependency_missing"
    assert statuses["board_list"] == "not_found"
    assert all(
        status in ("not_found", "dependency_missing") for status in statuses.values()
    ), f"akshare 缺失时只能是 not_found/dependency_missing，实际 {statuses}"
    for endpoint_id, record in report["endpoints"].items():
        detail = f"{record['error'] or ''}{record['note']}"
        assert "import akshare 失败" in detail, f"{endpoint_id} 没说清为什么失败：{detail}"
    assert any("not_found" in item for item in report["recommendations"])
    assert "not_found" in markdown


# --------------------------------------------------------------------------- #
# 10~12：纯函数与 CLI
# --------------------------------------------------------------------------- #
def test_classify_status_vocabulary():
    probe = load_probe_module()
    cases: Sequence[tuple[BaseException | None, int | None, str]] = (
        (None, 3, "ok"),
        (None, 0, "empty"),
        (AttributeError("no attribute"), None, "not_found"),
        (NotImplementedError("nope"), None, "not_found"),
        (TimeoutError("timed out"), None, "timeout"),
        (ConnectionError("connection refused"), None, "network"),
        (OSError("socket error"), None, "network"),
        (RuntimeError("missing token in request"), None, "token"),
        (RuntimeError("boom"), None, "error"),
    )
    for exc, rows, expected in cases:
        assert probe.classify_status(exc, rows=rows) == expected, f"{exc!r} 应判为 {expected}"
    with raises(ValueError):
        probe.classify_status(None)  # 编程错误：返回正常却没给行数


def test_check_pagination_signature_analysis():
    probe = load_probe_module()
    assert probe.check_pagination(FakeAKShareClient().stock_zh_a_hist)["supported"] is False

    class PagedStub:
        """带 page_size 参数的假客户端（现实中 AKShare 的 8 个候选端点都没有）。"""

        __name__ = "PagedStub"

        def stock_zh_a_hist(
            self, *, symbol: str, period: str = "daily", start_date: str = "",
            end_date: str = "", adjust: str = "", page_size: int = 50,
        ) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "日期": ["2022-03-01"], "开盘": [1.0], "收盘": [2.0], "最高": [2.0],
                    "最低": [1.0], "成交量": [10], "成交额": [2000.0],
                }
            )

    stub = PagedStub()
    pagination = probe.check_pagination(stub.stock_zh_a_hist)
    assert pagination["supported"] is True
    assert pagination["page_param"] == "page_size" and pagination["page_size"] == 50

    ctx = probe.make_context(end=PROBE_END, days=PROBE_DAYS)
    record = probe.probe_endpoint(stub, "bars_raw", ctx=ctx, limiter=RateLimiter(requests_per_minute=0))
    assert record["status"] == "ok"
    assert record["pagination"]["attempted"] is True
    assert record["pagination"]["rows_in_page"] == 1
    assert "单页上限需人工确认" in record["pagination"]["note"]

    plain = probe.probe_endpoint(
        FakeAKShareClient(), "bars_raw", ctx=ctx, limiter=RateLimiter(requests_per_minute=0)
    )
    assert plain["pagination"]["supported"] is False
    assert "签名无分页参数" in plain["pagination"]["note"]


def test_cli_arguments_and_help_and_fixture_root_guard():
    probe = load_probe_module()
    parser = probe.build_parser()
    namespace = parser.parse_args(
        [
            "--out", "somewhere", "--symbols", "600001, 000002", "--index", "399300.SZ",
            "--timeout", "5", "--days", "3", "--repeats", "2", "--end", "2022-03-07",
            "--report-period", "20211231", "--dry-run", "--fixture-root", "fx",
        ]
    )
    assert namespace.symbols == ("600001", "000002")
    assert namespace.index == "399300.SZ" and namespace.timeout == 5.0
    assert namespace.days == 3 and namespace.repeats == 2
    assert namespace.dry_run is True and namespace.fixture_root == "fx"
    assert str(namespace.end.date()) == "2022-03-07"
    assert str(namespace.report_period.date()) == "2021-12-31"

    with raises(SystemExit) as info:
        probe.main(["--help"])
    assert info.value.code == 0

    # 控制台编码陷阱（第 5 类编码坑的同类）：Windows 控制台是 cp936/GBK，
    # --help 里若有 GBK 编不出的字符（例如 U+2212 减号），argparse 打印帮助时会
    # 直接抛 UnicodeEncodeError —— 本用例初版就是这样红起来的。
    probe.build_parser().format_help().encode("gbk")


def test_fixture_root_in_live_mode_is_rejected_before_any_call():
    """footgun 防护：`--fixture-root` 在 **live 模式**必须**立刻报错退出**，不得静默忽略。

    静默忽略的后果特别隐蔽：用户以为在用 fixture 试跑，实际在**真实抓取**（花配额、
    动本地缓存，还可能把半截数据写进缓存）。所以这里不只看退出码，还要证明
    「拒绝发生在任何调用与任何落盘之前」。
    """
    import contextlib
    import io

    probe = load_probe_module()
    client = FakeAKShareClient()
    with workspace_tmp("probe_fixture_guard") as tmp:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with raises(SystemExit) as info:
                # 没有 --dry-run ⇒ live 模式
                probe.main(
                    ["--out", str(tmp), "--fixture-root", str(FIXTURES), "--end", PROBE_END],
                    client=client,
                )
        assert info.value.code == 2, "argparse 的用法错误必须以退出码 2 结束"
        assert "--dry-run" in stderr.getvalue(), "错误信息必须点明它只能与 --dry-run 同用"
        assert client.calls == [], "被拒绝时不得发出任何调用（否则就真的在抓数据）"
        assert not list(tmp.iterdir()), "被拒绝时不得写出任何报告"

        # 正向对照：同样传 --fixture-root，但带上 --dry-run 就能正常跑
        code, out_dir = run_dry(tmp, probe, "--fixture-root", str(FIXTURES))
        assert code == 0
        report, _ = read_report(out_dir)
        assert report["mode"] == "dry-run" and report["client"] == "FakeAKShareClient"


# --------------------------------------------------------------------------- #
# 12b：日期口径（`--end` / `--report-period` 的解析与来源）
# --------------------------------------------------------------------------- #
def test_latest_quarter_end_semantics():
    """`report_period` 的默认口径：≤ end 的**最近已过**季末（不是当季未到的季末）。

    取「当季未到的季末」会让 fundamentals 接口返回空表 —— 使用者会把「参数不对」
    误读成「接口不可用」，这是探测工具最不能给的假结论。
    """
    probe = load_probe_module()
    cases = {
        "2022-03-15": "2021-12-31",  # 当季未到季末 → 取上一季（明确 2 的结论）
        "2022-03-31": "2022-03-31",  # 当天就是季末 → 取它自己
        "2022-01-01": "2021-12-31",
        "2022-04-01": "2022-03-31",
        "2022-12-31": "2022-12-31",
        "2023-01-02": "2022-12-31",
    }
    for day, expected in cases.items():
        actual = str(probe.latest_quarter_end(day).date())
        assert actual == expected, f"{day} 的报告期应为 {expected}，实际 {actual}"


def test_context_records_resolved_dates_and_sources():
    """命令里的 `--end` 可以省略，但**报告里的日期必须可复现**，且要能看出是推导还是显式指定。"""
    probe = load_probe_module()

    derived = probe.make_context(end=PROBE_END).as_dict()
    assert derived["window"][1] == PROBE_END, "报告必须写解析后的绝对结束日期"
    assert derived["end_source"] == "cli(--end)"
    assert derived["report_period"] == "2021-12-31"
    assert derived["report_period_source"] == "derived(最近已过季末)"
    assert "未公布" in derived["report_period_reason"], "必须写明为什么这么取报告期"

    explicit = probe.make_context(end="2022-03-15", report_period="20211231").as_dict()
    assert explicit["report_period_source"] == "cli(--report-period)"
    assert "显式指定" in explicit["report_period_reason"]

    # 不传 --end：end 落到「今天（UTC）」，来源必须如实标注（默认值不可复现，
    # 但报告里记下的日期是可复现的）
    with workspace_tmp("probe_default_end") as tmp:
        code = probe.main(["--dry-run", "--out", str(tmp)])
        report, markdown = read_report(tmp / "dry-run")
    assert code == 0
    context = report["context"]
    assert context["end_source"] == "default(今天, UTC)"
    assert context["window"][1] == str(probe.utc_now().date())
    assert context["window"][1] in markdown, "解析后的日期必须出现在报告页首"


# --------------------------------------------------------------------------- #
# 13：报告三件套 + md/json 一致
# --------------------------------------------------------------------------- #
def test_markdown_carries_three_piece_headers_and_matches_json_statuses():
    probe = load_probe_module()
    with workspace_tmp("probe_md") as tmp:
        _, out_dir = run_dry(tmp, probe)
        report, markdown = read_report(out_dir)

    assert "生成命令" in markdown and report["command"] in markdown
    assert "生成时间" in markdown and report["probed_at"] in markdown
    assert "来源" in markdown and "tools/probe_akshare.py" in markdown
    assert "口径说明" in markdown and "单位推断" in markdown

    found = dict(re.findall(r"^### (\S+) — status=(\S+)$", markdown, flags=re.MULTILINE))
    assert set(found) == set(report["endpoints"]), "每个端点都必须有独立小节"
    for endpoint_id, status in found.items():
        assert status == report["endpoints"][endpoint_id]["status"], f"{endpoint_id} 的 md/json 状态不一致"


# --------------------------------------------------------------------------- #
# 14：mapper_check 的只读声明
# --------------------------------------------------------------------------- #
def test_mapper_check_is_read_only():
    """`mapper_check` 的「只读」声明必须可验证（补充 3）。

    声明是「不写缓存、不发新请求、不改全局状态」。其中：

    - **不发请求**是**结构性**的：`mapper_check` 的签名里根本没有 client，无从发请求；
    - **不写盘**用「禁止 `open`」的方式守：任何落盘动作都会立刻抛断言错误；
    - **不改入参**用深比较守（值 / 列 / dtype / `attrs` 全都不许动）——
      映射器若就地修改入参，provider 侧会拿到被污染的数据，而**不会报错**。
    """
    from unittest import mock

    probe = load_probe_module()
    frame = pd.DataFrame(
        {
            "日期": ["2022-03-01", "2022-03-02"], "开盘": [10.0, 10.2], "收盘": [10.2, 10.05],
            "最高": [10.3, 10.25], "最低": [9.95, 10.0], "成交量": [1234, 900],
            "成交额": [1258680.0, 904500.0],
        }
    )
    frame.attrs["warnings"] = ("原始警告",)
    before = frame.copy(deep=True)
    before.attrs = dict(frame.attrs)
    mappers_before = dict(probe.MAPPERS)

    # 上下文用工具自己的 `_mapper_context` 造（单一实现）：这样每个映射器拿到的都是
    # 它真正需要的参数，失败才只可能因为「列不对」而不是「参数没给」。
    ctx = probe.make_context(symbols=("600000.SH",), end=PROBE_END, days=PROBE_DAYS)
    contexts = {
        name: probe._mapper_context(probe.ENDPOINTS[name], ctx, industry_name="银行")
        for name in probe.MAPPERS
    }
    with mock.patch("builtins.open", side_effect=AssertionError("mapper_check 不得写盘")):
        results = {
            name: probe.mapper_check(name, frame, context=contexts[name])
            for name in sorted(probe.MAPPERS)
        }

    assert results["bars_raw"]["ok"] is True and results["bars_raw"]["rows"] == 2
    assert results["bars_hfq"]["ok"] is True
    # 同一张行情表喂给财务映射器必然失败（没有股票代码列）→ 证明映射器真的被执行了，
    # 而不是「什么都没做所以没副作用」
    assert results["fundamentals"]["ok"] is False
    assert "股票代码" in (results["fundamentals"]["error"] or "")

    assert frame.equals(before), "mapper_check 不得修改入参内容"
    assert list(frame.columns) == list(before.columns)
    assert dict(frame.attrs) == dict(before.attrs), "mapper_check 不得改动入参的 attrs"
    assert dict(probe.MAPPERS) == mappers_before, "mapper_check 不得改动全局映射器表"


# --------------------------------------------------------------------------- #
# 15：探测与 provider 的参数一致性（Q3 修正：单一实现）
# --------------------------------------------------------------------------- #
def test_probe_uses_same_kwargs_as_provider():
    """交叉比对两边的**调用账**：探测发出的每次调用，provider 也以完全相同参数发出。

    这条用例是「两份实现漂移」的机械红灯：`client_kwargs_for` 的调用参数若被探测侧
    自行拼凑（例如用股票代码去查指数成分），这里立刻不一致。
    """
    probe = load_probe_module()
    start, end = "2022-03-01", "2022-03-07"

    with workspace_tmp("probe_kwargs") as tmp:
        provider_client = FakeAKShareClient()
        provider = AKShareProvider(
            DataConfig(provider="akshare"),
            client=provider_client,
            cache=DataCache(tmp / "cache", provider="akshare"),
            limiter=RateLimiter(requests_per_minute=0),
            sleeper=lambda _seconds: None,
            now=lambda: pd.Timestamp("2022-03-15 08:00:00", tz="UTC").to_pydatetime(),
        )
        provider.fetch_bars(["600000.SH"], start, end, adjust="hfq")
        provider.fetch_symbol_meta(["600000.SH"])
        provider.fetch_trading_calendar(start, end)
        provider.fetch_index_members("000300.SH", start, end)
        provider.fetch_industry(["600000.SH"])
        provider.fetch_fundamentals(["600000.SH"], start, end)
        provider_calls = list(provider_client.calls)

        probe_client = FakeAKShareClient()
        ctx = probe.make_context(
            symbols=("600000.SH",), index_code="000300.SH", end=end, days=PROBE_DAYS
        )
        report = probe.probe_all(
            probe_client, ctx=ctx, limiter=RateLimiter(requests_per_minute=0), mode="dry-run"
        )
        probe_calls = list(probe_client.calls)

    def keyset(calls: Sequence[tuple[str, Mapping[str, Any]]]) -> set[tuple[str, tuple[Any, ...]]]:
        return {(fn, tuple(sorted(kwargs.items()))) for fn, kwargs in calls}

    provider_set, probe_set = keyset(provider_calls), keyset(probe_calls)
    assert probe_set, "探测没有发出任何调用"
    assert probe_set <= provider_set, (
        "探测构造的调用参数与 provider 不一致（探测结论对 provider 无效）：\n"
        f"仅探测发出：{sorted(probe_set - provider_set)}"
    )
    assert {fn for fn, _ in probe_calls} == {fn for fn, _ in provider_calls}, (
        "探测覆盖的函数集必须与 provider 用到的函数集一致"
    )
    assert set(report["endpoints"]) == set(ENDPOINTS)
    assert report["mode"] == "dry-run"
