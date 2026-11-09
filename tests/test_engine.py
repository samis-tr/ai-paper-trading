import numpy as np
import pytest

from conftest import bars_from_closes
from paper_trader.backtest import simulate
from paper_trader.data import DataError
from paper_trader.engine import run_cycle
from paper_trader.ledger import ACTIVE, DISABLED, HALTED, RESUME_PHRASE


def run_all(led, bars, cal, s, upto=None):
    res = []
    for i in range(upto or len(bars)):
        res.append(run_cycle(led, bars.iloc[: i + 1], cal, s, validate_data=False))
    return res


def uptrend(cal, n=230, extra=None):
    sess = cal.sessions("2021-01-04", "2022-12-30")[: n + len(extra or [])]
    closes = list(np.linspace(80, 110, n)) + list(extra or [])
    return bars_from_closes(sess, closes)


def test_synthetic_refused_in_live_cycle(led, rbars, cal, settings):
    with pytest.raises(DataError, match="Synthetic"):
        run_cycle(led, rbars, cal, settings)


def test_no_look_ahead(rbars, cal, settings):
    a, _ = simulate(rbars.iloc[:500], cal, settings)
    b, _ = simulate(rbars.iloc[:700], cal, settings)
    a, b = a.drop(columns="created_at"), b.drop(columns="created_at")
    assert a.equals(b.iloc[:500])
    assert len(b) == 700


def test_next_session_execution_at_open_with_costs(led, cal, settings):
    bars = uptrend(cal)
    run_all(led, bars, cal, settings)
    f = led.fills()[0]
    decision_i = settings.strategy.sma_window - 1
    exec_session = bars.index[decision_i + 1]
    assert f["session"] == exec_session.date().isoformat()
    assert f["ref_open"] == pytest.approx(bars["open"].iloc[decision_i + 1])
    assert f["price"] == pytest.approx(f["ref_open"] * 1.001)
    assert f["commission"] == 1.0


def test_position_cap_25pct_at_every_fill(rbars, cal, settings):
    snaps, led = simulate(rbars, cal, settings)
    for f in led.fills():
        snap = snaps.loc[f["session"]]
        assert snap["qty"] * f["ref_open"] <= 0.25 * (snap["cash"] + snap["qty"] * f["ref_open"]) + 1e-6
    assert led.position() >= 0


def test_long_only_sell_limited_and_rejected(led, cal, settings):
    bars = uptrend(cal, 30)
    led.place_order("x:1", settings.isin, "SELL", 5, "2021-01-04", bars.index[1].date().isoformat(), "test")
    run_all(led, bars, cal, settings, upto=2)
    o = led.db.execute("SELECT status FROM orders").fetchone()[0]
    assert o == "REJECTED" and led.position() == 0


def test_duplicate_orders_and_idempotent_rerun(led, cal, settings):
    assert led.place_order("k", settings.isin, "BUY", 1, "2021-01-04", "2021-01-05", "t") is not None
    assert led.place_order("k", settings.isin, "BUY", 1, "2021-01-04", "2021-01-05", "t") is None
    bars = uptrend(cal, 5)
    run_cycle(led, bars, cal, settings, validate_data=False)
    r = run_cycle(led, bars, cal, settings, validate_data=False)
    assert "already processed" in r.actions[0]
    assert len(led.snapshots()) == 1


def test_daily_loss_disables_new_trades(led, cal, settings):
    bars = uptrend(cal, 230, extra=[110 * 0.94])          # -6% day while ~22% invested
    run_all(led, bars, cal, settings)
    assert led.state() == DISABLED
    assert all(o.status != "PENDING" for o in led.pending_orders())
    n_orders = led.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    bars2 = uptrend(cal, 230, extra=[110 * 0.94, 115, 120])
    for i in range(len(bars), len(bars2)):
        run_cycle(led, bars2.iloc[: i + 1], cal, settings, validate_data=False)
    assert led.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == n_orders


def test_drawdown_halts_and_gap_risk(led, cal, settings):
    # gap down open far below previous close: fills/marks at the real open, loss can exceed the limits
    drops = [110 * 0.92, 110 * 0.92 * 0.92, 110 * 0.92 ** 3]
    bars = uptrend(cal, 230, extra=drops)
    res = run_all(led, bars, cal, settings)
    assert led.state() == HALTED
    assert any("HALT" in a for x in res for a in x.actions)
    n_orders = led.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    # after HALT: still monitored (snapshots continue) but no orders are ever placed
    bars2 = uptrend(cal, 230, extra=drops + [95, 110, 125])
    for i in range(len(bars), len(bars2)):
        r = run_cycle(led, bars2.iloc[: i + 1], cal, settings, validate_data=False)
        assert r.state == HALTED
    assert led.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == n_orders
    assert len(led.snapshots()) == len(bars2)


def test_halt_resume_requires_phrase(led):
    led.set_state(HALTED, "test")
    with pytest.raises(PermissionError):
        led.set_state(ACTIVE, "agent tries to clear")
    with pytest.raises(PermissionError):
        led.resume("yes please", "agent")
    led.resume(RESUME_PHRASE, "owner")
    assert led.state() == ACTIVE


def test_missed_execution_session_cancels_order(led, cal, settings):
    bars = uptrend(cal, 10)
    led.place_order("m", settings.isin, "BUY", 1, bars.index[0].date().isoformat(),
                    bars.index[1].date().isoformat(), "t")
    run_cycle(led, bars.iloc[:1], cal, settings, validate_data=False)
    run_cycle(led, bars.iloc[:3], cal, settings, validate_data=False)  # session 1 missed (outage)
    st, reason = led.db.execute("SELECT status, reason FROM orders WHERE idem_key='m'").fetchone()
    assert st == "CANCELLED" and "missed" in reason
    assert led.fills() == []
