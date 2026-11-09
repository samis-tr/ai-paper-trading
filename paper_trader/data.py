"""Market data loading and validation. Missing data is never filled with invented prices."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

from .calendar_xetra import CalendarError, TradingCalendar
from .config import DataPolicy

COLUMNS = ["open", "high", "low", "close", "volume"]


class DataError(Exception):
    """Data is stale, missing, inconsistent or unavailable. The engine must stop."""


def bars_from_records(records: list[dict], source: str) -> pd.DataFrame:
    if not records:
        raise DataError("No price records received")
    df = pd.DataFrame(records)
    missing = {"date", *COLUMNS} - set(df.columns)
    if missing:
        raise DataError(f"Missing columns: {sorted(missing)}")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    for c in COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["source"] = source
    return df.set_index("date")[[*COLUMNS, "source"]]


def load_csv(path: Path, source: str) -> pd.DataFrame:
    with open(path, newline="", encoding="utf-8") as f:
        return bars_from_records(list(csv.DictReader(f)), source)


def fetch_eodhd(symbol: str, api_token: str, start: str, timeout: float = 20.0,
                client: httpx.Client | None = None) -> pd.DataFrame:
    """Fetch daily bars. Raw (unadjusted) OHLC is used for execution; VWCE is accumulating."""
    url = f"https://eodhd.com/api/eod/{symbol}"
    params = {"api_token": api_token, "fmt": "json", "from": start, "period": "d"}
    try:
        c = client or httpx.Client(timeout=timeout)
        r = c.get(url, params=params)
        r.raise_for_status()
        rows = r.json()
    except (httpx.HTTPError, ValueError) as e:
        # Never echo the URL (contains the token).
        raise DataError(f"Data provider unavailable: {type(e).__name__}") from None
    if not isinstance(rows, list):
        raise DataError("Unexpected provider payload")
    return bars_from_records(rows, source=f"eodhd:{symbol}")


def validate(bars: pd.DataFrame, cal: TradingCalendar, now: datetime | None,
             policy: DataPolicy, as_of_session: pd.Timestamp | None = None) -> pd.Timestamp:
    """Raise DataError on any problem. Returns the latest session in the data.

    now: if given, the latest bar must equal the expected latest XETRA session (staleness check).
    """
    if bars is None or bars.empty:
        raise DataError("No data")
    if bars.index.has_duplicates:
        raise DataError(f"Duplicate dates: {sorted(set(bars.index[bars.index.duplicated()].date))[:5]}")
    if not bars.index.is_monotonic_increasing:
        raise DataError("Dates not sorted")
    if bars[COLUMNS[:4]].isna().any().any():
        raise DataError("Missing OHLC values")
    if (bars[COLUMNS[:4]] <= 0).any().any():
        raise DataError("Non-positive prices")
    bad = (bars["high"] < bars[["open", "close", "low"]].max(axis=1)) | \
          (bars["low"] > bars[["open", "close", "high"]].min(axis=1))
    if bad.any():
        raise DataError(f"Inconsistent OHLC on {bars.index[bad][0].date()}")
    try:
        expected = cal.sessions(bars.index[0], bars.index[-1])
    except CalendarError as e:
        raise DataError(f"Trading calendar failure: {e}") from e
    extra = bars.index.difference(expected)
    if len(extra):
        raise DataError(f"Bars on non-trading days: {[d.date() for d in extra[:5]]}")
    gaps = expected.difference(bars.index)
    if len(gaps):
        raise DataError(f"Missing sessions: {[d.date() for d in gaps[:5]]} (total {len(gaps)})")
    moves = bars["close"].pct_change().abs()
    if (moves > policy.max_abs_daily_move).any():
        d = moves[moves > policy.max_abs_daily_move].index[0]
        raise DataError(f"Suspicious move >{policy.max_abs_daily_move:.0%} on {d.date()} - manual review required")
    latest = bars.index[-1]
    if now is not None:
        try:
            exp = cal.expected_latest_session(now, policy.close_buffer_hours)
        except CalendarError as e:
            raise DataError(f"Trading calendar failure: {e}") from e
        if latest < exp:
            raise DataError(f"Stale data: latest bar {latest.date()}, expected {exp.date()}")
        if latest > exp:
            raise DataError(f"Bar from the future: {latest.date()} > expected {exp.date()}")
    if as_of_session is not None and latest != as_of_session:
        raise DataError(f"Latest bar {latest.date()} != cycle session {as_of_session.date()}")
    return latest
