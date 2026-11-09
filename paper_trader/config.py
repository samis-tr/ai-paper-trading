"""Static, owner-controlled configuration. Changing these values is a code change, not an LLM decision."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB = Path(os.environ.get("PAPER_DB", DATA_DIR / "ledger.db"))  # PAPER_DB used only for demos

# Marker for generated test prices. The live paper cycle refuses them by default.
SYNTHETIC_SOURCE = "SYNTHETIC-TEST-ONLY"


@dataclass(frozen=True)
class RiskPolicy:
    initial_equity_eur: float = 1000.0
    max_position_weight: float = 0.25      # max 25% of equity in the ETF
    daily_loss_limit: float = 0.01         # 1% equity loss vs previous close -> disable new trades
    max_drawdown_limit: float = 0.03       # 3% from peak -> HALT
    long_only: bool = True                 # informational; enforced in code unconditionally


@dataclass(frozen=True)
class CostModel:
    commission_eur: float = 1.00           # fixed per order (low-cost German broker assumption)
    half_spread: float = 0.0005            # 0.05% paid on each side
    slippage: float = 0.0005               # 0.05% extra adverse move per fill
    fractional_shares: bool = False        # real ETF on XETRA trades in whole shares

    def buy_price(self, open_price: float) -> float:
        return open_price * (1 + self.half_spread + self.slippage)

    def sell_price(self, open_price: float) -> float:
        return open_price * (1 - self.half_spread - self.slippage)


@dataclass(frozen=True)
class StrategyParams:
    """Fixed ex-ante parameters (not optimised on history)."""
    kind: str = "sma"                      # "sma" | "hold" (benchmark)
    sma_window: int = 200
    band: float = 0.01                     # +/-1% hysteresis around the SMA
    rebalance_threshold: float = 0.05      # only trade if weight drifts > 5 pts
    hold_weight: float = 0.25              # used only for kind == "hold"


@dataclass(frozen=True)
class DataPolicy:
    max_abs_daily_move: float = 0.15       # >15% close-to-close move is treated as suspect -> stop
    close_buffer_hours: float = 3.0        # bar for session S expected only 3h after S closes


@dataclass(frozen=True)
class Settings:
    risk: RiskPolicy = field(default_factory=RiskPolicy)
    costs: CostModel = field(default_factory=CostModel)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    data: DataPolicy = field(default_factory=DataPolicy)
    isin: str = "IE00BK5BQT80"
