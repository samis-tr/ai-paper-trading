"""Fill a SEPARATE demo ledger (data/demo_synthetic.db) by replaying synthetic bars through the real engine,
so the dashboard can be previewed. Never touches data/ledger.db."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_trader.calendar_xetra import TradingCalendar  # noqa: E402
from paper_trader.config import Settings  # noqa: E402
from paper_trader.data import load_csv, validate  # noqa: E402
from paper_trader.config import SYNTHETIC_SOURCE  # noqa: E402
from paper_trader.engine import run_cycle  # noqa: E402
from paper_trader.ledger import Ledger  # noqa: E402

db = ROOT / "data" / "demo_synthetic.db"
if db.exists():
    sys.exit(f"{db} exists; delete it manually to rebuild the demo")
s, cal = Settings(), TradingCalendar()
bars = load_csv(ROOT / "data" / "synthetic_bars.csv", SYNTHETIC_SOURCE)
validate(bars, cal, None, s.data)
led = Ledger(db)
led.init(s.risk.initial_equity_eur, s.isin)
for i in range(len(bars)):
    run_cycle(led, bars.iloc[: i + 1], cal, s, validate_data=False)
led.add_actual_cost("2026-11-05", "demo-entry", 0.0, "example row: real invoices go here")
print(led.control(), led.last_snapshot())
