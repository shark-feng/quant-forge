"""数据质量检查 Q1~Q12（M4）。

与 `data/schema.py::validate_bars`（结构级校验，8 类）的分工：

- `validate_bars`：**canonical schema 的结构与基本业务约束**（列是否齐、价格是否为正），
  面向「数据能不能进 DataStore」；
- 本模块 `QualityChecker`：**数据源层面的口径与偏差检查**（单位量级、复权跳变、
  公告日、覆盖缺口、幸存者偏差），面向「这份数据可不可信、要不要披露」。

`docs/11_akshare_provider.md` §7 的 Q1~Q12 逐条实现，
其中两条是本项目最在意的陷阱：

- **Q3 量级校验**：识别「成交量单位是手还是股」。若把「手」当「股」，
  成交额、参与率上限、冲击成本会整体错 100 倍，而且**不会报错**；
- **Q12 幸存者偏差自检**：候选池里若一只退市标的都没有，很可能是用了
  「当前存续股」的名单，回测收益会被系统性高估。

本模块是**纯函数式**的：只读输入、返回报告，不修改任何传入的 DataFrame，
也不做落盘（落盘与 CLI 属 M5）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

__all__ = ["QualityFinding", "QualityThresholds", "ProviderQualityReport", "QualityChecker"]

#: Q1~Q12 的固定标题（报告里按此顺序展示）
QUALITY_TITLES: dict[str, str] = {
    "Q1": "主键重复",
    "Q2": "OHLC 合法性",
    "Q3": "成交量单位一致性（手 vs 股）",
    "Q4": "复权因子",
    "Q5": "涨跌停区间",
    "Q6": "停牌一致性",
    "Q7": "上市/退市一致性",
    "Q8": "交易日覆盖",
    "Q9": "跨表一致性",
    "Q10": "公告日",
    "Q11": "市值/财务非负",
    "Q12": "幸存者偏差自检",
}


@dataclass(slots=True)
class QualityThresholds:
    """各项检查的阈值（全部可配置，代码中无魔法数字）。"""

    amount_ratio_low: float = 0.5       # Q3：amount/(volume×close) 合理下界
    amount_ratio_high: float = 2.0      # Q3：合理上界
    adj_jump: float = 0.30              # Q4：|Δln(adj_factor)| 告警阈值
    coverage_gap_ratio: float = 0.05    # Q8：标的存在日期缺口的最大容许比例
    min_symbols_for_coverage: int = 2   # Q8：样本标的太少时不做覆盖率判断
    max_samples: int = 20               # 每条 finding 保留的样本行数


@dataclass(slots=True)
class QualityFinding:
    code: str
    level: str          # error | warning
    message: str
    count: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return QUALITY_TITLES.get(self.code, self.code)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "level": self.level,
            "message": self.message,
            "count": self.count,
            "samples": list(self.samples),
        }


@dataclass(slots=True)
class ProviderQualityReport:
    findings: list[QualityFinding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @property
    def ok(self) -> bool:
        return not self.errors()

    def errors(self) -> list[QualityFinding]:
        return [f for f in self.findings if f.level == "error"]

    def warnings(self) -> list[QualityFinding]:
        return [f for f in self.findings if f.level == "warning"]

    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    def add(self, finding: QualityFinding) -> "ProviderQualityReport":
        self.findings.append(finding)
        return self

    def merge(self, other: "ProviderQualityReport") -> "ProviderQualityReport":
        self.findings.extend(other.findings)
        self.stats.update(other.stats)
        return self

    def exit_code(self) -> int:
        """0 = 无 error；2 = 存在 error（供 CLI 使用，与 POSIX 习惯一致）。"""
        return 2 if self.errors() else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": len(self.errors()),
            "warnings": len(self.warnings()),
            "exit_code": self.exit_code(),
            "stats": dict(self.stats),
            "findings": [f.as_dict() for f in self.findings],
        }

    def to_markdown(self, *, max_samples: int | None = None) -> str:
        limit = max_samples if max_samples is not None else 20
        lines: list[str] = ["# 数据质量报告", ""]
        lines.append(f"- 结论：{'✅ 通过' if self.ok else '❌ 存在错误'}")
        lines.append(f"- 错误 {len(self.errors())} 条 / 告警 {len(self.warnings())} 条")
        lines.append(f"- 退出码：`{self.exit_code()}`")
        lines.append("")
        if self.stats:
            lines.append("## 统计")
            lines.append("")
            lines.append("| 指标 | 值 |")
            lines.append("| --- | --- |")
            for key, value in self.stats.items():
                lines.append(f"| `{key}` | {value} |")
            lines.append("")
        if not self.findings:
            lines.append("未发现质量问题。")
            return "\n".join(lines) + "\n"

        lines.append("## 明细")
        lines.append("")
        lines.append("| 编号 | 检查项 | 级别 | 数量 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in self.findings:
            level = "错误" if f.level == "error" else "告警"
            lines.append(f"| {f.code} | {f.title} | {level} | {f.count} | {f.message} |")
        lines.append("")

        detailed = [f for f in self.findings if f.samples]
        if detailed:
            lines.append("## 样本（最多各 %d 条）" % limit)
            for f in detailed:
                lines.append("")
                lines.append(f"### {f.code} {f.title}")
                for row in f.samples[:limit]:
                    lines.append(f"- {row}")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 检查器
# --------------------------------------------------------------------------- #
class QualityChecker:
    """Q1~Q12 检查器。所有方法只读输入。"""

    def __init__(self, *, thresholds: QualityThresholds | None = None) -> None:
        self.thresholds = thresholds or QualityThresholds()

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _rows(self, frame: pd.DataFrame, mask: Any, columns: Sequence[str]) -> list[dict[str, Any]]:
        """把命中行转成样本（只保留存在的列，并转成可序列化类型）。"""
        cols = [c for c in columns if c in frame.columns]
        if not cols:
            return []
        sub = frame.loc[mask, cols].head(self.thresholds.max_samples)
        out: list[dict[str, Any]] = []
        for record in sub.to_dict("records"):
            out.append({k: _jsonable(v) for k, v in record.items()})
        return out

    @staticmethod
    def _has(frame: pd.DataFrame | None, columns: Sequence[str]) -> bool:
        return frame is not None and len(frame) > 0 and all(c in frame.columns for c in columns)

    # ------------------------------------------------------------------ #
    # Q1 / Q2：行情表自身
    # ------------------------------------------------------------------ #
    def check_primary_key(self, bars: pd.DataFrame) -> ProviderQualityReport:
        """Q1 主键 ``(date, symbol)`` 重复。"""
        report = ProviderQualityReport()
        if not self._has(bars, ("date", "symbol")):
            return report
        dup = bars.duplicated(subset=["date", "symbol"], keep=False)
        n = int(dup.sum())
        report.stats["duplicate_rows"] = n
        if n:
            report.add(
                QualityFinding(
                    "Q1",
                    "error",
                    f"主键 (date, symbol) 存在 {n} 条重复记录（会导致同一日重复撮合/重复计收益）",
                    count=n,
                    samples=self._rows(bars, dup, ("date", "symbol", "close")),
                )
            )
        return report

    def check_ohlc(self, bars: pd.DataFrame) -> ProviderQualityReport:
        """Q2 OHLC 合法性：``high >= max(open, close)``、``low <= min(open, close)``、``high >= low``。"""
        report = ProviderQualityReport()
        if not self._has(bars, ("open", "high", "low", "close")):
            return report

        upper = bars[["open", "close"]].max(axis=1)
        lower = bars[["open", "close"]].min(axis=1)
        bad = (
            (bars["high"] < bars["low"])
            | (bars["high"] < upper - 1e-9)
            | (bars["low"] > lower + 1e-9)
            | (bars[["open", "high", "low", "close"]] <= 0).any(axis=1)
        )
        n = int(bad.sum())
        report.stats["ohlc_violations"] = n
        if n:
            report.add(
                QualityFinding(
                    "Q2",
                    "error",
                    f"存在 {n} 条 OHLC 区间不合法（high/low 未覆盖 open/close，或存在非正值）",
                    count=n,
                    samples=self._rows(bars, bad, ("date", "symbol", "open", "high", "low", "close")),
                )
            )
        return report

    # ------------------------------------------------------------------ #
    # Q3：手 vs 股（本项目最在意的陷阱）
    # ------------------------------------------------------------------ #
    def check_volume_unit(self, bars: pd.DataFrame) -> ProviderQualityReport:
        """Q3 成交量单位一致性：``amount / (volume × close)`` 中位数应落在合理区间。

        量纲正确时该比值 ≈ 1（amount 为元、volume 为股）。
        若 volume 实际是「手」（1 手 = 100 股），比值 ≈ 100 → **必须报错**：
        否则成交额、参与率上限（基于 volume）与冲击成本会整体错 100 倍且无人察觉。
        """
        report = ProviderQualityReport()
        if not self._has(bars, ("amount", "volume", "close")):
            return report

        denom = bars["volume"] * bars["close"]
        valid = (denom > 0) & bars["volume"].notna() & bars["close"].notna()
        if not bool(valid.any()):
            report.stats["amount_ratio_median"] = None
            return report

        ratio = (bars.loc[valid, "amount"] / denom[valid]).astype(float)
        median = float(ratio.median())
        report.stats["amount_ratio_median"] = round(median, 4)
        report.stats["amount_ratio_rows"] = int(valid.sum())

        low, high = self.thresholds.amount_ratio_low, self.thresholds.amount_ratio_high
        if not (low <= median <= high):
            hint = ""
            if median > high:
                hint = (
                    f"比值偏高（≈{median:.1f}），极可能是 volume 的单位是「手」而不是「股」；"
                    "请在 data.unit_conversion.volume_to_shares 配置 100"
                )
            else:
                hint = f"比值偏低（≈{median:.3f}），可能是 amount 单位不是元（如千元），或 close 口径不一致"
            report.add(
                QualityFinding(
                    "Q3",
                    "error",
                    f"成交额/成交量量级异常（中位数 {median:.4f}，期望 ∈ [{low}, {high}]）。{hint}",
                    count=int(valid.sum()),
                    samples=self._rows(
                        bars,
                        valid & ((ratio < low) | (ratio > high)),
                        ("date", "symbol", "volume", "close", "amount"),
                    ),
                )
            )
        return report

    # ------------------------------------------------------------------ #
    # Q4~Q7：行情附加字段
    # ------------------------------------------------------------------ #
    def check_adjustment_factors(self, bars: pd.DataFrame) -> ProviderQualityReport:
        """Q4 复权因子：必须为正（error）；单日跳变 |Δln f| > 阈值（warning）。"""
        report = ProviderQualityReport()
        if not self._has(bars, ("adj_factor",)):
            return report

        bad = (bars["adj_factor"] <= 0) | bars["adj_factor"].isna()
        n_bad = int(bad.sum())
        report.stats["adj_factor_invalid"] = n_bad
        if n_bad:
            report.add(
                QualityFinding(
                    "Q4",
                    "error",
                    f"adj_factor 存在 {n_bad} 个非正/缺失值（无法正确复权）",
                    count=n_bad,
                    samples=self._rows(bars, bad, ("date", "symbol", "adj_factor")),
                )
            )

        # 跳变：按标的逐序列计算 |Δln f|
        jumps = pd.Series(False, index=bars.index)
        if "symbol" in bars.columns and "date" in bars.columns:
            frame = bars[["date", "symbol", "adj_factor"]].copy()
            frame = frame[frame["adj_factor"] > 0]
            if len(frame):
                frame = frame.sort_values(["symbol", "date"], kind="stable")
                ln = frame["adj_factor"].astype(float).map(math.log)
                delta = ln.groupby(frame["symbol"]).diff().abs()
                jumps.loc[frame.index] = (delta > self.thresholds.adj_jump).fillna(False)
        n_jump = int(jumps.sum())
        report.stats["adj_factor_jumps"] = n_jump
        if n_jump:
            report.add(
                QualityFinding(
                    "Q4",
                    "warning",
                    f"存在 {n_jump} 处复权因子单步跳变超过 {self.thresholds.adj_jump:.0%}"
                    "（可能对应真实的除权除息，也可能是数据错误，请抽查）",
                    count=n_jump,
                    samples=self._rows(bars, jumps, ("date", "symbol", "adj_factor")),
                )
            )
        return report

    def check_price_limits(self, bars: pd.DataFrame) -> ProviderQualityReport:
        """Q5 涨跌停区间：未停牌日的 close 应落在 [limit_down, limit_up] 内。"""
        report = ProviderQualityReport()
        if not self._has(bars, ("limit_up", "limit_down", "close")):
            return report

        both = bars["limit_up"].notna() & bars["limit_down"].notna()
        inverted = both & (bars["limit_up"] < bars["limit_down"])

        tradable = both.copy()
        if "is_suspended" in bars.columns:
            tradable &= ~bars["is_suspended"].astype(bool)
        out_of_band = tradable & (
            (bars["close"] > bars["limit_up"] + 1e-6) | (bars["close"] < bars["limit_down"] - 1e-6)
        )

        n_inv, n_band = int(inverted.sum()), int(out_of_band.sum())
        report.stats["limit_inverted"] = n_inv
        report.stats["close_out_of_limit"] = n_band
        if n_inv:
            report.add(
                QualityFinding(
                    "Q5",
                    "warning",
                    f"存在 {n_inv} 条 limit_up < limit_down 的记录（涨跌停价推算错误）",
                    count=n_inv,
                    samples=self._rows(bars, inverted, ("date", "symbol", "limit_up", "limit_down")),
                )
            )
        if n_band:
            report.add(
                QualityFinding(
                    "Q5",
                    "warning",
                    f"存在 {n_band} 条未停牌日收盘价越出涨跌停区间的记录（会影响「买不进/卖不出」判定）",
                    count=n_band,
                    samples=self._rows(
                        bars, out_of_band, ("date", "symbol", "close", "limit_up", "limit_down")
                    ),
                )
            )
        return report

    def check_suspension_consistency(self, bars: pd.DataFrame) -> ProviderQualityReport:
        """Q6 停牌一致性：``is_suspended`` 与 ``volume == 0`` 应一致。"""
        report = ProviderQualityReport()
        if not self._has(bars, ("is_suspended", "volume")):
            return report

        flag = bars["is_suspended"].astype(bool)
        zero = bars["volume"].fillna(0) <= 0
        mismatch = flag != zero
        n = int(mismatch.sum())
        report.stats["suspension_mismatch"] = n
        if n:
            report.add(
                QualityFinding(
                    "Q6",
                    "warning",
                    f"存在 {n} 条停牌标记与成交量不一致的记录（停牌应有 0 成交，非停牌应有成交）",
                    count=n,
                    samples=self._rows(bars, mismatch, ("date", "symbol", "is_suspended", "volume")),
                )
            )
        return report

    def check_listing_consistency(
        self,
        bars: pd.DataFrame,
        *,
        symbol_meta: pd.DataFrame | None = None,
    ) -> ProviderQualityReport:
        """Q7 上市/退市一致性：行情不得早于 list_date，也不得晚于 delist_date。"""
        report = ProviderQualityReport()
        if not self._has(bars, ("date", "symbol")):
            return report

        meta = symbol_meta
        if not self._has(meta, ("symbol",)):
            # 退化：直接读 bars 自带的 list_date / delist_date 列
            meta = None
            if "list_date" not in bars.columns and "delist_date" not in bars.columns:
                return report

        frame = bars.copy()
        if meta is not None:
            cols = [c for c in ("symbol", "list_date", "delist_date") if c in meta.columns]
            frame = frame.drop(columns=[c for c in ("list_date", "delist_date") if c in frame.columns])
            frame = frame.merge(meta[cols], on="symbol", how="left")

        bad = pd.Series(False, index=frame.index)
        if "list_date" in frame.columns:
            ld = pd.to_datetime(frame["list_date"], errors="coerce")
            dt = pd.to_datetime(frame["date"], errors="coerce")
            bad |= (ld.notna() & dt.notna() & (dt < ld)).fillna(False)
        if "delist_date" in frame.columns:
            dd = pd.to_datetime(frame["delist_date"], errors="coerce")
            dt = pd.to_datetime(frame["date"], errors="coerce")
            bad |= (dd.notna() & dt.notna() & (dt > dd)).fillna(False)

        n = int(bad.sum())
        report.stats["listing_violations"] = n
        if n:
            report.add(
                QualityFinding(
                    "Q7",
                    "error",
                    f"存在 {n} 条行情日期超出 [list_date, delist_date] 的记录"
                    "（数据错位或上市/退市日错误）",
                    count=n,
                    samples=self._rows(frame, bad, ("date", "symbol", "list_date", "delist_date")),
                )
            )
        return report

    # ------------------------------------------------------------------ #
    # Q8：交易日覆盖
    # ------------------------------------------------------------------ #
    def check_calendar_coverage(
        self,
        bars: pd.DataFrame,
        *,
        calendar: Sequence[Any] | None = None,
    ) -> ProviderQualityReport:
        """Q8 交易日覆盖：非停牌日的缺失比例超过阈值 → 告警。"""
        report = ProviderQualityReport()
        if calendar is None or not self._has(bars, ("date", "symbol")):
            return report

        sessions = pd.to_datetime(pd.Series(list(calendar))).dt.normalize().dropna().unique()
        if len(sessions) == 0:
            return report

        frame = bars.copy()
        frame["_d"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        if "is_suspended" in frame.columns:
            frame = frame[~frame["is_suspended"].astype(bool)]
        frame = frame[frame["_d"].notna()]

        total_expected = len(sessions)
        gaps: dict[str, int] = {}
        for symbol, group in frame.groupby("symbol"):
            present = pd.Index(group["_d"].unique())
            missing = total_expected - len(present.intersection(pd.Index(sessions)))
            ratio = missing / total_expected
            if ratio > self.thresholds.coverage_gap_ratio:
                gaps[str(symbol)] = missing

        report.stats["coverage_expected_sessions"] = total_expected
        report.stats["coverage_gap_symbols"] = len(gaps)
        if gaps:
            report.add(
                QualityFinding(
                    "Q8",
                    "warning",
                    f"{len(gaps)} 个标的的交易日缺口超过 {self.thresholds.coverage_gap_ratio:.0%}"
                    "（非停牌日却无行情；若是数据源分页/限流导致，请重新抓取）",
                    count=len(gaps),
                    samples=[
                        {"symbol": s, "missing_sessions": n}
                        for s, n in sorted(gaps.items(), key=lambda kv: -kv[1])[
                            : self.thresholds.max_samples
                        ]
                    ],
                )
            )
        return report

    # ------------------------------------------------------------------ #
    # Q9：跨表一致性
    # ------------------------------------------------------------------ #
    def check_cross_tables(
        self,
        bars: pd.DataFrame,
        *,
        symbol_meta: pd.DataFrame,
        members: pd.DataFrame | None = None,
    ) -> ProviderQualityReport:
        """Q9 跨表一致性：``bars.symbol`` 与 ``index_members.symbol`` 必须都在 ``symbol_meta`` 中。"""
        report = ProviderQualityReport()
        if not self._has(symbol_meta, ("symbol",)):
            return report

        known = set(symbol_meta["symbol"].astype(str))
        missing_bars: list[str] = []
        if self._has(bars, ("symbol",)):
            missing_bars = sorted(set(bars["symbol"].astype(str)) - known)
        missing_members: list[str] = []
        if self._has(members, ("symbol",)):
            missing_members = sorted(set(members["symbol"].astype(str)) - known)

        report.stats["symbols_missing_meta_from_bars"] = len(missing_bars)
        report.stats["symbols_missing_meta_from_members"] = len(missing_members)
        if missing_bars or missing_members:
            detail = []
            if missing_bars:
                detail.append(f"行情表有 {len(missing_bars)} 个标的缺元信息")
            if missing_members:
                detail.append(f"指数成分表有 {len(missing_members)} 个标的缺元信息")
            report.add(
                QualityFinding(
                    "Q9",
                    "error",
                    "；".join(detail) + "（缺 list_date/board 等会导致股票池过滤口径不正确）",
                    count=len(missing_bars) + len(missing_members),
                    samples=[{"symbol": s, "from": "bars"} for s in missing_bars[:10]]
                    + [{"symbol": s, "from": "index_members"} for s in missing_members[:10]],
                )
            )
        return report

    # ------------------------------------------------------------------ #
    # Q10 / Q11：财务
    # ------------------------------------------------------------------ #
    def check_fundamentals(self, fundamentals: pd.DataFrame) -> ProviderQualityReport:
        """Q10 公告日：必须有 ``announce_date`` 且 ``>= report_period``；缺失即 error。"""
        report = ProviderQualityReport()
        if fundamentals is None or len(fundamentals) == 0:
            return report

        if "announce_date" not in fundamentals.columns:
            report.add(
                QualityFinding(
                    "Q10",
                    "error",
                    "财务表缺少 announce_date（公告日）：无法按公告日口径取数，"
                    "否则会造成未来函数（用未公告的财报做决策）",
                    count=int(len(fundamentals)),
                )
            )
            return report

        missing = fundamentals["announce_date"].isna()
        n_missing = int(missing.sum())

        bad_order = pd.Series(False, index=fundamentals.index)
        if "report_period" in fundamentals.columns:
            rp = pd.to_datetime(fundamentals["report_period"], errors="coerce")
            ad = pd.to_datetime(fundamentals["announce_date"], errors="coerce")
            bad_order = (ad.notna() & rp.notna() & (ad < rp)).fillna(False)
        n_order = int(bad_order.sum())

        report.stats["announce_date_missing"] = n_missing
        report.stats["announce_date_before_report_period"] = n_order
        if n_missing:
            report.add(
                QualityFinding(
                    "Q10",
                    "error",
                    f"存在 {n_missing} 条缺失公告日的记录",
                    count=n_missing,
                    samples=self._rows(fundamentals, missing, ("symbol", "report_period")),
                )
            )
        if n_order:
            report.add(
                QualityFinding(
                    "Q10",
                    "error",
                    f"存在 {n_order} 条公告日早于报告期的记录（时间倒置，必为数据错误）",
                    count=n_order,
                    samples=self._rows(
                        fundamentals, bad_order, ("symbol", "report_period", "announce_date")
                    ),
                )
            )
        return report

    def check_financial_non_negative(self, fundamentals: pd.DataFrame) -> ProviderQualityReport:
        """Q11 市值/财务非负：市值、总资产、净资产为负或为 0 的比例过高 → 告警。"""
        report = ProviderQualityReport()
        if fundamentals is None or len(fundamentals) == 0:
            return report

        fields = [c for c in ("total_mv", "total_assets", "net_assets") if c in fundamentals.columns]
        if not fields:
            return report

        bad = pd.Series(False, index=fundamentals.index)
        counts: dict[str, int] = {}
        for col in fields:
            invalid = (fundamentals[col].notna()) & (fundamentals[col] <= 0)
            counts[col] = int(invalid.sum())
            bad |= invalid.fillna(False)
        report.stats.update({f"non_positive_{k}": v for k, v in counts.items()})

        n = int(bad.sum())
        if n:
            report.add(
                QualityFinding(
                    "Q11",
                    "warning",
                    f"存在 {n} 条市值/财务非正值记录（检查项：{', '.join(fields)}）",
                    count=n,
                    samples=self._rows(fundamentals, bad, ("symbol", "report_period", *fields)),
                )
            )
        return report

    # ------------------------------------------------------------------ #
    # Q12：幸存者偏差自检
    # ------------------------------------------------------------------ #
    def check_survivorship(
        self,
        *,
        symbol_meta: pd.DataFrame,
        candidates: Sequence[str],
    ) -> ProviderQualityReport:
        """Q12 幸存者偏差自检：候选池里应**至少有一只已退市**标的。

        一只退市标的都没有，通常意味着用的是「当前存续股」名单 ——
        回测收益会被系统性高估（退市股往往对应大幅下跌）。

        注意：若数据源压根不提供退市信息（``capabilities.delistings=False``），
        这里仍会告警 —— 因为「查不到」与「没有」在风险上是等价的。
        """
        report = ProviderQualityReport()
        cand = [str(s) for s in candidates]
        report.stats["survivorship_candidates"] = len(cand)
        if not cand:
            report.stats["survivorship_delisted"] = 0
            report.add(
                QualityFinding(
                    "Q12",
                    "warning",
                    "候选池为空，无法做幸存者偏差自检",
                    count=0,
                )
            )
            return report

        delisted: list[str] = []
        if self._has(symbol_meta, ("symbol", "delist_date")):
            meta = symbol_meta[symbol_meta["symbol"].astype(str).isin(set(cand))]
            dd = pd.to_datetime(meta["delist_date"], errors="coerce")
            delisted = sorted(meta.loc[dd.notna(), "symbol"].astype(str).tolist())

        report.stats["survivorship_delisted"] = len(delisted)
        if not delisted:
            report.add(
                QualityFinding(
                    "Q12",
                    "warning",
                    f"候选池 {len(cand)} 只标的中没有任何已退市标的："
                    "极可能只取到了当前存续股，回测收益存在幸存者偏差（系统性高估）",
                    count=len(cand),
                    samples=[{"symbol": s} for s in cand[: self.thresholds.max_samples]],
                )
            )
        return report

    # ------------------------------------------------------------------ #
    # 组合入口
    # ------------------------------------------------------------------ #
    def run_all(
        self,
        *,
        bars: pd.DataFrame,
        symbol_meta: pd.DataFrame | None = None,
        fundamentals: pd.DataFrame | None = None,
        members: pd.DataFrame | None = None,
        calendar: Sequence[Any] | None = None,
        candidates: Sequence[str] | None = None,
    ) -> ProviderQualityReport:
        """按 Q1~Q12 顺序跑全部检查（缺输入的项目自动跳过）。"""
        report = ProviderQualityReport()
        if bars is not None and len(bars):
            report.merge(self.check_primary_key(bars))
            report.merge(self.check_ohlc(bars))
            report.merge(self.check_volume_unit(bars))
            report.merge(self.check_adjustment_factors(bars))
            report.merge(self.check_price_limits(bars))
            report.merge(self.check_suspension_consistency(bars))
            report.merge(self.check_listing_consistency(bars, symbol_meta=symbol_meta))
            report.merge(self.check_calendar_coverage(bars, calendar=calendar))
            period = None
            if "date" in bars.columns and len(bars):
                dt = pd.to_datetime(bars["date"], errors="coerce").dropna()
                if len(dt):
                    period = (str(dt.min().date()), str(dt.max().date()))
            if period:
                report.stats["bars_start"], report.stats["bars_end"] = period
            report.stats["bars_rows"] = int(len(bars))
            if "symbol" in bars.columns:
                report.stats["bars_symbols"] = int(bars["symbol"].nunique())
        if symbol_meta is not None and len(symbol_meta):
            report.merge(self.check_cross_tables(bars, symbol_meta=symbol_meta, members=members))
        if fundamentals is not None and len(fundamentals):
            report.merge(self.check_fundamentals(fundamentals))
            report.merge(self.check_financial_non_negative(fundamentals))
        if candidates is not None and symbol_meta is not None:
            report.merge(self.check_survivorship(symbol_meta=symbol_meta, candidates=candidates))
        return report


def _jsonable(value: Any) -> Any:
    """把 numpy/pandas 标量转成可 JSON 序列化的 Python 原生类型。"""
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 6)
    if isinstance(value, pd.Timestamp):
        return str(value.date())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):  # pragma: no cover
            return str(value)
    return str(value)
