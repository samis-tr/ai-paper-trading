from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest

from paper_trader.config import DataPolicy
from paper_trader.data import DataError, fetch_eodhd, validate
from paper_trader.registry import RegistryError, provider_symbol

P = DataPolicy()


def test_valid_series_passes(rbars, cal):
    assert validate(rbars, cal, None, P) == rbars.index[-1]


def test_duplicate_dates(rbars, cal):
    bad = pd.concat([rbars, rbars.iloc[[-1]]])
    with pytest.raises(DataError, match="Duplicate"):
        validate(bad, cal, None, P)


def test_missing_session_is_not_filled(rbars, cal):
    bad = rbars.drop(rbars.index[100])
    with pytest.raises(DataError, match="Missing sessions"):
        validate(bad, cal, None, P)


def test_bar_on_holiday_or_weekend(rbars, cal):
    bad = rbars.copy()
    bad.loc[pd.Timestamp("2020-12-25")] = bad.iloc[0]  # Christmas: XETRA closed
    with pytest.raises(DataError, match="non-trading"):
        validate(bad.sort_index(), cal, None, P)


@pytest.mark.parametrize("col,val,msg", [("high", 1.0, "Inconsistent"), ("close", -5.0, "Non-positive"),
                                         ("open", float("nan"), "Missing OHLC")])
def test_inconsistent_values(rbars, cal, col, val, msg):
    bad = rbars.copy()
    bad.iloc[50, bad.columns.get_loc(col)] = val
    with pytest.raises(DataError, match=msg):
        validate(bad, cal, None, P)


def test_suspicious_jump_stops(rbars, cal):
    bad = rbars.copy()
    i = 200
    for c in ["open", "high", "low", "close"]:
        bad.iloc[i:, bad.columns.get_loc(c)] *= 1.25
    with pytest.raises(DataError, match="Suspicious"):
        validate(bad, cal, None, P)


def test_stale_data_stops(rbars, cal):
    now = datetime(2023, 3, 1, 20, 0, tzinfo=timezone.utc)  # data ends 2022-12-30
    with pytest.raises(DataError, match="Stale"):
        validate(rbars, cal, now, P)


def test_fresh_data_ok_and_future_bar_rejected(rbars, cal):
    # 2022-12-30 is a Friday session; Monday 2023-01-02 early morning -> expected latest = 2022-12-30
    assert validate(rbars, cal, datetime(2023, 1, 2, 6, tzinfo=timezone.utc), P) == pd.Timestamp("2022-12-30")
    # Same day, before close + buffer -> bar should not exist yet
    with pytest.raises(DataError, match="future"):
        validate(rbars, cal, datetime(2022, 12, 30, 12, tzinfo=timezone.utc), P)


def test_trading_calendar_failure(cal):
    old = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1],
                        "source": ["x"]}, index=pd.DatetimeIndex(["1990-01-02"]))
    with pytest.raises(DataError, match="calendar"):
        validate(old, cal, None, P)


def test_network_outage_raises_without_leaking_token():
    def boom(request):
        raise httpx.ConnectError("down", request=request)
    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(DataError) as e:
        fetch_eodhd("VWCE.XETRA", "SECRET123", "2020-01-01", client=client)
    assert "SECRET123" not in str(e.value)


def test_provider_http_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(DataError, match="unavailable"):
        fetch_eodhd("VWCE.XETRA", "t", "2020-01-01", client=client)


def test_unverified_symbol_is_refused(tmp_path):
    with pytest.raises(RegistryError, match="NOT verified"):
        provider_symbol("IE00BK5BQT80", "eodhd", verified_path=tmp_path / "none.json")
    with pytest.raises(RegistryError):
        provider_symbol("US0000000000", "eodhd")
