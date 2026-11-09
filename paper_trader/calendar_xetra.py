"""Exchange calendar wrapper. Any calendar failure is a hard stop."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import exchange_calendars as xcals
import pandas as pd


class CalendarError(Exception):
    pass


class TradingCalendar:
    def __init__(self, code: str = "XETR"):
        try:
            self.cal = xcals.get_calendar(code)
        except Exception as e:  # pragma: no cover - library failure path
            raise CalendarError(f"Cannot load calendar {code}: {e}") from e
        self.code = code

    def _check_range(self, ts: pd.Timestamp) -> None:
        if ts < self.cal.first_session or ts > self.cal.last_session:
            raise CalendarError(f"{ts.date()} outside calendar range "
                                f"{self.cal.first_session.date()}..{self.cal.last_session.date()}")

    def is_session(self, d) -> bool:
        ts = pd.Timestamp(d).normalize()
        self._check_range(ts)
        return bool(self.cal.is_session(ts))

    def sessions(self, start, end) -> pd.DatetimeIndex:
        s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        self._check_range(s)
        self._check_range(e)
        return self.cal.sessions_in_range(s, e)

    def next_session(self, d) -> pd.Timestamp:
        ts = pd.Timestamp(d).normalize()
        self._check_range(ts)
        nxt = self.cal.next_session(ts) if self.cal.is_session(ts) else self.cal.date_to_session(ts, "next")
        self._check_range(nxt)
        return nxt

    def expected_latest_session(self, now: datetime, buffer_hours: float) -> pd.Timestamp:
        """Most recent session whose daily bar should be available at `now` (UTC aware)."""
        if now.tzinfo is None:
            raise CalendarError("now must be timezone-aware")
        today = pd.Timestamp(now.astimezone(timezone.utc).date())
        self._check_range(today)
        s = self.cal.date_to_session(today, "previous")
        close_utc = self.cal.session_close(s).to_pydatetime()
        if now < close_utc + timedelta(hours=buffer_hours):
            s = self.cal.previous_session(s)
        return s
