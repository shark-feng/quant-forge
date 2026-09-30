"""回测引擎主循环（日频事件驱动）。

单个交易日的事件序列（严格对应 ``docs/00_system_design.md`` §5.2）：

```
09:00  TimerEvent(PRE_OPEN)      解锁 T+1、计提现金分红、风控日初始化
09:30  MarketDataEvent(OPEN)     撮合 T-1 日收盘产生的订单 → FillEvent
15:00  MarketDataEvent(CLOSE)    按收盘价估值 → 记录净值
15:00  TimerEvent(CLOSE)
15:10  TimerEvent(POST_CLOSE)    策略计算 T 日信号 → 组合生成订单计划 → 风控 → 挂单
```

**T+1 保证**：信号在 T 日收盘后产生，订单的 ``submit_date`` 由引擎强制设为 T 的下一个交易日；
``Broker.create_order`` 会拒绝 ``submit_date <= signal_date`` 的订单（:class:`FutureFunctionError`）。

**架构说明**：引擎是「调度器 + 事件流水线」。驱动按阶段推进并发布事件（可订阅、可录制、可断言顺序），
账务更新由 :class:`~aqs.engine.broker.Broker` 在成交时同步完成，避免事件订阅者重复计账。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..config.loader import load_risk_config
from ..config.schema import BaseConfig, RiskConfig, UniverseConfig, construct
from ..core.dates import DateLike, to_date
from ..core.enums import OrderType, RejectReason, RiskAction, SessionPhase, Side
from ..core.events import EventRecorder
from ..core.exceptions import AQSError, EngineError
from ..core.logging import get_logger
from ..core.models import MarketSnapshot, Order, SignalIntent
from ..data.store import DataStore
from ..portfolio.base import OrderPlan, Portfolio
from ..risk.base import NullRiskEngine, RiskEngine
from ..risk.engine import RuleRiskEngine
from ..risk.stats import RejectionRecord, evaluate_rejections, summarize_latency
from ..strategy.base import Strategy, StrategyContext
from .account import Account
from .broker import Broker
from .cost import CostModel
from .event_loop import EngineEventLoop
from .matching import MatchingEngine

__all__ = ["BacktestEngine", "BacktestResult"]

logger = get_logger("engine.backtest")


@dataclass(slots=True)
class BacktestResult:
    """回测输出（供 M6 评价层与 M7 报告层消费）。"""

    config: BaseConfig | None
    store: DataStore
    account: Account
    broker: Broker
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    universe_records: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    recorder: EventRecorder | None = None
    risk: RiskEngine | None = None

    # ------------------------------------------------------------------ #
    def summary(self) -> dict[str, Any]:
        """基础摘要。

        注意：**完整评价指标体系（夏普/Calmar/Sortino/换手/容量/滑点敏感性）
        属于 M6 评价层**；本方法只给出链路自检所需的最小指标。
        """
        eq = self.equity_curve
        out: dict[str, Any] = {}
        out.update({f"account_{k}": v for k, v in self.account.summary().items() if k != "cost_detail"})
        out.update({f"broker_{k}": v for k, v in self.broker.summary().items() if k != "match_stats"})
        if len(eq):
            total_return = float(eq["total_value"].iloc[-1] / eq["total_value"].iloc[0] - 1.0)
            years = max(len(eq) / 252.0, 1e-9)
            out["total_return"] = total_return
            out["annualized_return"] = float((1.0 + total_return) ** (1.0 / years) - 1.0)
            out["max_drawdown"] = float(eq["drawdown"].min())
            out["n_days"] = int(len(eq))
        out["n_trades"] = int(len(self.trades))
        out["diagnostics"] = self.diagnostics
        return out

    def save(self, output_dir: str | Path | None = None) -> dict[str, Path]:
        """把回测结果落盘（CSV + JSON 摘要）。"""
        base = Path(output_dir or (self.config.report.output_dir if self.config else "reports"))
        base.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        if self.config is None or self.config.report.save_equity_curve:
            p = base / "equity_curve.csv"
            self.equity_curve.to_csv(p, encoding="utf-8-sig")
            written["equity_curve"] = p
        if self.config is None or self.config.report.save_trades:
            p = base / "trades.csv"
            self.trades.to_csv(p, index=False, encoding="utf-8-sig")
            written["trades"] = p
        p = base / "orders.csv"
        self.orders.to_csv(p, index=False, encoding="utf-8-sig")
        written["orders"] = p
        if self.universe_records:
            p = base / "universe_stats.csv"
            pd.DataFrame(self.universe_records).to_csv(p, index=False, encoding="utf-8-sig")
            written["universe_stats"] = p
        p = base / "summary.json"
        p.write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written["summary"] = p
        return written


class BacktestEngine:
    """日频事件驱动回测引擎。

    Args:
        store: 数据仓库（PIT 访问）。
        config: 主配置；``None`` 时使用默认配置。
        strategy: 策略实例，或 ``ctx -> Sequence[SignalIntent]`` 可调用对象。
        portfolio: 组合实例，或 ``(signals, ctx) -> Sequence[OrderPlan]`` 可调用对象。
        risk: 风控引擎；``None`` 时使用 :class:`NullRiskEngine`（M5 交付前为占位，会在日志中警告）。
        on_fill: 成交回调（报告层可直接消费）。
        subscribe: 事件订阅（``EventType`` 或事件类 → 处理器），用于审计/报告。
    """

    def __init__(
        self,
        store: DataStore,
        config: BaseConfig | Mapping[str, Any] | None = None,
        *,
        strategy: Strategy | Callable[[StrategyContext], Sequence[SignalIntent]] | None = None,
        portfolio: Portfolio | Callable[[Sequence[SignalIntent], StrategyContext], Sequence[OrderPlan]] | None = None,
        risk: RiskEngine | None = None,
        cost_model: CostModel | None = None,
        seed: int | None = None,
        on_fill: Callable[[Any], None] | None = None,
        subscribe: Mapping[Any, Callable[[Any], None]] | None = None,
    ) -> None:
        self.store = store
        self.config: BaseConfig = config if isinstance(config, BaseConfig) else construct(BaseConfig, dict(config or {}))
        self.strategy = strategy
        self.portfolio = portfolio
        self.cost_model = cost_model or CostModel(self.config.costs)
        self.seed = int(seed if seed is not None else self.config.project.seed)
        self.on_fill = on_fill
        self.risk_config: RiskConfig = self._load_risk_config()
        self.risk: RiskEngine = risk or self._build_risk_engine()
        self.loop = EngineEventLoop(on_error="raise")
        for key, handler in dict(subscribe or {}).items():
            self.loop.subscribe(key, handler)
        self._dropped_orders = 0
        self._rejections: list[RejectionRecord] = []

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def _load_risk_config(self) -> RiskConfig:
        path = self.config.risk.config_file
        if not path:
            return RiskConfig(enabled=self.config.risk.enabled)
        try:
            cfg = load_risk_config(path)
        except AQSError as exc:
            logger.warning("未能加载风控配置 %s（%s），改用默认阈值", path, exc)
            return RiskConfig(enabled=self.config.risk.enabled)
        cfg.enabled = bool(cfg.enabled and self.config.risk.enabled)
        return cfg

    def _build_risk_engine(self) -> RiskEngine:
        """按配置构建 RMS：启用时使用规则引擎，并注入行业映射与历史收益提供器。"""
        cfg = self.risk_config
        if not cfg.enabled:
            logger.info("风控未启用（risk.enabled=false），全部订单放行")
            return NullRiskEngine(dataclasses.replace(cfg, audit_log=None))
        engine = RuleRiskEngine(
            dataclasses.replace(cfg, audit_log=None),
            industry_of=self._industry_lookup,
            returns_of=self._returns_lookup,
            config_file=self.config.risk.config_file,
        )
        logger.info("风控已启用：%d 条规则（mode=%s）", len(engine.rules), cfg.mode)
        return engine

    def _industry_lookup(self, symbol: str) -> str | None:
        try:
            return self.store.symbol_meta(symbol).industry
        except AQSError:
            return None

    def _returns_lookup(self, symbol: str, day: DateLike, window: int) -> list[float]:
        """某标的截至 ``day`` 的最近 ``window`` 个收益率（PIT 安全）。"""
        import pandas as pd

        hist = self.store.history(symbol, day, window + 1, fields=["date", "close"])
        if hist.empty or len(hist) < 2:
            return []
        series = pd.Series(hist["close"].to_numpy(), index=pd.DatetimeIndex(hist["date"]))
        returns = series.pct_change().dropna()
        return [float(x) for x in returns.tail(window).tolist()]

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(
        self,
        *,
        start: DateLike | None = None,
        end: DateLike | None = None,
        universe_config: UniverseConfig | None = None,
    ) -> BacktestResult:
        cfg = self.config
        engine_cfg = cfg.engine
        cal = self.store.calendar

        if cal.first_day is None:
            raise EngineError("交易日历为空，无法回测")
        start_day = to_date(start if start is not None else (engine_cfg.start or cal.first_day))
        end_day = to_date(end if end is not None else (engine_cfg.end or cal.last_day))
        sessions = cal.sessions(start_day, end_day)
        if not sessions:
            raise EngineError(f"回测区间 {start_day} ~ {end_day} 内没有交易日")

        account = Account(engine_cfg.initial_cash)
        broker = Broker(
            account,
            engine_config=engine_cfg,
            cost_model=self.cost_model,
            matching=MatchingEngine(engine_cfg, cost_model=self.cost_model, seed=self.seed),
            seed=self.seed,
        )
        uni_cfg = universe_config or self.store.universe_config
        adv_window = cfg.costs.impact.advisory_window
        universe_records: list[dict[str, Any]] = []
        risk_hook = self._risk_hook()
        self._dropped_orders = 0
        self._rejections = []

        logger.info(
            "回测开始：%s ~ %s（%d 个交易日），初始资金 %.2f，成交价口径 %s",
            sessions[0],
            sessions[-1],
            len(sessions),
            engine_cfg.initial_cash,
            engine_cfg.execution_price,
        )

        for day in sessions:
            # ---------- ① 开盘前 ----------
            self.loop.publish_timer(day, SessionPhase.PRE_OPEN)
            self.loop.drain()
            universe = self.store.universe(day, config=uni_cfg)
            stats = self.store.universe_builder.last_stats
            if stats is not None:
                universe_records.append(stats.as_dict())

            snapshot_open = self._snapshot(day, SessionPhase.OPEN, adv_window, universe)
            account.unlock_t1()
            account.accrue_dividends(snapshot_open)
            self.risk.on_day_start(day, account)

            # ---------- ② 开盘：撮合上一交易日收盘产生的订单 ----------
            self.loop.publish_market_data(day, SessionPhase.OPEN, snapshot_open)
            self.loop.drain()
            fills = broker.process_open(snapshot_open, risk_hook=risk_hook)
            if fills:
                self.loop.publish_fills(snapshot_open, fills)
                self.loop.drain()
                for fill in fills:
                    self.risk.on_fill(fill)
                if self.on_fill is not None:
                    for fill in fills:
                        self.on_fill(fill)

            # ---------- ③ 收盘：估值与净值 ----------
            snapshot_close = self._snapshot(day, SessionPhase.CLOSE, adv_window, universe)
            self.loop.publish_market_data(day, SessionPhase.CLOSE, snapshot_close)
            self.loop.drain()
            account.mark_to_market(snapshot_close)
            self.loop.publish_timer(day, SessionPhase.CLOSE)
            self.loop.drain()
            account.record_daily(day)
            # 风控日终：以当日收盘净值更新峰值/回撤，使回撤类规则当日即可生效（延迟 1 个交易日）
            self.risk.on_day_end(day, account)
            # ---------- ④ 盘后：组合级风控 → 信号 → 组合 → 风控 → 挂单（T+1 执行） ----------
            self.loop.publish_timer(day, SessionPhase.POST_CLOSE)
            self.loop.drain()
            # 盘后决策使用 POST_CLOSE 快照（同一批收盘行情，时间戳 15:10），保证事件时间戳单调
            snapshot_post = MarketSnapshot(
                date=day,
                phase=SessionPhase.POST_CLOSE,
                bars=snapshot_close.bars,
                adv_volume=snapshot_close.adv_volume,
                ts=cal.timestamp(day, SessionPhase.POST_CLOSE),
            )
            # 组合级风控：与「当天有没有订单」无关，必须每日评估（回撤/当日亏损/撤单率）
            evaluate = getattr(self.risk, "evaluate_controls", None)
            if callable(evaluate):
                evaluate(account, snapshot_post)

            if self.strategy is not None:
                ctx = self._context(day, snapshot_post, account, universe, broker)
                signals = self._run_strategy(ctx)
                if signals:
                    self.loop.publish_signals(day, signals, getattr(self.strategy, "name", "strategy"))
                    self.loop.drain()
                plans = self._run_portfolio(signals, ctx)
                plans = self._apply_risk_controls(list(plans), account, snapshot_post)
                self._place_orders(broker, plans, day, snapshot_post)

        equity = account.equity_curve()
        result = BacktestResult(
            config=cfg,
            store=self.store,
            account=account,
            broker=broker,
            equity_curve=equity,
            trades=broker.fills_frame(),
            orders=broker.orders_frame(),
            universe_records=universe_records,
            recorder=self.loop.recorder,
            risk=self.risk,
        )
        result.diagnostics = self._diagnostics(broker, account, sessions, start_day, end_day)
        logger.info(
            "回测结束：净值 %.2f，收益 %.2f%%，成交 %d 笔，累计成本 %.2f 元",
            account.total_value,
            (account.total_value / account.initial_cash - 1.0) * 100.0,
            len(broker.fills),
            account.total_costs,
        )
        return result

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _snapshot(
        self,
        day: _date,
        phase: SessionPhase,
        adv_window: int,
        universe: Sequence[str],
    ) -> MarketSnapshot:
        """构建市场快照：当日全部行情 + 股票池 ADV（冲击成本用）。"""
        bars = self.store.bars_on(day)
        symbols = list(universe) if universe else list(bars.keys())
        adv_volume: dict[str, float] = {}
        for sym in symbols:
            value = self.store.rolling_value(sym, "volume", day, adv_window)
            if value is not None:
                adv_volume[sym] = value
        return MarketSnapshot(
            date=day,
            phase=phase,
            bars=bars,
            adv_volume=adv_volume,
            ts=self.store.calendar.timestamp(day, phase),
        )

    def _context(
        self,
        day: _date,
        snapshot: MarketSnapshot,
        account: Account,
        universe: Sequence[str],
        broker: Broker | None = None,
    ) -> StrategyContext:
        pending_buys: tuple[str, ...] = ()
        pending_sells: tuple[str, ...] = ()
        if broker is not None:
            pending_buys = tuple(
                dict.fromkeys(o.symbol for o in broker.working if o.side is Side.BUY)
            )
            pending_sells = tuple(
                dict.fromkeys(o.symbol for o in broker.working if o.side is Side.SELL)
            )
        return StrategyContext(
            date=day,
            phase=SessionPhase.POST_CLOSE,
            data=self.store.as_of(day),
            account=account,
            snapshot=snapshot,
            calendar=self.store.calendar,
            universe=list(universe),
            config=self.config,
            strategy_name=getattr(self.strategy, "name", ""),
            pending_buy_symbols=pending_buys,
            pending_sell_symbols=pending_sells,
        )

    def _run_strategy(self, ctx: StrategyContext) -> list[SignalIntent]:
        strategy = self.strategy
        if strategy is None:
            return []
        out = strategy.on_bar(ctx) if hasattr(strategy, "on_bar") else strategy(ctx)  # type: ignore[operator]
        return [s for s in (out or ()) if s is not None]

    def _run_portfolio(self, signals: Sequence[SignalIntent], ctx: StrategyContext) -> list[OrderPlan]:
        portfolio = self.portfolio
        if portfolio is None or not signals:
            return []
        plans = (
            portfolio.generate_orders(signals, ctx)
            if hasattr(portfolio, "generate_orders")
            else portfolio(signals, ctx)  # type: ignore[operator]
        )
        return [p for p in (plans or ()) if p is not None and p.quantity > 0]

    def _apply_risk_controls(
        self,
        plans: list[OrderPlan],
        account: Account,
        snapshot: MarketSnapshot,
    ) -> list[OrderPlan]:
        """把 RMS 的组合级动作（减仓 / 强平 / 暂停）落到订单计划上。

        - ``target_exposure_scale() < 1``：按「当前敞口 → 目标敞口」生成减仓卖单；
        - ``force_close``：清仓全部可卖持仓，并丢弃所有买入计划；
        - ``is_paused``：丢弃所有买入计划（已有持仓保留，卖出仍允许）。
        """
        scale = self.risk.target_exposure_scale()
        sells: list[OrderPlan] = []

        if scale < 1.0 and account.positions:
            total = account.total_value
            exposure = account.positions_value / total if total > 0 else 0.0
            if exposure > scale + 1e-6:
                reduction = 1.0 - (scale / exposure)
                for symbol in account.holding_symbols:
                    sellable = account.sellable(symbol)
                    if sellable <= 0:
                        continue
                    quantity = sellable if scale <= 0 else self._round_down(
                        sellable * reduction, self.config.engine.lot_size
                    )
                    if quantity <= 0:
                        continue
                    sells.append(
                        OrderPlan(
                            symbol,
                            Side.SELL,
                            quantity,
                            tag="risk_reduce" if scale > 0 else "risk_close",
                            reason=f"target_exposure_scale={scale:.2f}",
                        )
                    )
                if sells:
                    self.risk.audit.log(
                        "control",
                        "reduce_exposure",
                        ts=snapshot.date,
                        target_scale=scale,
                        current_exposure=exposure,
                        orders=len(sells),
                    )

        if self.risk.force_close_requested or self.risk.is_paused:
            blocked = sum(1 for p in plans if p.side is Side.BUY)
            plans = [p for p in plans if p.side is not Side.BUY]
            if blocked:
                self.risk.audit.log(
                    "control",
                    "buy_plans_blocked",
                    ts=snapshot.date,
                    count=blocked,
                    reason=self.risk.pause_reason or "force_close",
                )

        # 卖单在前：T+1 开盘 FIFO 撮合，先释放现金再买入
        return sells + plans

    @staticmethod
    def _round_down(quantity: float, lot: int) -> float:
        if quantity <= 0:
            return 0.0
        value = float(int(quantity // lot) * lot)
        if value <= 0 and quantity >= 1:
            return float(lot) if quantity >= lot else 0.0
        return value

    def _place_orders(
        self,
        broker: Broker,
        plans: Sequence[OrderPlan],
        signal_day: _date,
        snapshot: MarketSnapshot,
    ) -> list[Order]:
        """把订单计划转成订单，经风控后提交撮合队列（T+1 执行）。"""
        if not plans:
            return []
        next_day = self.store.calendar.next_trading_day(signal_day)
        if next_day is None:
            self._dropped_orders += len(plans)
            logger.debug("%s 是最后一个交易日，%d 条订单计划无法 T+1 执行，已忽略", signal_day, len(plans))
            return []

        submitted: list[Order] = []
        for plan in plans:
            order = broker.create_order(
                plan.symbol,
                plan.side,
                plan.quantity,
                signal_date=signal_day,
                decision_ts=snapshot.ts,
                submit_date=next_day,
                order_type=plan.order_type,
                limit_price=plan.limit_price,
                tag=plan.tag,
            )
            decision = self.risk.check_order(order, broker.account, snapshot)
            self.loop.publish_risk_check(snapshot, order, broker.account)
            self.loop.drain()
            if decision.action is RiskAction.REJECT:
                order.mark_rejected(decision.reject_reason or RejectReason.RISK_REJECTED, by_risk=True)
                self._record_rejection(order, decision.rule, snapshot)
                self.risk.on_reject(order, decision, snapshot)
                continue
            if decision.action is RiskAction.PAUSE:
                self._record_rejection(order, decision.rule, snapshot, action="pause")
                continue
            if decision.action is RiskAction.REDUCE and decision.modified_quantity is not None:
                original = order.quantity
                order.quantity = min(order.quantity, float(decision.modified_quantity))
                if order.quantity < original:
                    self._record_rejection(order, decision.rule, snapshot, action="reduce", quantity=original)
            if order.quantity <= 0:
                continue
            self.loop.publish_order(snapshot, order)
            self.loop.drain()
            broker.submit(order)
            self.risk.on_order_submitted(order, signal_day)
            submitted.append(order)
        return submitted

    def _record_rejection(
        self,
        order: Order,
        rule: str,
        snapshot: MarketSnapshot,
        *,
        action: str = "reject",
        quantity: float | None = None,
    ) -> None:
        """记录被风控拦截/削减的订单（供误杀率评估）。"""
        price = 0.0
        bar = snapshot.bar(order.symbol)
        if bar is not None:
            price = bar.close
        self._rejections.append(
            RejectionRecord(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=float(quantity if quantity is not None else order.quantity),
                price=price,
                day=snapshot.date,
                rule=rule,
                action=action,
            )
        )

    def _risk_hook(self) -> Callable[[Order, Account, MarketSnapshot], Any]:
        """撮合前的风控复核（价格偏离、暂停交易等依赖当日快照的规则）。"""

        def hook(order: Order, account: Account, snapshot: MarketSnapshot) -> Any:
            decision = self.risk.check_order(order, account, snapshot)
            self.loop.publish_risk_check(snapshot, order, account)
            self.loop.drain()
            if decision.action is RiskAction.REJECT:
                self._record_rejection(order, decision.rule, snapshot)
            return decision

        return hook

    def _diagnostics(
        self,
        broker: Broker,
        account: Account,
        sessions: Sequence[_date],
        start_day: _date,
        end_day: _date,
    ) -> dict[str, Any]:
        quality = self.store.quality
        risk_report = {}
        if self._rejections:
            report = evaluate_rejections(self._rejections, self.store, horizon=5)
            risk_report["rejection"] = report.as_dict()
        else:
            risk_report["rejection"] = {
                "n_rejections": 0,
                "n_evaluated": 0,
                "n_false": 0,
                "false_reject_rate": 0.0,
                "horizon_days": 5,
                "by_rule": {},
            }
        risk_report["latency"] = summarize_latency(self.risk.triggers).as_dict()
        risk_report["by_rule"] = dict(self.risk.stats.by_rule) if isinstance(self.risk, RuleRiskEngine) else {}
        risk_report["stats"] = (
            self.risk.stats.as_dict() if isinstance(self.risk, RuleRiskEngine) else {"checked": 0}
        )
        return {
            "sessions": len(sessions),
            "start": str(start_day),
            "end": str(end_day),
            "t_plus_one": self.config.engine.t_plus_one,
            "execution_price": self.config.engine.execution_price,
            "risk_engine": type(self.risk).__name__,
            "risk_enabled": bool(self.config.risk.enabled),
            "risk_paused": self.risk.is_paused,
            "risk_force_close": self.risk.force_close_requested,
            "risk_exposure_scale": self.risk.target_exposure_scale(),
            "risk_rules": (
                [r.name for r in self.risk.rules] if isinstance(self.risk, RuleRiskEngine) else []
            ),
            "risk": risk_report,
            "alerts": len(self.risk.alerts()),
            "cost_model": self.cost_model.describe(),
            "cost_missing_adv": self.cost_model.missing_adv_count,
            "matching": {
                "max_participation": self.config.engine.matching.max_participation,
                "allow_partial_fill": self.config.engine.matching.allow_partial_fill,
                "limit_up_fill_prob": self.config.engine.matching.limit_up_fill_prob,
            },
            "match_stats": dict(broker.matching.stats),
            "events_processed": self.loop.stats.processed,
            "events_by_type": dict(self.loop.stats.by_type),
            "orders_dropped_no_next_day": self._dropped_orders,
            "open_orders_at_end": len(broker.working),
            "store": self.store.describe(),
            "data_quality_errors": len(quality.errors) if quality else 0,
            "data_quality_warnings": len(quality.warnings) if quality else 0,
            "account_summary": account.summary(),
        }
