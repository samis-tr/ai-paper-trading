"""The only component allowed to create and process paper orders.

Daily cycle for session D (run after D's close data is available):
  1. If HALTED: monitoring only (steps 2 and 4), no execution, no new orders.
  2. Validate data (stale / missing / inconsistent / calendar) -> stop on any problem.
  3. Execute PENDING orders scheduled for D at D's OPEN (+spread, slippage, commission) -> models gap risk.
  4. Mark to market at D's close, snapshot, evaluate risk limits.
  5. If still ACTIVE, decide target for D+1 and place an order for the NEXT session (idempotent).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import pandas as pd

from .calendar_xetra import TradingCalendar
from .config import SYNTHETIC_SOURCE, Settings
from .data import DataError, validate
from .ledger import ACTIVE, DISABLED, HALTED, Ledger, Order
from .strategy import target_weight

Notify = Callable[[str, str], None]  # (level, message)


@dataclass
class CycleResult:
    session: str | None
    state: str
    actions: list[str] = field(default_factory=list)
    snapshot: dict | None = None


def _qty(x: float, fractional: bool) -> float:
    return round(x, 6) if fractional else float(math.floor(x + 1e-9))


def run_cycle(ledger: Ledger, bars: pd.DataFrame, cal: TradingCalendar, s: Settings,
              now: datetime | None = None, validate_data: bool = True, allow_synthetic: bool = False,
              notify: Notify | None = None) -> CycleResult:
    notify = notify or (lambda level, msg: None)
    # When HALTED the cycle still validates data and marks to market (monitoring), but never
    # executes or places orders: pending orders are cancelled and no decision step runs.
    state = ledger.state()

    if validate_data:
        if not allow_synthetic and (bars["source"] == SYNTHETIC_SOURCE).any():
            raise DataError("Synthetic test data is not allowed in the paper-trading cycle")
        session = validate(bars, cal, now, s.data)
    else:
        session = bars.index[-1]
    skey = session.date().isoformat()

    last = ledger.last_snapshot()
    if last and last["session"] >= skey:
        return CycleResult(skey, state, [f"session {skey} already processed (idempotent re-run)"])

    bar = bars.loc[session]
    actions: list[str] = []

    # ---- 3. execute pending orders at today's open ----
    for o in ledger.pending_orders():
        if o.exec_session > skey:
            continue  # scheduled for a later session
        if o.exec_session < skey:
            ledger.close_order(o.id, "CANCELLED", f"missed execution session {o.exec_session} (outage/gap)")
            actions.append(f"cancelled stale order {o.id}")
        elif state != ACTIVE:
            ledger.close_order(o.id, "CANCELLED", f"trading state {state}")
            actions.append(f"cancelled order {o.id} (state {state})")
        else:
            actions.append(_execute(ledger, o, float(bar["open"]), skey, s))

    # ---- 4. mark to market and risk ----
    qty, cash = ledger.position(), ledger.cash()
    close = float(bar["close"])
    equity = cash + qty * close
    prev_eq = last["equity"] if last else ledger.initial_equity
    peak = max(last["peak"] if last else ledger.initial_equity, equity)
    dd = 1 - equity / peak
    daily_pnl = equity - prev_eq
    daily_ret = equity / prev_eq - 1

    if dd >= s.risk.max_drawdown_limit:
        msg = f"Drawdown {dd:.2%} >= {s.risk.max_drawdown_limit:.0%} on {skey}: system HALTED"
        ledger.set_state(HALTED, msg, "CRITICAL")
        notify("CRITICAL", msg)
        actions.append("HALT: drawdown limit")
    elif daily_ret <= -s.risk.daily_loss_limit and ledger.state() == ACTIVE:
        msg = f"Daily loss {daily_ret:.2%} <= -{s.risk.daily_loss_limit:.0%} on {skey}: new paper trades disabled"
        ledger.set_state(DISABLED, msg, "CRITICAL")
        notify("CRITICAL", msg)
        actions.append("DISABLE: daily loss limit")
    state = ledger.state()

    snap = dict(session=skey, close=close, qty=qty, cash=round(cash, 6), equity=round(equity, 6),
                peak=round(peak, 6), drawdown=round(dd, 8), daily_pnl=round(daily_pnl, 6),
                daily_ret=round(daily_ret, 8), state=state, data_source=str(bar["source"]))
    ledger.add_snapshot(**snap)

    # ---- 5. decide for next session ----
    if state == ACTIVE:
        a = _decide(ledger, bars["close"], session, qty, equity, close, cal, s)
        if a:
            actions.append(a)
    return CycleResult(skey, state, actions, snap)


def _decide(ledger: Ledger, closes: pd.Series, session: pd.Timestamp, qty: float, equity: float,
            close: float, cal: TradingCalendar, s: Settings) -> str | None:
    max_w = s.risk.max_position_weight
    tw = target_weight(closes, qty > 0, s.strategy, max_w)
    desired = _qty(tw * equity / close, s.costs.fractional_shares)
    cur_w = qty * close / equity if equity > 0 else 0.0
    diff = desired - qty
    if tw == 0 and qty > 0:
        side, n, why = "SELL", qty, "signal exit"
    elif diff > 0 and (qty == 0 or diff * close / equity >= s.strategy.rebalance_threshold):
        side, n, why = "BUY", diff, f"target {tw:.0%}"
    elif diff < 0 and cur_w > max_w + 0.02:
        side, n, why = "SELL", -diff, f"trim to cap ({cur_w:.1%} > {max_w:.0%})"
    else:
        if tw > 0 and qty == 0 and desired == 0:
            ledger.event("INFO", "NO_TRADE", f"Target {tw:.0%} smaller than one share at {close:.2f}")
        return None
    nxt = cal.next_session(session).date().isoformat()
    isin = ledger.meta("isin")
    oid = ledger.place_order(f"{isin}:{nxt}", isin, side, n, session.date().isoformat(), nxt, why)
    return f"order {oid}: {side} {n:g} for {nxt}" if oid else "duplicate order ignored"


def _execute(ledger: Ledger, o: Order, open_px: float, skey: str, s: Settings) -> str:
    c = s.costs
    held, cash = ledger.position(), ledger.cash()
    if o.side == "SELL":
        q = min(o.qty, held)  # long-only: never sell more than held
        if q <= 0:
            ledger.close_order(o.id, "REJECTED", "nothing to sell (long-only)")
            return f"rejected order {o.id}"
        ledger.record_fill(o, skey, q, open_px, c.sell_price(open_px), c.commission_eur, "filled at open")
        return f"filled SELL {q:g} @ open {open_px:.2f}"
    # BUY: re-check cap and cash at the actual open price (gap risk)
    price = c.buy_price(open_px)
    equity_open = cash + held * open_px
    cap_q = _qty(s.risk.max_position_weight * equity_open / price - held, c.fractional_shares)
    cash_q = _qty((cash - c.commission_eur) / price, c.fractional_shares)
    q = min(o.qty, cap_q, cash_q)
    if q <= 0:
        ledger.close_order(o.id, "REJECTED", f"cap/cash check at open failed (cap_q={cap_q}, cash_q={cash_q})")
        return f"rejected order {o.id}"
    note = "filled at open" if q == o.qty else f"reduced {o.qty:g}->{q:g} by cap/cash check at open"
    ledger.record_fill(o, skey, q, open_px, price, c.commission_eur, note)
    return f"filled BUY {q:g} @ open {open_px:.2f}"
