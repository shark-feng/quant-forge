# AKShare 离线样例（**手工构造，不是真实抓取数据**）

> ⚠️ **诚实声明**：本目录下的 CSV **全部是手工构造的样例**，按 `docs/11_akshare_provider.md` §3/§4
> 记录的**候选**接口列名编写，用于离线测试字段映射与单位换算。
> 它们**不是**从 AKShare 抓取的真实行情/财务/成分数据，也**不代表**任何真实证券的实际情况。
> 真实字段名、dtype、分页与限额由使用者联网执行 `tools/probe_akshare.py` 探测后固化（M5）。
>
> 因此：这些文件**只能**证明「映射代码按预期消费这种形状的数据」，
> **不能**证明「AKShare 真的返回这种列名」。后者必须由探测报告回答。

## 文件清单

| 文件 | 模拟接口 | 关键点 |
|---|---|---|
| `stock_zh_a_hist_600000_raw.csv` | `stock_zh_a_hist(adjust="")` | 中文列名；**成交量单位是「手」**（映射需 ×100 转股）；含 `涨跌幅`（与复权因子交叉校验用） |
| `stock_zh_a_hist_600000_hfq.csv` | `stock_zh_a_hist(adjust="hfq")` | 与原始序列的比值即复权因子；样例中 03-04 起比值由 2.0 变 2.2（模拟一次除权），用于验证「首日归一 + 比值变化」 |
| `stock_individual_info_em_600000.csv` | `stock_individual_info_em` | `item/value` 长表；`上市时间=19991110`（紧凑写法，映射需解析） |
| `tool_trade_date_hist_sina.csv` | `tool_trade_date_hist_sina` | 单列 `trade_date` |
| `index_stock_cons_csindex_000300_20220301.csv` | `index_stock_cons_csindex` | **当前**成分快照（03-01：600000/000001） |
| `index_stock_cons_csindex_000300_20220315.csv` | 同上 | 第二次快照（03-15：000001/300750）→ 验证区间闭合：600000 在 03-14 退出 |
| `stock_yjbb_em_20211231.csv` | `stock_yjbb_em(date="20211231")` | 报告期 2021-12-31（年报），公告日落在 2022-03/04；**其中一行公告日为空**（映射必须丢行并计数，不得用报告期顶替）；另有一行公告日早于请求区间（应按 PIT 口径排除）。**公告日必须晚于报告期** —— 样例刻意如此，否则就是财务未来函数 |
| `stock_board_industry_name_em.csv` | `stock_board_industry_name_em` | 行业板块列表（银行 / 半导体） |
| `stock_board_industry_cons_em_bank.csv` | `stock_board_industry_cons_em(银行)` | 板块成分；文件名用 ASCII（`bank`）以避免中文文件名在不同平台的编码问题 |
| `stock_board_industry_cons_em_semi.csv` | `stock_board_industry_cons_em(半导体)` | 同上（`semi`） |

## 与真实数据的差距（必须知道）

1. **列名可能不同**：候选名来自文档与社区经验，探测后可能需改 `MAPPERS` / `ENDPOINTS`（单点修改）；
2. **规模极小**：真实接口按页返回数千行、按报告期分批，这里每文件只有几行；
3. **没有真实异常**：限额、超时、空返回、字段改名等失败模式由测试里的 `FakeAKShareClient` 注入模拟；
4. **财务单位未核实**：真实接口可能以「万元/亿元」返回，样例统一为元 —— **单位换算是 M5 的必须核实项**。
