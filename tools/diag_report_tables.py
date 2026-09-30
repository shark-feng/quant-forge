"""诊断脚本：核对 reports/<run>/ 落盘表格的统计口径。

用途
----
D1/D2/D3 诊断任务取数工具。只读，不修改任何回测路径或报告文件。

用法
----
    set PYTHONPATH=D:\\Quantify\\src;D:\\Quantify
    python tools\\diag_report_tables.py [--reports D:\\Quantify\\reports] [--run ma_cross]

输出
----
1. trades.csv：按 (side, tag) 分类计数
2. orders.csv：按 status / reject_reason / side 分类计数
3. orders.csv：quantity 非整手（拒绝后）订单明细
4. orders.csv：status 与 reject_reason 同时非空的行（字段语义核查）
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import sys

# 整手股数（A 股买入最小单位）。此处仅用于诊断打印，不参与任何交易决策。
LOT_SIZE = 100


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _is_integral_lot(value: str) -> bool:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return True
    return abs(qty - round(qty)) < 1e-9 and int(round(qty)) % LOT_SIZE == 0


def _fmt_counter(counter: collections.Counter, indent: str = "    ") -> str:
    if not counter:
        return indent + "(空)"
    width = max(len(str(k)) for k in counter)
    lines = []
    for key, count in counter.most_common():
        lines.append(f"{indent}{str(key):<{width}}  {count}")
    return "\n".join(lines)


def diagnose_trades(run_dir: str) -> dict:
    rows = _read_csv(os.path.join(run_dir, "trades.csv"))
    print(f"== trades.csv  rows={len(rows)}")
    if not rows:
        return {}
    print("   列: " + ", ".join(rows[0].keys()))
    sides = collections.Counter(r.get("side", "") for r in rows)
    tags = collections.Counter(r.get("tag", "") for r in rows)
    pair = collections.Counter(
        (r.get("side", ""), r.get("tag", "")) for r in rows
    )
    print("   [side]"); print(_fmt_counter(sides, "      "))
    print("   [tag]"); print(_fmt_counter(tags, "      "))
    print("   [side x tag]"); print(_fmt_counter(pair, "      "))
    return {"rows": len(rows), "side": dict(sides), "tag": dict(tags)}


def diagnose_orders(run_dir: str) -> dict:
    rows = _read_csv(os.path.join(run_dir, "orders.csv"))
    print(f"== orders.csv  rows={len(rows)}")
    if not rows:
        return {}
    print("   列: " + ", ".join(rows[0].keys()))

    status = collections.Counter(r.get("status", "") for r in rows)
    reason = collections.Counter(r.get("reject_reason", "") for r in rows)
    side = collections.Counter(r.get("side", "") for r in rows)
    print("   [status]"); print(_fmt_counter(status, "      "))
    print("   [reject_reason]"); print(_fmt_counter(reason, "      "))
    print("   [side]"); print(_fmt_counter(side, "      "))

    # 非整手订单
    frac = [r for r in rows if not _is_integral_lot(r.get("quantity", ""))]
    print(f"   [非整手 quantity（非 100 倍数或小数）] {len(frac)} 条")
    for r in frac[:30]:
        print(
            "      {oid} side={side} qty={qty} status={status} reason={reason!r} "
            "filled={filled} ts={ts}".format(
                oid=r.get("order_id", r.get("id", "?")),
                side=r.get("side", ""),
                qty=r.get("quantity", ""),
                status=r.get("status", ""),
                reason=r.get("reject_reason", ""),
                filled=r.get("filled_quantity", r.get("filled", "")),
                ts=r.get("timestamp", r.get("created_at", "")),
            )
        )

    # status 与 reject_reason 并存的行
    both = [
        r for r in rows
        if (r.get("status", "") in ("filled", "partially_filled", "FILLED", "PARTIAL"))
        and (r.get("reject_reason") or "").strip() not in ("", "none", "None")
    ]
    print(f"   [已成交但带 reject_reason] {len(both)} 条")
    for r in both[:30]:
        print(
            "      {oid} status={status} reason={reason} filled={filled}".format(
                oid=r.get("order_id", r.get("id", "?")),
                status=r.get("status", ""),
                reason=r.get("reject_reason", ""),
                filled=r.get("filled_quantity", r.get("filled", "")),
            )
        )

    # 被拒订单的 quantity 分布
    rejected = [r for r in rows if r.get("status", "").upper().startswith(("REJECT", "REJ"))]
    print(f"   [status=REJECTED] {len(rejected)} 条")

    return {
        "rows": len(rows),
        "status": dict(status),
        "reject_reason": dict(reason),
        "fractional": len(frac),
        "filled_with_reason": len(both),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reports/<run>/ 表格口径诊断")
    parser.add_argument("--reports", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports"))
    parser.add_argument("--run", default=None, help="只诊断某一个 run 目录")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.reports):
        print(f"reports 目录不存在: {args.reports}", file=sys.stderr)
        return 2

    runs = [args.run] if args.run else sorted(
        d for d in os.listdir(args.reports)
        if os.path.isdir(os.path.join(args.reports, d))
    )
    summary: dict[str, dict] = {}
    for run in runs:
        run_dir = os.path.join(args.reports, run)
        print("#" * 70)
        print(f"# run = {run}    dir = {run_dir}")
        print("#" * 70)
        t = diagnose_trades(run_dir)
        o = diagnose_orders(run_dir)
        print()
        summary[run] = {"trades": t, "orders": o}
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
