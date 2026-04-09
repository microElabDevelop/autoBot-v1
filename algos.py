import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

import pandas as pd
import requests
from websocket import WebSocketApp

REST_BASE = "https://fapi.binance.com"
WS_BASE = "wss://fstream.binance.com/ws"
HISTORY_LIMIT = 500
MAX_POINTS = 2500


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


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return (100 - (100 / (1 + rs))).fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def crossover_up(a_prev, a_now, b_prev, b_now) -> bool:
    return a_prev <= b_prev and a_now > b_now


def crossover_down(a_prev, a_now, b_prev, b_now) -> bool:
    return a_prev >= b_prev and a_now < b_now


def compute_vsa(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out

    out["bar_spread"] = (out["high"] - out["low"]).clip(lower=1e-12)
    out["body"] = (out["close"] - out["open"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["close_pos"] = (out["close"] - out["low"]) / out["bar_spread"]
    out["body_pct"] = out["body"] / out["bar_spread"]

    out["spread_ma20"] = out["bar_spread"].rolling(20).mean()
    out["vol_ma20"] = out["volume"].rolling(20).mean()
    out["close_ma20"] = out["close"].rolling(20).mean()
    out["close_ma50"] = out["close"].rolling(50).mean()

    out["high_vol"] = out["volume"] > out["vol_ma20"] * 1.8
    out["ultra_high_vol"] = out["volume"] > out["vol_ma20"] * 2.5
    out["low_vol"] = out["volume"] < out["vol_ma20"] * 0.7
    out["wide_spread"] = out["bar_spread"] > out["spread_ma20"] * 1.35
    out["narrow_spread"] = out["bar_spread"] < out["spread_ma20"] * 0.8

    out["up_bar"] = out["close"] > out["open"]
    out["down_bar"] = out["close"] < out["open"]

    out["trend_up_bg"] = (
        (out["close"] > out["close_ma20"]) &
        (out["close_ma20"] > out["close_ma50"])
    )
    out["trend_down_bg"] = (
        (out["close"] < out["close_ma20"]) &
        (out["close_ma20"] < out["close_ma50"])
    )

    out["near_10bar_low"] = out["low"] <= out["low"].rolling(10).min()
    out["near_10bar_high"] = out["high"] >= out["high"].rolling(10).max()

    out["vsa_no_demand_raw"] = (
        out["up_bar"] & out["narrow_spread"] & out["low_vol"] &
        out["trend_down_bg"] & (out["close_pos"] < 0.60)
    )
    out["vsa_no_supply_raw"] = (
        out["down_bar"] & out["narrow_spread"] & out["low_vol"] &
        out["trend_up_bg"] & (out["close_pos"] > 0.40)
    )
    out["vsa_stopping_volume_raw"] = (
        out["down_bar"] & out["high_vol"] & out["wide_spread"] &
        (out["close_pos"] > 0.55) & out["near_10bar_low"]
    )
    out["vsa_shakeout_raw"] = (
        out["down_bar"] & out["ultra_high_vol"] & out["wide_spread"] &
        ((out["lower_wick"] / out["bar_spread"]) > 0.35) &
        (out["close_pos"] > 0.68) & out["near_10bar_low"]
    )
    out["vsa_upthrust_raw"] = (
        out["up_bar"] & out["ultra_high_vol"] & out["wide_spread"] &
        ((out["upper_wick"] / out["bar_spread"]) > 0.35) &
        (out["close_pos"] < 0.55) & out["near_10bar_high"]
    )

    out["vsa_shakeout_confirm"] = (
        out["vsa_shakeout_raw"].shift(1).fillna(False) &
        out["up_bar"] &
        (out["close"] > out["high"].shift(1) * 0.998)
    )
    out["vsa_stopping_volume_confirm"] = (
        out["vsa_stopping_volume_raw"].shift(1).fillna(False) &
        out["up_bar"] &
        (out["close"] >= out["close"].shift(1))
    )
    out["vsa_upthrust_confirm"] = (
        out["vsa_upthrust_raw"].shift(1).fillna(False) &
        out["down_bar"] &
        (out["close"] < out["low"].shift(1) * 1.002)
    )
    out["vsa_no_demand_confirm"] = (
        out["vsa_no_demand_raw"].shift(1).fillna(False) &
        out["down_bar"] &
        (out["close"] < out["close"].shift(1))
    )
    out["vsa_no_supply_confirm"] = (
        out["vsa_no_supply_raw"].shift(1).fillna(False) &
        out["up_bar"] &
        (out["close"] > out["close"].shift(1))
    )

    out["vsa_bullish"] = (
        out["vsa_shakeout_confirm"] |
        out["vsa_stopping_volume_confirm"] |
        out["vsa_no_supply_confirm"]
    )
    out["vsa_bearish"] = (
        out["vsa_upthrust_confirm"] |
        out["vsa_no_demand_confirm"]
    )

    return out


def _safe_step(low: float, high: float, bins: int) -> float:
    return max(1e-9, high - low) / max(1, bins)


def build_volume_profile_from_candles(
    df: pd.DataFrame,
    bins: int = 80,
    lookback: Optional[int] = 300,
) -> Dict[str, Any]:
    if df.empty:
        return {"prices": [], "volumes": [], "poc": None, "vah": None, "val": None, "total_volume": 0.0}

    work = df.copy()
    if lookback is not None and len(work) > lookback:
        work = work.iloc[-lookback:].copy()

    low = float(work["low"].min())
    high = float(work["high"].max())
    step = _safe_step(low, high, bins)

    prices = [low + (i + 0.5) * step for i in range(bins)]
    volumes = [0.0 for _ in range(bins)]

    for _, row in work.iterrows():
        bar_low = float(row["low"])
        bar_high = float(row["high"])
        bar_vol = float(row["volume"])

        if bar_high <= bar_low:
            idx = max(0, min(bins - 1, int((bar_low - low) / step)))
            volumes[idx] += bar_vol
            continue

        touched = []
        total_overlap = 0.0
        for i in range(bins):
            a = low + i * step
            b = a + step
            overlap = max(0.0, min(bar_high, b) - max(bar_low, a))
            if overlap > 0:
                touched.append((i, overlap))
                total_overlap += overlap

        if total_overlap <= 0:
            continue

        for idx, overlap in touched:
            volumes[idx] += bar_vol * (overlap / total_overlap)

    total_volume = float(sum(volumes))
    if total_volume <= 0:
        return {"prices": prices, "volumes": volumes, "poc": None, "vah": None, "val": None, "total_volume": 0.0}

    poc_idx = max(range(len(volumes)), key=lambda i: volumes[i])
    poc = prices[poc_idx]

    ranked = sorted(range(len(volumes)), key=lambda i: volumes[i], reverse=True)
    target = total_volume * 0.70
    running = 0.0
    included = set()
    for idx in ranked:
        included.add(idx)
        running += volumes[idx]
        if running >= target:
            break

    inc_prices = [prices[i] for i in included]
    return {
        "prices": prices,
        "volumes": volumes,
        "poc": poc,
        "vah": max(inc_prices),
        "val": min(inc_prices),
        "total_volume": total_volume,
    }


def build_vrvp(df: pd.DataFrame, bins: int = 80, lookback: int = 300) -> Dict[str, Any]:
    return build_volume_profile_from_candles(df, bins=bins, lookback=lookback)


def build_session_volume_profile(df: pd.DataFrame, bins: int = 60) -> Dict[str, Any]:
    if df.empty:
        return {"prices": [], "volumes": [], "poc": None, "vah": None, "val": None, "session_date": None}

    work = df.copy()
    work["session_date"] = work["open_time"].dt.strftime("%Y-%m-%d")
    last_session = work["session_date"].iloc[-1]
    session_df = work[work["session_date"] == last_session].copy()

    prof = build_volume_profile_from_candles(session_df, bins=bins, lookback=None)
    prof["session_date"] = last_session
    return prof


def add_profile_levels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out

    vrvp = build_vrvp(out, bins=80, lookback=min(300, len(out)))
    session = build_session_volume_profile(out, bins=60)

    out["vrvp_poc"] = vrvp["poc"]
    out["vrvp_vah"] = vrvp["vah"]
    out["vrvp_val"] = vrvp["val"]

    out["session_poc"] = session["poc"]
    out["session_vah"] = session["vah"]
    out["session_val"] = session["val"]
    return out


def fetch_agg_trades(symbol: str, limit: int = 220) -> pd.DataFrame:
    r = requests.get(
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
            "imbalance_ratio": 0.0
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


def compute_indicators(df: pd.DataFrame, symbol: Optional[str] = None) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out

    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], 200)
    out["rsi14"] = rsi(out["close"], 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(out["close"])
    out["atr14"] = atr(out, 14)
    out["vol_ma"] = out["volume"].rolling(20).mean()
    out["vol_spike"] = out["volume"] > (out["vol_ma"] * 1.5)

    out = compute_vsa(out)
    out = add_profile_levels(out)

    for col, default in {
        "agg_buy_vol": pd.NA,
        "agg_sell_vol": pd.NA,
        "agg_delta": pd.NA,
        "agg_delta_ratio": pd.NA,
        "agg_buy_pressure": False,
        "agg_sell_pressure": False,
        "best_bid": pd.NA,
        "best_ask": pd.NA,
        "spread": pd.NA,
        "spread_bps": pd.NA,
        "book_bid_vol_top": pd.NA,
        "book_ask_vol_top": pd.NA,
        "book_imbalance": pd.NA,
        "microprice": pd.NA,
        "book_bid_dominant": False,
        "book_ask_dominant": False,
        "agg_delta_bullish": False,
        "agg_delta_bearish": False,
    }.items():
        if col not in out.columns:
            out[col] = default

    return out


def score_row(df: pd.DataFrame, i: int):
    prev = df.iloc[i - 1]
    cur = df.iloc[i]

    long_checks = {
        "EMA20 > EMA50": bool(cur["ema20"] > cur["ema50"]),
        "EMA50 > EMA200": bool(cur["ema50"] > cur["ema200"]),
        "MACD Cross Up": bool(crossover_up(prev["macd"], cur["macd"], prev["macd_signal"], cur["macd_signal"])),
        "RSI > 55": bool(cur["rsi14"] > 55),
        "Vol Spike": bool(cur["vol_spike"]),
        "Bullish VSA": bool(cur.get("vsa_bullish", False)),
        "No Supply Confirm": bool(cur.get("vsa_no_supply_confirm", False)),
        "Shakeout Confirm": bool(cur.get("vsa_shakeout_confirm", False)),
        "MACD Hist > 0": bool(cur["macd_hist"] > 0),
        "Above VRVP POC": bool(pd.notna(cur.get("vrvp_poc")) and cur["close"] > cur["vrvp_poc"]),
        "Above Session POC": bool(pd.notna(cur.get("session_poc")) and cur["close"] > cur["session_poc"]),
        "Agg Delta Bullish": bool(cur.get("agg_delta_bullish", False)),
        "Book Bid Dominant": bool(cur.get("book_bid_dominant", False)),
    }

    short_checks = {
        "EMA20 < EMA50": bool(cur["ema20"] < cur["ema50"]),
        "EMA50 < EMA200": bool(cur["ema50"] < cur["ema200"]),
        "MACD Cross Down": bool(crossover_down(prev["macd"], cur["macd"], prev["macd_signal"], cur["macd_signal"])),
        "RSI < 45": bool(cur["rsi14"] < 45),
        "Vol Spike": bool(cur["vol_spike"]),
        "Bearish VSA": bool(cur.get("vsa_bearish", False)),
        "No Demand Confirm": bool(cur.get("vsa_no_demand_confirm", False)),
        "Upthrust Confirm": bool(cur.get("vsa_upthrust_confirm", False)),
        "MACD Hist < 0": bool(cur["macd_hist"] < 0),
        "Below VRVP POC": bool(pd.notna(cur.get("vrvp_poc")) and cur["close"] < cur["vrvp_poc"]),
        "Below Session POC": bool(pd.notna(cur.get("session_poc")) and cur["close"] < cur["session_poc"]),
        "Agg Delta Bearish": bool(cur.get("agg_delta_bearish", False)),
        "Book Ask Dominant": bool(cur.get("book_ask_dominant", False)),
    }

    long_weights = {
        "EMA20 > EMA50": 10,
        "EMA50 > EMA200": 8,
        "MACD Cross Up": 10,
        "RSI > 55": 8,
        "Vol Spike": 6,
        "Bullish VSA": 14,
        "No Supply Confirm": 8,
        "Shakeout Confirm": 8,
        "MACD Hist > 0": 6,
        "Above VRVP POC": 6,
        "Above Session POC": 6,
        "Agg Delta Bullish": 5,
        "Book Bid Dominant": 5,
    }
    short_weights = {
        "EMA20 < EMA50": 10,
        "EMA50 < EMA200": 8,
        "MACD Cross Down": 10,
        "RSI < 45": 8,
        "Vol Spike": 6,
        "Bearish VSA": 14,
        "No Demand Confirm": 8,
        "Upthrust Confirm": 8,
        "MACD Hist < 0": 6,
        "Below VRVP POC": 6,
        "Below Session POC": 6,
        "Agg Delta Bearish": 5,
        "Book Ask Dominant": 5,
    }

    long_score_raw = sum(long_weights[k] for k, v in long_checks.items() if v)
    short_score_raw = sum(short_weights[k] for k, v in short_checks.items() if v)

    long_score = round(100.0 * long_score_raw / sum(long_weights.values()), 2)
    short_score = round(100.0 * short_score_raw / sum(short_weights.values()), 2)

    return {
        "long_checks": long_checks,
        "short_checks": short_checks,
        "long_score": long_score,
        "short_score": short_score,
        "long_ready": long_score >= 65,
        "short_ready": short_score >= 65,
        "long_reasons": [k for k, v in long_checks.items() if v],
        "short_reasons": [k for k, v in short_checks.items() if v],
    }


def fetch_klines(symbol: str, interval: str, limit: int = HISTORY_LIMIT) -> pd.DataFrame:
    r = requests.get(
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

    return compute_indicators(pd.DataFrame(data), symbol=symbol)


class BinanceFeed(threading.Thread):
    def __init__(self, symbol: str, interval: str, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self.symbol = symbol.lower()
        self.interval = interval
        self.out_queue = out_queue
        self.ws_app: Optional[WebSocketApp] = None
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
        self.ws_app: Optional[WebSocketApp] = None
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