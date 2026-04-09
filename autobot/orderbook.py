"""Order book and book heatmap helpers."""

import time
from typing import Dict, Any, List

import requests

from .constants import REST_BASE


def fetch_order_book(symbol: str, limit: int = 100) -> Dict[str, Any]:
    r = requests.get(
        f"{REST_BASE}/fapi/v1/depth",
        params={"symbol": symbol.upper(), "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def compute_order_book_features(symbol: str, levels: int = 20) -> Dict[str, Any]:
    try:
        ob = fetch_order_book(symbol, limit=max(50, levels))
        bids = [(float(p), float(q)) for p, q in ob.get("bids", [])[:levels]]
        asks = [(float(p), float(q)) for p, q in ob.get("asks", [])[:levels]]

        if not bids or not asks:
            raise ValueError("empty book")

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0
        spread_bps = (spread / mid) * 10000.0 if mid > 0 else None

        bid_vol_top = sum(q for _, q in bids)
        ask_vol_top = sum(q for _, q in asks)
        denom = max(1e-12, bid_vol_top + ask_vol_top)
        imbalance = (bid_vol_top - ask_vol_top) / denom

        bid1 = bids[0][1]
        ask1 = asks[0][1]
        microprice = ((best_ask * bid1) + (best_bid * ask1)) / max(1e-12, bid1 + ask1)

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_bps": spread_bps,
            "bid_vol_top": bid_vol_top,
            "ask_vol_top": ask_vol_top,
            "book_imbalance": imbalance,
            "microprice": microprice,
        }
    except Exception:
        return {
            "best_bid": None,
            "best_ask": None,
            "spread": None,
            "spread_bps": None,
            "bid_vol_top": 0.0,
            "ask_vol_top": 0.0,
            "book_imbalance": 0.0,
            "microprice": None,
        }


class OrderBookHeatmapAccumulator:
    def __init__(self, max_snapshots: int = 250):
        self.max_snapshots = max_snapshots
        self.snapshots: List[Dict[str, Any]] = []
        self.heat: Dict[float, float] = {}

    def update_from_depth(self, bids: List[List[str]], asks: List[List[str]], price_round: int = 2):
        snap = {
            "ts": time.time(),
            "bids": [(round(float(p), price_round), float(q)) for p, q in bids],
            "asks": [(round(float(p), price_round), float(q)) for p, q in asks],
        }
        self.snapshots.append(snap)
        if len(self.snapshots) > self.max_snapshots:
            self.snapshots.pop(0)

        for p, q in snap["bids"]:
            self.heat[p] = self.heat.get(p, 0.0) + q
        for p, q in snap["asks"]:
            self.heat[p] = self.heat.get(p, 0.0) + q

    def top_heat_levels(self, n: int = 20):
        return sorted(self.heat.items(), key=lambda x: x[1], reverse=True)[:n]
