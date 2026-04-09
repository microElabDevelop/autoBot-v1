"""Data classes for position, trade, and signal state."""

from dataclasses import dataclass, field
from typing import Dict, List, Any

import pandas as pd


@dataclass
class Position:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    capital_usdt: float
    leverage: float
    notional_usdt: float
    qty: float
    entry_fee: float
    bars_held: int = 0
    source: str = "auto"
    score_at_entry: float = 0.0
    entry_signals: List[str] = field(default_factory=list)
    entry_snapshot: Dict[str, List[str]] = field(default_factory=lambda: {
        "true_signals": [],
        "false_signals": [],
    })


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    source: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    capital_usdt: float
    leverage: float
    qty: float
    gross_pnl: float
    fees: float
    net_pnl: float
    roi_pct: float
    exit_reason: str
    hold_bars: int
    score_at_entry: float = 0.0
    entry_signals: List[str] = field(default_factory=list)
