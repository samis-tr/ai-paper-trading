"""Instrument registry keyed by ISIN. Provider symbols are never guessed."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT

REGISTRY_PATH = PROJECT_ROOT / "config" / "instruments.json"
VERIFIED_PATH = PROJECT_ROOT / "data" / "verified_symbols.json"  # written only by verify-provider


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class Instrument:
    isin: str
    name: str
    ticker: str
    exchange_mic: str
    calendar: str
    currency: str


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def get_instrument(isin: str, path: Path = REGISTRY_PATH) -> Instrument:
    reg = _load(path)
    if isin not in reg:
        raise RegistryError(f"ISIN {isin} not in registry")
    r = reg[isin]
    return Instrument(isin, r["name"], r["ticker"], r["exchange_mic"], r["calendar"], r["currency"])


def provider_symbol(isin: str, provider: str, path: Path = REGISTRY_PATH,
                    verified_path: Path = VERIFIED_PATH) -> str:
    """Return the provider symbol only if it has been verified for this ISIN."""
    reg = _load(path)
    entry = reg.get(isin, {}).get("provider_symbols", {}).get(provider)
    if not entry:
        raise RegistryError(f"No {provider} mapping for {isin}")
    verified = _load(verified_path).get(isin, {}).get(provider)
    if not (entry.get("verified") or verified == entry["symbol"]):
        raise RegistryError(
            f"{provider} symbol {entry['symbol']} for {isin} is NOT verified. Run `verify-provider` first.")
    return entry["symbol"]


def mark_verified(isin: str, provider: str, symbol: str, verified_path: Path = VERIFIED_PATH) -> None:
    data = _load(verified_path)
    data.setdefault(isin, {})[provider] = symbol
    verified_path.parent.mkdir(parents=True, exist_ok=True)
    verified_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
