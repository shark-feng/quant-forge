# M1 数据层设计：接口 / 伪代码 / 测试用例

> 对应代码：`src/aqs/data/`、`src/aqs/core/calendar.py`
> 验收门禁：`tests/test_data_schema.py`、`test_calendar.py`、`test_data_store.py`、`test_universe.py` 全绿。

---

## 1. 设计目标

1. **PIT（Point-In-Time）正确**：任何查询都必须显式或隐式限定 `as_of` 时间点，越界访问抛 `LookaheadError`（而非静默返回）。
2. **双价格体系**：原始价（成交/涨跌停/成本）与后复权价（信号/收益）分开存放、分开取用。
3. **公告日优先**：财务数据一律按 `announce_date` 过滤，`report_period` 只作为标签。
4. **股票池可复现**：给定日期与配置，股票池是纯函数；含已退市股票与历史指数成分，规避幸存者偏差。
5. **可替换数据源**：CSV / Parquet / 合成数据三种 provider 走同一 schema。

---

## 2. 列规范（Canonical Schema）

### 2.1 日线 `bars`（长表，主键 `(date, symbol)`）

| 列 | 类型 | 必需 | 说明 |
|---|---|---|---|
| date | datetime64[ns] | ✅ | 交易日（已归一化到 00:00） |
| symbol | str | ✅ | `600000.SH` 格式 |
| open/high/low/close | float64 | ✅ | 原始价，>0 |
| volume | float64 | ✅ | 股 |
| amount | float64 | ⭕ | 元；缺失时用 `close × volume` 估算并记 warning（流动性过滤依赖它） |
| adj_factor | float64 | ⭕ | 累计后复权因子，>0（默认 1.0） |
| prev_close | float64 | ⭕ | 缺失时用同标的上一条 close 补齐 |
| limit_up / limit_down | float64 | ⭕ | 缺失时按板块规则推算 |
| is_suspended | bool | ⭕ | 默认 False；缺列时用 `volume <= 0` 推断 |
| is_st | bool | ⭕ | 默认 False；按标的前向填充 |
| list_date | datetime64[ns] | ⭕ | 上市日；缺失时用「该标的第几根 K 线」估算上市交易日数 |
| delist_date | datetime64[ns] | ⭕ | 退市日（NaN=未退市） |

**派生列**（由 `normalize_bars` 计算，不允许数据源自带）：
`bar_seq`、`board`、`prev_adj_factor`、`open_adj/high_adj/low_adj/close_adj`、`base_adj_factor`。

### 2.2 指数成分 `index_members`（主键 `(index_code, symbol, effective_from)`）

`index_code, symbol, effective_from, effective_to(NaN=当前有效)`
→ 查询 `members(index_code, date)` 取 `effective_from <= date <= effective_to`。

### 2.3 财务数据 `fundamentals`（主键 `(symbol, report_period, announce_date)`）

`symbol, report_period, announce_date, roe, net_profit, revenue, total_assets, total_equity, ...`
→ **过滤规则**：`announce_date <= as_of`，同一 `report_period` 取 `announce_date` 最大的一条。

### 2.4 交易日历

由 `bars` 中出现过的日期并集派生（`source=derived`），或从文件读取（`source=file`）。停牌日 **仍然计入交易日**（关键：停牌不等于休市）。

---

## 3. 接口定义

```python
# src/aqs/data/store.py
class DataStore:
    def __init__(self, bars, calendar, index_members=None, fundamentals=None,
                 config: DataConfig | None = None, name: str = "store") -> None

    # ---- 元信息 ----
    def trading_days(self, start=None, end=None) -> list[date]
    def symbols(self, include_delisted: bool = True) -> list[str]
    def symbol_meta(self, symbol) -> SymbolMeta            # list_date/delist_date/board

    # ---- 行情 ----
    def bar(self, symbol, date, *, as_of=None) -> Bar | None
    def bars_on(self, date, symbols=None, *, as_of=None) -> dict[str, Bar]
    def history(self, symbol, end, window: int, *, fields=None, as_of=None) -> DataFrame
    def history_panel(self, symbols, end, window, field="close_adj", *, as_of=None) -> DataFrame
    def adv(self, symbol, end, window: int = 20, *, as_of=None) -> float   # 日均成交额
    def turnover(self, symbol, end, window: int = 20, *, as_of=None) -> float

    # ---- 财务（公告日口径）----
    def fundamentals(self, symbols, as_of, fields=None) -> DataFrame

    # ---- 股票池 ----
    def index_members(self, index_code, date) -> list[str]
    def universe(self, date, *, config=None, index_code=None) -> list[str]

    # ---- PIT ----
    def as_of(self, ts) -> PITView

class PITView:                    # 只读视图，禁止越界
    __slots__ = ("_store", "_as_of", "violations")
    trading_days / bars_on / history / adv / universe / fundamentals  # 全部把 _as_of 作为上界
```

**越界行为**：`PITView` 在内部断言 `requested_end <= self._as_of`，否则 `raise LookaheadError(...)`，并把违规记录进 `violations`（供测试与审计）。

---

## 4. 伪代码

### 4.1 复权价计算（loader 阶段一次性完成）

```
factor_ref = adj_factor[第一个交易日]            # 后复权基准，使首日价格 = 原始价
for col in (open, high, low, close):
    col + "_adj" = col * adj_factor / factor_ref
```

要点：**绝不**用后复权价判断涨跌停、计算成交金额与费用——撮合与成本全部走原始价。

### 4.2 股票池动态调整（`UniverseBuilder.build(date)`）

```
candidates = index_members(index_code, date)  if mode == index else all_listed(date)
result = []
for s in candidates:
    bar = bars[s, date]
    if bar is None:                      continue   # 当日无数据
    if exclude_suspended and bar.is_suspended:      continue
    if exclude_st and bar.is_st:                    continue
    if bar.listed_days < min_list_days:             continue   # 上市不足 60 个交易日
    if adv(s, date, liquidity_window) < min_amount:  continue   # 20 日日均成交额 < 5000 万
    result.append(s)
return sorted(result)
```

- `all_listed(date)` 包含 **已退市股票**（`list_date <= date <= delist_date`），保证幸存者偏差可控。
- 所有窗口只使用 `date` 当日及之前的数据。

### 4.3 数据质量校验（`validate_bars`）

```
检查项：
  1. 必需列存在、类型可转换
  2. (date, symbol) 无重复
  3. OHLC > 0；high >= max(open, close) >= min(open, close) >= low
  4. adj_factor > 0 且单调（同一 symbol 内不出现 0/负）
  5. volume >= 0, amount >= 0
  6. limit_up >= limit_down；未停牌日 close 落在 [limit_down, limit_up]
  7. list_date <= date <= delist_date（若有）
  8. 停牌日 volume == 0 的软告警
输出：DataQualityReport(errors, warnings, stats) —— errors 非空时调用方决定 raise 或记录
```

---

## 5. 测试用例（已实现）

下表每一行都对应 `tests/` 下真实执行的用例；当前数据层相关用例共 **66 个**，全部通过。

| # | 用例 | 断言 | 对应测试 |
|---|---|---|---|
| T1.1 | 列缺失 / 重名 | `validate_bars`/`normalize_bars` 报错且信息含列名 | `test_data_schema.py::test_normalize_bars_requires_columns` |
| T1.2 | 重复 `(date,symbol)` | 报 error | `test_validate_duplicate_key` |
| T1.3 | `adj_factor <= 0` | 报 error | `test_validate_bad_adj_factor` |
| T1.4 | `high < low` / 非正价格 | 报 error | `test_validate_bad_price_and_range` |
| T1.5 | 停牌日 volume>0 | 仅 warning，不阻断 | `test_validate_suspension_is_warning_only` |
| T1.6 | 后复权计算 | 首日 `close_adj == close`；除权日比例正确 | `test_adjusted_prices_use_first_bar_as_base` |
| T1.7 | 涨跌停推算 | 主板 10% / 创业板 20% / ST 5%，四舍五入到分 | `test_limit_prices_main_board_and_st`、`test_limit_prices_round_to_cent` |
| T2.1 | `next/prev_trading_day`、`sessions` | 跨周末/越界返回 `None` | `test_calendar.py`（6 个用例） |
| T2.2 | `next_tradable_day` | 跳过停牌日；全停牌返回 `None` | `test_next_tradable_day_skips_suspension` |
| T3.1 | **未来函数**：`history(end=d)` 最大日期 ≤ d | 断言成立 | `test_history_never_returns_future_rows` |
| T3.2 | **未来函数**：`PITView(as_of=d)` 请求未来 | 抛 `LookaheadError`，`violations` 记录 | `test_as_of_view_blocks_future_request`、`test_as_of_view_blocks_future_bars_on_and_universe` |
| T3.3 | **未来函数**：财务数据 `announce_date <= as_of` | 报告期已过但未公告的记录不可见 | `test_fundamentals_follow_announce_date`、`test_fundamentals_point_in_time_visibility` |
| T3.4 | 同一报告期多条公告 | 取 `announce_date` 最大者 | `test_fundamentals_point_in_time_visibility` |
| T3.5 | `adv` / `rolling_field` 只用 ≤ as_of 的窗口 | 手算一致；窗口不足返回 `None`/0 | `test_adv_uses_only_past_data`、`test_rolling_value_matches_manual_mean` |
| T3.6 | `bars_on(d)` 只返回 d 当日数据 | 集合相等 | `test_bars_on_returns_only_that_day` |
| T4.1 | **幸存者偏差**：退市股在退市前仍在池内 | 包含 | `test_delisted_symbol_included_before_delisting` |
| T4.2 | 退市后日期不含该股 | 排除 | `test_delisted_symbol_absent_after_delist_date` |
| T4.3 | ST 过滤（可开关） | 剔除 / 放开 | `test_universe_applies_all_filters`、`test_st_filter_can_be_disabled` |
| T4.4 | 上市不足 60 交易日过滤 | 剔除 | `test_listed_days_are_trading_days`、`test_listing_days_filter_disabled` |
| T4.5 | 流动性不足（20 日均额 < 5000 万）过滤 | 剔除；阈值边界保留 | `test_liquidity_threshold_boundary` |
| T4.6 | 历史指数成分：不同日期成员不同 | 与变更表一致 | `test_index_mode_uses_historical_membership` |

---

## 6. 数据接入约定（真实数据源）

实现一个 `BarLoader` 即可接入真实数据：

```python
class BarLoader(Protocol):
    def load_bars(self, symbols=None, start=None, end=None) -> pd.DataFrame: ...      # 必须返回 canonical schema
    def load_index_members(self, index_code) -> pd.DataFrame: ...
    def load_fundamentals(self, symbols=None) -> pd.DataFrame: ...                    # 必须含 announce_date
```

禁止在 loader 内做任何复权/填充的“智能”操作；只做字段映射与类型归一，其余交给数据层。
