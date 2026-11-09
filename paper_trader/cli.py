"""Owner command line. Usage: python -m paper_trader.cli <command> [...]"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from .backtest import run_comparison
from .calendar_xetra import CalendarError, TradingCalendar
from .config import DATA_DIR, DEFAULT_DB, PROJECT_ROOT, SYNTHETIC_SOURCE, Settings
from .data import DataError, fetch_eodhd, load_csv, validate
from .engine import run_cycle
from .jobs import Job, JobRejected, emergency_halt, parse_job
from .ledger import HALTED, Ledger
from .notifier import daily_summary, send
from .registry import RegistryError, get_instrument, mark_verified, provider_symbol

BARS = DATA_DIR / "bars.csv"
REPORTS = PROJECT_ROOT / "reports"


def load_env(path: Path = PROJECT_ROOT / ".env") -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def ledger() -> Ledger:
    led = Ledger(DEFAULT_DB)
    s = Settings()
    led.init(s.risk.initial_equity_eur, s.isin)
    return led


def load_bars(path: Path = BARS) -> pd.DataFrame:
    if not path.exists():
        raise DataError(f"No bar store at {path}. Run `fetch` or `import-csv` first.")
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df


def save_bars(df: pd.DataFrame) -> None:
    BARS.parent.mkdir(parents=True, exist_ok=True)
    df.reset_index().to_csv(BARS, index=False, date_format="%Y-%m-%d")


def notify(level: str, msg: str) -> None:
    send(f"🚨 [{level}] {msg}")


def cmd_run_daily(a) -> int:
    led, s, cal = ledger(), Settings(), TradingCalendar()
    now = datetime.fromisoformat(a.now) if getattr(a, "now", None) else datetime.now(timezone.utc)
    try:
        res = run_cycle(led, load_bars(), cal, s, now=now, allow_synthetic=getattr(a, "allow_synthetic", False),
                        notify=notify)
    except (DataError, CalendarError) as e:
        led.event("ERROR", "DATA_STOP", str(e))
        notify("ERROR", f"Cycle stopped, no trading: {e}")
        print(f"STOPPED: {e}")
        return 2
    print(json.dumps(res.__dict__, indent=2, ensure_ascii=False))
    return 0


def cmd_job(a) -> int:
    led = ledger()
    try:
        job = parse_job(a.name, led)
    except JobRejected as e:
        print(f"REJECTED: {e}")
        return 3
    if job is Job.RUN_DAILY:
        return cmd_run_daily(argparse.Namespace())
    if job is Job.GENERATE_REPORT:
        print(daily_summary(led))
    elif job is Job.STATUS:
        cmd_status(a)
    elif job is Job.EMERGENCY_HALT:
        emergency_halt(led, "agent-job")
        notify("CRITICAL", "Emergency stop triggered via agent job")
        print("HALTED")
    return 0


def cmd_status(_a) -> int:
    led = ledger()
    snap = led.last_snapshot()
    print(json.dumps({"control": led.control(), "position_qty": led.position(), "cash": round(led.cash(), 2),
                      "last_snapshot": snap, "event_chain_ok": led.verify_chain()}, indent=2, ensure_ascii=False))
    return 0


def cmd_halt(a) -> int:
    emergency_halt(ledger(), a.who)
    notify("CRITICAL", f"Emergency stop by {a.who}")
    print("HALTED. Resume requires: resume --who NAME (and the approval phrase).")
    return 0


def cmd_resume(a) -> int:
    phrase = input("Type the approval phrase exactly: ")
    try:
        ledger().resume(phrase, a.who)
    except PermissionError as e:
        print(f"DENIED: {e}")
        return 4
    print("Paper trading resumed.")
    return 0


def cmd_import_csv(a) -> int:
    df = load_csv(Path(a.path), a.source)
    validate(df, TradingCalendar(), None, Settings().data)
    save_bars(df)
    print(f"Imported {len(df)} bars {df.index[0].date()}..{df.index[-1].date()} source={a.source}")
    return 0


def cmd_fetch(a) -> int:
    token = os.environ.get("EODHD_API_TOKEN")
    if not token:
        print("EODHD_API_TOKEN missing in .env")
        return 2
    s = Settings()
    sym = provider_symbol(s.isin, "eodhd")
    df = fetch_eodhd(sym, token, a.start)
    validate(df, TradingCalendar(), None, s.data)
    save_bars(df)
    print(f"Fetched {len(df)} bars {df.index[0].date()}..{df.index[-1].date()} from {sym}")
    return 0


def cmd_verify_provider(_a) -> int:
    """Checks EODHD's metadata for the symbol against the registry (ISIN, exchange, currency)."""
    token = os.environ.get("EODHD_API_TOKEN")
    if not token:
        print("EODHD_API_TOKEN missing in .env")
        return 2
    s = Settings()
    inst = get_instrument(s.isin)
    reg = json.loads((PROJECT_ROOT / "config" / "instruments.json").read_text(encoding="utf-8"))
    sym = reg[s.isin]["provider_symbols"]["eodhd"]["symbol"]
    try:
        r = httpx.get(f"https://eodhd.com/api/fundamentals/{sym}", params={"api_token": token, "fmt": "json",
                      "filter": "General"}, timeout=20)
        r.raise_for_status()
        g = r.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"Could not verify ({type(e).__name__}). Plan may not include metadata; verify manually.")
        return 2
    checks = {"ISIN": (g.get("ISIN"), inst.isin), "CurrencyCode": (g.get("CurrencyCode"), inst.currency),
              "Exchange": (g.get("Exchange"), "XETRA")}
    for k, (got, want) in checks.items():
        print(f"{k}: provider={got!r} expected={want!r} {'OK' if got == want else 'MISMATCH'}")
    if all(got == want for got, want in checks.values()):
        mark_verified(s.isin, "eodhd", sym)
        print(f"VERIFIED: {sym} <-> {s.isin}")
        return 0
    return 5


def cmd_backtest(a) -> int:
    bars = load_csv(Path(a.csv), a.source) if a.csv else load_bars()
    res = run_comparison(bars, TradingCalendar())
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORTS / f"backtest_{stamp}.json"
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    synthetic = SYNTHETIC_SOURCE in res["data_source"]
    print(("⚠️ SYNTHETIC TEST DATA — results say NOTHING about real performance\n" if synthetic else "")
          + f"data {res['first_bar']}..{res['last_bar']} source={res['data_source']}")
    print(f"{'variant':38} {'final€':>9} {'return':>8} {'maxDD':>7} {'sharpe':>7} {'fees€':>7} halt")
    for k, v in res["variants"].items():
        f = v["full"]
        print(f"{k:38} {v['final_equity']:9.2f} {f['total_return']:8.2%} {f['max_drawdown']:7.2%} "
              f"{f.get('sharpe_rf0', 0):7.2f} {v.get('costs', {}).get('commission', 0):7.2f} {v.get('first_halt')}")
    print(f"report: {out}")
    return 0


def cmd_summary(a) -> int:
    text = daily_summary(ledger())
    print(text)
    if a.send:
        print("telegram:", send(text))
    return 0


def cmd_add_cost(a) -> int:
    ledger().add_actual_cost(a.date, a.vendor, float(a.amount), a.note)
    print("recorded")
    return 0


def cmd_make_synthetic(a) -> int:
    """Deterministic fake prices for TESTING ONLY. Tagged and refused by the live cycle."""
    cal = TradingCalendar()
    sess = cal.sessions(a.start, a.end)
    rng = np.random.default_rng(a.seed)
    r = rng.normal(0.0003, 0.009, len(sess))
    close = 80 * np.exp(np.cumsum(r))
    open_ = close / np.exp(r) * np.exp(rng.normal(0, 0.003, len(sess)))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, len(sess))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, len(sess))))
    df = pd.DataFrame({"date": sess, "open": open_.round(4), "high": high.round(4), "low": low.round(4),
                       "close": close.round(4), "volume": rng.integers(50_000, 300_000, len(sess))})
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, date_format="%Y-%m-%d")
    print(f"wrote {len(df)} SYNTHETIC bars to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()
    p = argparse.ArgumentParser(prog="paper_trader")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(f=cmd_status)
    sub.add_parser("status").set_defaults(f=cmd_status)
    x = sub.add_parser("run-daily"); x.add_argument("--now"); x.add_argument("--allow-synthetic", action="store_true")
    x.set_defaults(f=cmd_run_daily)
    x = sub.add_parser("job"); x.add_argument("name"); x.set_defaults(f=cmd_job)
    x = sub.add_parser("halt"); x.add_argument("--who", required=True); x.set_defaults(f=cmd_halt)
    x = sub.add_parser("resume"); x.add_argument("--who", required=True); x.set_defaults(f=cmd_resume)
    x = sub.add_parser("import-csv"); x.add_argument("path"); x.add_argument("--source", required=True)
    x.set_defaults(f=cmd_import_csv)
    x = sub.add_parser("fetch"); x.add_argument("--start", default="2019-07-01"); x.set_defaults(f=cmd_fetch)
    sub.add_parser("verify-provider").set_defaults(f=cmd_verify_provider)
    x = sub.add_parser("backtest"); x.add_argument("--csv"); x.add_argument("--source", default=SYNTHETIC_SOURCE)
    x.set_defaults(f=cmd_backtest)
    x = sub.add_parser("summary"); x.add_argument("--send", action="store_true"); x.set_defaults(f=cmd_summary)
    x = sub.add_parser("add-cost"); x.add_argument("date"); x.add_argument("vendor"); x.add_argument("amount")
    x.add_argument("--note", default=""); x.set_defaults(f=cmd_add_cost)
    x = sub.add_parser("make-synthetic"); x.add_argument("--out", default=str(DATA_DIR / "synthetic_bars.csv"))
    x.add_argument("--start", default="2019-07-24"); x.add_argument("--end", default="2026-11-04")
    x.add_argument("--seed", type=int, default=7); x.set_defaults(f=cmd_make_synthetic)
    a = p.parse_args(argv)
    try:
        return a.f(a)
    except (DataError, CalendarError, RegistryError) as e:
        print(f"STOPPED: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
