"""Backtests reuse the exact production engine on an in-memory ledger, one session at a time.
The engine only ever sees bars up to the current session, so look-ahead is structurally impossible.

Walk-forward note: parameters are fixed ex-ante (no fitting), so walk-forward evaluation = consecutive
out-of-sample calendar-year windows, plus a final hold-out window reported separately."""
from __future__ import annotations

import math
from dataclasses import replace

import pandas as pd

from .calendar_xetra import TradingCalendar
from .config import DataPolicy, RiskPolicy, Settings, StrategyParams
from .data import validate
from .engine import run_cycle
from .ledger import Ledger

NO_LIMITS = dict(daily_loss_limit=math.inf, max_drawdown_limit=math.inf)


def simulate(bars: pd.DataFrame, cal: TradingCalendar, s: Settings) -> tuple[pd.DataFrame, Ledger]:
    validate(bars, cal, None, s.data)  # full-series check once; per-step checks are equivalent
    led = Ledger(":memory:")
    led.init(s.risk.initial_equity_eur, s.isin)
    for i in range(len(bars)):
        run_cycle(led, bars.iloc[: i + 1], cal, s, validate_data=False)
    snaps = pd.DataFrame(led.snapshots())
    snaps["session"] = pd.to_datetime(snaps["session"])
    return snaps.set_index("session"), led


def metrics(equity: pd.Series, start_equity: float | None = None) -> dict:
    if equity.empty:
        return {}
    e0 = start_equity if start_equity is not None else float(equity.iloc[0])
    rets = equity.pct_change().fillna(equity.iloc[0] / e0 - 1)
    years = max(len(equity) / 252, 1e-9)
    total = float(equity.iloc[-1] / e0 - 1)
    peak = equity.cummax().clip(lower=e0)
    vol = float(rets.std() * math.sqrt(252))
    return {
        "start": equity.index[0].date().isoformat(), "end": equity.index[-1].date().isoformat(),
        "sessions": len(equity), "total_return": total,
        "cagr": (1 + total) ** (1 / years) - 1 if total > -1 else -1.0,
        "max_drawdown": float((1 - equity / peak).max()), "ann_vol": vol,
        "sharpe_rf0": float(rets.mean() * 252 / vol) if vol > 0 else 0.0,
    }


def run_comparison(bars: pd.DataFrame, cal: TradingCalendar, base: Settings | None = None,
                   holdout_sessions: int = 252) -> dict:
    base = base or Settings()
    variants = {
        "strategy_sma200_with_risk_policy": base,
        "strategy_sma200_no_limits": replace(base, risk=replace(base.risk, **NO_LIMITS)),
        "buy_hold_25pct": replace(base, strategy=StrategyParams(kind="hold", hold_weight=0.25),
                                  risk=replace(base.risk, **NO_LIMITS)),
        "buy_hold_100pct": replace(base, strategy=StrategyParams(kind="hold", hold_weight=1.0),
                                   risk=replace(base.risk, max_position_weight=1.0, **NO_LIMITS)),
    }
    out: dict = {"variants": {}, "data_source": sorted(set(bars["source"])),
                 "first_bar": bars.index[0].date().isoformat(), "last_bar": bars.index[-1].date().isoformat()}
    e0 = base.risk.initial_equity_eur
    for name, st in variants.items():
        snaps, led = simulate(bars, cal, st)
        eq = snaps["equity"]
        halt = snaps.index[snaps["state"] == "HALTED"]
        disabled = snaps.index[snaps["state"] == "NEW_TRADES_DISABLED"]
        windows = []
        is_eq, ho_eq = eq.iloc[:-holdout_sessions], eq.iloc[-holdout_sessions:]
        for yr, seg in is_eq.groupby(is_eq.index.year):
            prev = eq[eq.index < seg.index[0]]
            windows.append({"window": str(yr), **metrics(seg, float(prev.iloc[-1]) if len(prev) else e0)})
        prev = eq[eq.index < ho_eq.index[0]]
        out["variants"][name] = {
            "full": metrics(eq, e0),
            "walk_forward_windows": windows,
            "holdout": metrics(ho_eq, float(prev.iloc[-1]) if len(prev) else e0),
            "costs": led.total_costs(),
            "first_halt": halt[0].date().isoformat() if len(halt) else None,
            "first_trading_disabled": disabled[0].date().isoformat() if len(disabled) else None,
            "final_equity": float(eq.iloc[-1]),
        }
    out["variants"]["cash_only"] = {"full": {"total_return": 0.0, "max_drawdown": 0.0}, "final_equity": e0}
    return out
