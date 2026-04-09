"""VSA and volume profile helpers."""

from typing import Any, Dict, Optional

import pandas as pd


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
