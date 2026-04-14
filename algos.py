"""AutoBot algorithm wrapper module.

This file exposes the package-level algorithm modules under the legacy
root module name for backward compatibility with existing imports.
"""

from autobot.constants import REST_BASE, WS_BASE, HISTORY_LIMIT, MAX_POINTS
from autobot.data_types import Position, ClosedTrade
from autobot.indicators import ema, rsi, macd, atr, crossover_up, crossover_down
from autobot.vsa_profile import (
    compute_vsa,
    build_volume_profile_from_candles,
    build_vrvp,
    build_session_volume_profile,
    add_profile_levels,
)
from autobot.orderbook import compute_order_book_features, fetch_order_book, OrderBookHeatmapAccumulator
from autobot.fetchers import (
    fetch_klines,
    fetch_agg_trades,
    fetch_futures_symbols,
    fetch_futures_24h_tickers,
    scan_futures_movers,
    build_footprint_from_agg_trades,
    BinanceFeed,
    BinanceDepthFeed,
)
from autobot.strategy import compute_indicators, score_row

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
    "fetch_order_book",
    "OrderBookHeatmapAccumulator",
    "fetch_klines",
    "fetch_agg_trades",
    "fetch_futures_symbols",
    "fetch_futures_24h_tickers",
    "scan_futures_movers",
    "build_footprint_from_agg_trades",
    "BinanceFeed",
    "BinanceDepthFeed",
    "compute_indicators",
    "score_row",
]
