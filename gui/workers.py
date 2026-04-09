import pandas as pd
from PySide6.QtCore import QThread, Signal
from algos import build_vrvp, build_session_volume_profile, fetch_agg_trades, build_footprint_from_agg_trades, compute_order_book_features

class AnalyticsWorker(QThread):
    data_ready = Signal(dict)

    def __init__(self, symbol: str, df: pd.DataFrame):
        super().__init__()
        self.symbol = symbol
        self.df = df.copy()

    def run(self):
        out: Dict[str, Any] = {}

        try:
            if not self.df.empty:
                df_small = self.df.tail(320).copy()
                out["profile"] = {
                    "vrvp": build_vrvp(df_small, bins=60, lookback=min(220, len(df_small))),
                    "session": build_session_volume_profile(df_small, bins=40),
                    "last_price": float(df_small.iloc[-1]["close"]),
                }
        except Exception as e:
            out["profile_error"] = str(e)

        try:
            trades = fetch_agg_trades(self.symbol, limit=220)
            out["footprint"] = build_footprint_from_agg_trades(trades)
        except Exception as e:
            out["footprint_error"] = str(e)

        try:
            out["book"] = compute_order_book_features(self.symbol, levels=20)
        except Exception as e:
            out["book_error"] = str(e)

        self.data_ready.emit(out)


class HistoryLoadWorker(QThread):
    loaded = Signal(pd.DataFrame)
    error = Signal(str)

    def __init__(self, symbol: str, interval: str, limit: int):
        super().__init__()
        self.symbol = symbol
        self.interval = interval
        self.limit = limit

    def run(self):
        try:
            df = fetch_klines(self.symbol, self.interval, self.limit)
            df = compute_indicators(df)
            self.loaded.emit(df)
        except Exception as exc:
            self.error.emit(str(exc))


