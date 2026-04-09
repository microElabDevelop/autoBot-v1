"""Strategy core: indicator assembly and score computation."""

from typing import Dict, Any, Optional

import pandas as pd

from .indicators import ema, rsi, macd, atr, crossover_up, crossover_down
from .vsa_profile import compute_vsa, add_profile_levels


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
