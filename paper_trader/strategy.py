"""Deterministic baseline strategy. Uses only closes up to and including the decision session."""
from __future__ import annotations

import pandas as pd

from .config import StrategyParams


def target_weight(closes: pd.Series, currently_invested: bool, p: StrategyParams, max_weight: float) -> float:
    if p.kind == "hold":
        return min(p.hold_weight, max_weight)
    if p.kind != "sma":
        raise ValueError(f"Unknown strategy {p.kind}")
    if len(closes) < p.sma_window:
        return 0.0  # warm-up: stay in cash
    sma = float(closes.iloc[-p.sma_window:].mean())
    last = float(closes.iloc[-1])
    if currently_invested:
        return 0.0 if last < sma * (1 - p.band) else max_weight
    return max_weight if last > sma * (1 + p.band) else 0.0
