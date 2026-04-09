"""Data fetchers for Binance klines, agg trades, and live websocket feeds."""

import json
import queue
import threading
import time
from typing import Dict, Any, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from websocket import WebSocketApp

from .constants import REST_BASE, WS_BASE
from .strategy import compute_indicators


def retry_session(retries: int = 3, backoff_factor: float = 0.5, status_forcelist=None):
    if status_forcelist is None:
        status_forcelist = [429, 500, 502, 503, 504]
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    session = retry_session()
    r = session.get(
        f"{REST_BASE}/fapi/v1/klines",
        params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json()

    data = []
    for k in rows:
        data.append({
            "open_time": pd.to_datetime(k[0], unit="ms", utc=True),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": pd.to_datetime(k[6], unit="ms", utc=True),
            "is_closed": True,
        })

    df = pd.DataFrame(data)
    return compute_indicators(df, symbol=symbol)


def fetch_agg_trades(symbol: str, limit: int = 220) -> pd.DataFrame:
    session = retry_session()
    r = session.get(
        f"{REST_BASE}/fapi/v1/aggTrades",
        params={"symbol": symbol.upper(), "limit": limit},
        timeout=12,
    )
    r.raise_for_status()
    rows = r.json()

    data = []
    for x in rows:
        price = float(x["p"])
        qty = float(x["q"])
        is_sell_aggressor = bool(x["m"])
        data.append({
            "trade_id": int(x["a"]),
            "price": price,
            "qty": qty,
            "quote_qty": price * qty,
            "time": pd.to_datetime(x["T"], unit="ms", utc=True),
            "is_sell_aggressor": is_sell_aggressor,
            "is_buy_aggressor": not is_sell_aggressor,
        })
    return pd.DataFrame(data)


def build_footprint_from_agg_trades(trades_df: pd.DataFrame, price_step: Optional[float] = None) -> Dict[str, Any]:
    if trades_df.empty:
        return {
            "levels": [], "buy_vol": [], "sell_vol": [], "delta": [],
            "total_buy": 0.0, "total_sell": 0.0, "delta_total": 0.0,
            "imbalance_ratio": 0.0,
        }

    prices = trades_df["price"].astype(float)
    if price_step is None:
        median_price = float(prices.median())
        if median_price >= 50000:
            price_step = 5.0
        elif median_price >= 5000:
            price_step = 1.0
        elif median_price >= 500:
            price_step = 0.1
        else:
            price_step = 0.01

    def bucket_price(p: float) -> float:
        return round(round(p / price_step) * price_step, 8)

    grouped: Dict[float, Dict[str, float]] = {}
    for _, row in trades_df.iterrows():
        level = bucket_price(float(row["price"]))
        qty = float(row["qty"])
        if level not in grouped:
            grouped[level] = {"buy": 0.0, "sell": 0.0}
        if bool(row["is_buy_aggressor"]):
            grouped[level]["buy"] += qty
        else:
            grouped[level]["sell"] += qty

    levels = sorted(grouped.keys())
    buy_vol = [grouped[p]["buy"] for p in levels]
    sell_vol = [grouped[p]["sell"] for p in levels]
    delta = [b - s for b, s in zip(buy_vol, sell_vol)]

    total_buy = float(sum(buy_vol))
    total_sell = float(sum(sell_vol))
    delta_total = total_buy - total_sell
    denom = max(1e-12, total_buy + total_sell)

    return {
        "levels": levels,
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "delta": delta,
        "total_buy": total_buy,
        "total_sell": total_sell,
        "delta_total": delta_total,
        "imbalance_ratio": delta_total / denom,
        "price_step": price_step,
    }


class BinanceFeed(threading.Thread):
    def __init__(self, symbol: str, interval: str, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self.symbol = symbol.lower()
        self.interval = interval
        self.out_queue = out_queue
        self.ws_app = None
        self.stopped = False

    def run(self):
        url = f"{WS_BASE}/{self.symbol}@kline_{self.interval}"

        def on_message(ws, message):
            if self.stopped:
                return
            try:
                obj = json.loads(message)
                k = obj.get("k")
                if not k:
                    return
                self.out_queue.put({
                    "symbol": self.symbol.upper(),
                    "interval": self.interval,
                    "open_time": pd.to_datetime(k["t"], unit="ms", utc=True),
                    "open": float(k["o"]),
                    "high": float(k["h"]),
                    "low": float(k["l"]),
                    "close": float(k["c"]),
                    "volume": float(k["v"]),
                    "close_time": pd.to_datetime(k["T"], unit="ms", utc=True),
                    "is_closed": bool(k["x"]),
                })
            except Exception as e:
                self.out_queue.put({"type": "error", "message": str(e)})

        def on_error(ws, error):
            self.out_queue.put({"type": "error", "message": str(error)})

        self.ws_app = WebSocketApp(url, on_message=on_message, on_error=on_error)
        self.ws_app.run_forever(ping_interval=120, ping_timeout=30)

    def stop(self):
        self.stopped = True
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass


class BinanceDepthFeed(threading.Thread):
    def __init__(self, symbol: str, out_queue: queue.Queue, levels: int = 20):
        super().__init__(daemon=True)
        self.symbol = symbol.lower()
        self.out_queue = out_queue
        self.levels = levels
        self.ws_app = None
        self.stopped = False

    def run(self):
        url = f"{WS_BASE}/{self.symbol}@depth{self.levels}@100ms"

        def on_message(ws, message):
            if self.stopped:
                return
            try:
                obj = json.loads(message)
                self.out_queue.put({
                    "type": "depth",
                    "symbol": self.symbol.upper(),
                    "bids": obj.get("b", []),
                    "asks": obj.get("a", []),
                    "ts": obj.get("T", int(time.time() * 1000)),
                })
            except Exception as e:
                self.out_queue.put({"type": "error", "message": str(e)})

        def on_error(ws, error):
            self.out_queue.put({"type": "error", "message": str(error)})

        self.ws_app = WebSocketApp(url, on_message=on_message, on_error=on_error)
        self.ws_app.run_forever(ping_interval=120, ping_timeout=30)

    def stop(self):
        self.stopped = True
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
