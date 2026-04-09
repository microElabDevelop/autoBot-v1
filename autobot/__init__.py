"""AutoBot core package.

This package contains the main trading algorithm modules, helpers, and
utility exports used by the application.
"""

from .constants import REST_BASE, WS_BASE, HISTORY_LIMIT, MAX_POINTS
from .data_types import Position, ClosedTrade
from .indicators import ema, rsi, macd, atr, crossover_up, crossover_down
from .vsa_profile import (
    compute_vsa,
    build_volume_profile_from_candles,
    build_vrvp,
    build_session_volume_profile,
    add_profile_levels,
)
from .orderbook import compute_order_book_features, OrderBookHeatmapAccumulator
from .fetchers import (
    fetch_klines,
    fetch_agg_trades,
    build_footprint_from_agg_trades,
    BinanceFeed,
    BinanceDepthFeed,
)
from .orderbook import fetch_order_book
from .strategy import compute_indicators, score_row

__all__ = [
    "REST_BASE",
    "WS_BASE",
    "HISTORY_LIMIT",
    "MAX_POINTS",
    "Position",
    "ClosedTrade",
    "ema",
    "rsi",
    "macd",
    "atr",
    "crossover_up",
    "crossover_down",
    "compute_vsa",
    "build_volume_profile_from_candles",
    "build_vrvp",
    "build_session_volume_profile",
    "add_profile_levels",
    "compute_order_book_features",
    "OrderBookHeatmapAccumulator",
    "fetch_klines",
    "fetch_agg_trades",
    "build_footprint_from_agg_trades",
    "fetch_order_book",
    "BinanceFeed",
    "BinanceDepthFeed",
    "compute_indicators",
    "score_row",
]
