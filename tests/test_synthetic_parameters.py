"""V2：`generate_market_data` 的**参数路径解耦**（标的属性序列与代码分配分离）。

缺陷（V2）：属性序列（上市窗口 / 退市 / ST / 价格路径 / 财务数值）原先是多个位置
**共用一个 rng** 顺序抽样，抽样次数取决于**标的数量与遍历顺序** —— 于是同一个位置的属性
会随「是否传 `symbols` / 传几个 / 什么顺序」漂移。后果不是报错，而是
**做「两个入口一致性」比较时会得出假结论**（M4-10 的第一次一致性尝试就是这么被骗的）。

修复（`_position_rng`）：位置 ``i`` 的每一次抽样都来自 ``default_rng([seed, i])``，
故不变量成立 —— **第 i 个位置的属性只由 ``(seed, i)`` 决定**，与传入的代码/数量/顺序无关。

语义边界（刻意如此，见 docstring 与 `docs/DEVELOPMENT.md` §3）：
**属性属于「位置」，代码由调用方列表顺序决定**。因此「同一个代码在不同顺序下拿到不同属性」
是预期行为；一致性比较必须两侧用**同一顺序**，或两侧都不传 `symbols`。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tests.compat import raises

from aqs.data.synthetic import _symbol_codes, generate_market_data

START = "2022-01-04"
END = "2022-06-30"
SEED = 4242
N = 12  # 混合板块族（>6 才会出现深市代码），能暴露顺序/数量耦合


def make(**kwargs) -> object:
    kwargs.setdefault("seed", SEED)
    return generate_market_data(start=START, end=END, **kwargs)


def restrict(bundle, codes):
    """把 bundle 限制到给定代码（保持原始顺序无关，便于与更短的生成对比）。"""
    import types

    keep = {str(c) for c in codes}
    return types.SimpleNamespace(
        bars=bundle.bars[bundle.bars["symbol"].isin(keep)].copy(),
        fundamentals=bundle.fundamentals[bundle.fundamentals["symbol"].isin(keep)].copy(),
    )


def by_position(bundle, codes) -> pd.DataFrame:
    """把 bars 的行按「代码在 ``codes`` 中的位置」重排，并把 symbol 换成位置标签。

    这样比较的是**位置层面的属性**（上市窗口/退市/ST/价格/成交量），
    而不受「位置 i 上放的是哪个代码」影响 —— 正是本缺陷要锁定的不变量。
    """
    position = {str(c): i for i, c in enumerate(codes)}
    frame = bundle.bars.copy()
    frame["pos"] = frame["symbol"].map(position)
    assert frame["pos"].notna().all(), f"存在不属于 codes 的代码：{set(frame['symbol']) - set(codes)}"
    frame["symbol"] = frame["pos"].map(lambda p: f"POS{int(p):03d}")
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def fundamentals_by_position(bundle, codes) -> pd.DataFrame:
    position = {str(c): i for i, c in enumerate(codes)}
    frame = bundle.fundamentals.copy()
    frame["pos"] = frame["symbol"].map(position)
    frame["symbol"] = frame["pos"].map(lambda p: f"POS{int(p):03d}")
    return frame.sort_values(["symbol", "report_period"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 正例：同长度 + 同顺序 → 完全一致（含 symbol 列）
# --------------------------------------------------------------------------- #
def test_generating_with_internal_codes_matches_auto_generation():
    codes = _symbol_codes(N)
    auto = make(n_symbols=N)
    explicit = make(symbols=list(codes))

    pd.testing.assert_frame_equal(
        auto.bars.sort_values(["symbol", "date"]).reset_index(drop=True),
        explicit.bars.sort_values(["symbol", "date"]).reset_index(drop=True),
    )
    assert sorted(auto.bars["symbol"].unique()) == sorted(codes)


# --------------------------------------------------------------------------- #
# 反例 1：不同代码、同长度 → 属性按位置相同、代码不同
# --------------------------------------------------------------------------- #
def test_custom_codes_receive_attributes_by_position():
    codes = _symbol_codes(N)
    custom = [f"T{1000 + i:04d}.SH" for i in range(N)]
    auto = make(n_symbols=N)
    other = make(symbols=custom)

    assert set(other.bars["symbol"]) == set(custom)
    assert set(custom) != set(codes), "本用例要求两份代码不同"
    # 除代码本身外，逐行逐列完全相同（含 dtype）
    pd.testing.assert_frame_equal(by_position(auto, codes), by_position(other, custom))


# --------------------------------------------------------------------------- #
# 反例 2：**长度截断** → 前 k 个位置的属性必须与 N 个时一致（本次修复的缺陷）
# --------------------------------------------------------------------------- #
def test_attribute_sequence_does_not_depend_on_symbol_count():
    codes = _symbol_codes(N)
    auto = make(n_symbols=N)
    for k in (1, 4, 7):
        shorter = make(symbols=list(codes[:k]))
        assert set(shorter.bars["symbol"]) == set(codes[:k])
        pd.testing.assert_frame_equal(
            by_position(restrict(auto, codes[:k]), codes[:k]),
            by_position(shorter, codes[:k]),
        )


def test_fundamentals_also_decoupled_from_symbol_count():
    codes = _symbol_codes(N)
    auto = make(n_symbols=N)
    shorter = make(symbols=list(codes[:4]))
    pd.testing.assert_frame_equal(
        fundamentals_by_position(restrict(auto, codes[:4]), codes[:4]),
        fundamentals_by_position(shorter, codes[:4]),
    )


# --------------------------------------------------------------------------- #
# 反例 3：顺序 → 属性属于「位置」而不是「代码」（语义边界，必须显式锁定）
# --------------------------------------------------------------------------- #
def test_attributes_belong_to_positions_not_codes():
    """逆序传入：位置 i 的属性不变，但位置 i 上坐的是另一个代码。

    这条**不是**缺陷，而是刻意语义：把「属性」绑在位置上，才能保证
    「传内部生成的代码」与「不传」逐行一致（正例 1）。
    若哪天改成「属性绑代码」，本用例会失败，提醒同步更新文档与一致性测试写法。
    """
    codes = _symbol_codes(N)
    auto = make(n_symbols=N)
    reversed_codes = list(reversed(codes))
    flipped = make(symbols=reversed_codes)

    # 位置层面：完全一致（位置 i 的属性没变）
    pd.testing.assert_frame_equal(by_position(auto, codes), by_position(flipped, reversed_codes))
    # 代码层面：位置 0 上的代码变了（这就是「顺序敏感」的全部含义）
    pos0_auto = auto.bars.loc[auto.bars["symbol"] == codes[0]]
    pos0_flip = flipped.bars.loc[flipped.bars["symbol"] == reversed_codes[0]]
    assert codes[0] != reversed_codes[0]
    assert not pos0_auto.reset_index(drop=True).equals(pos0_flip.reset_index(drop=True)), (
        "位置 0 上的代码换了，其数据理应对应另一个位置的属性"
    )


# --------------------------------------------------------------------------- #
# 反例 4/5：非法入参必须显式报错（旧实现会静默产出坏数据）
# --------------------------------------------------------------------------- #
def test_duplicate_symbols_are_rejected():
    """旧实现：重复代码不报错，却产出主键 (date, symbol) 重复的 bars。"""
    with raises(ValueError) as ctx:
        make(symbols=["600001.SH", "600001.SH"])
    message = str(ctx.value)
    assert "重复" in message and "主键" in message

    # 正例：不重复则正常
    ok = make(symbols=["600001.SH", "600002.SH"])
    assert ok.bars["symbol"].nunique() == 2
    assert not ok.bars.duplicated(subset=["date", "symbol"]).any()


def test_empty_symbols_is_rejected_not_silently_regenerated():
    """旧实现：``symbols=[]`` 是 falsy → 静默退化为「按 n_symbols 自动生成」。"""
    with raises(ValueError) as ctx:
        make(symbols=[], n_symbols=N)
    assert "空序列" in str(ctx.value)

    # 不传（None）才是「自动生成」
    auto = make(n_symbols=N)
    assert auto.bars["symbol"].nunique() == N


# --------------------------------------------------------------------------- #
# 确定性：同参数两次生成必须完全一致（跨进程由 test_determinism.py 守护）
# --------------------------------------------------------------------------- #
def test_generation_is_deterministic_with_per_position_streams():
    a = make(n_symbols=N)
    b = make(n_symbols=N)
    pd.testing.assert_frame_equal(a.bars, b.bars)
    pd.testing.assert_frame_equal(a.fundamentals, b.fundamentals)

    # 不同 seed 必须给出不同数据（否则说明随机流没生效）
    c = make(n_symbols=N, seed=SEED + 1)
    assert not np.array_equal(a.bars["close"].to_numpy(), c.bars["close"].to_numpy())
