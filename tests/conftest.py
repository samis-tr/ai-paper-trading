import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper_trader.calendar_xetra import TradingCalendar  # noqa: E402
from paper_trader.config import SYNTHETIC_SOURCE, Settings  # noqa: E402
from paper_trader.ledger import Ledger  # noqa: E402


@pytest.fixture(scope="session")
def cal():
    return TradingCalendar()


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def led(settings):
    l = Ledger(":memory:")
    l.init(settings.risk.initial_equity_eur, settings.isin)
    return l


def bars_from_closes(sessions, closes, opens=None) -> pd.DataFrame:
    closes = np.asarray(closes, float)
    opens = np.asarray(opens, float) if opens is not None else np.r_[closes[0], closes[:-1]]
    hi = np.maximum(opens, closes) * 1.001
    lo = np.minimum(opens, closes) * 0.999
    return pd.DataFrame({"open": opens, "high": hi, "low": lo, "close": closes, "volume": 1000,
                         "source": SYNTHETIC_SOURCE}, index=pd.DatetimeIndex(sessions, name="date"))


def random_bars(cal, start="2019-07-24", end="2022-12-30", seed=1) -> pd.DataFrame:
    sess = cal.sessions(start, end)
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0004, 0.009, len(sess))
    close = 80 * np.exp(np.cumsum(r))
    opens = close / np.exp(r) * np.exp(rng.normal(0, 0.003, len(sess)))
    return bars_from_closes(sess, close, opens)


@pytest.fixture
def rbars(cal):
    return random_bars(cal)
