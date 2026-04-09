import hmac
import hashlib
import json
import os
import queue
import requests
import time
from datetime import datetime
from html import escape
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode
try:
    import winsound
except ImportError:
    winsound = None


import pandas as pd
from PySide6.QtCore import QTimer, QUrl, QThread, Signal, Qt
from PySide6.QtGui import QAction, QColor, QBrush
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QSplitter,
    QCheckBox,
    QFrame,
    QToolButton,
    QMenu,
    QTabWidget,
    QTextBrowser,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from algos import (
    Position,
    ClosedTrade,
    BinanceFeed,
    BinanceDepthFeed,
    OrderBookHeatmapAccumulator,
    fetch_klines,
    compute_indicators,
    score_row,
    build_vrvp,
    build_session_volume_profile,
    fetch_agg_trades,
    build_footprint_from_agg_trades,
    compute_order_book_features,
    HISTORY_LIMIT,
    MAX_POINTS,
    REST_BASE,
)

POLL_MS = 80

from .templates import CHART_HTML, HEATMAP_HTML, PROFILE_HTML, FOOTPRINT_HTML
from .workers import AnalyticsWorker, HistoryLoadWorker

class MainWindow(QMainWindow):
    LONG_KEYS = [
        "EMA20 > EMA50",
        "EMA50 > EMA200",
        "MACD Cross Up",
        "RSI > 55",
        "Vol Spike",
        "Bullish VSA",
        "No Supply Confirm",
        "Shakeout Confirm",
        "MACD Hist > 0",
        "Above VRVP POC",
        "Above Session POC",
        "Agg Delta Bullish",
        "Book Bid Dominant",
    ]

    SHORT_KEYS = [
        "EMA20 < EMA50",
        "EMA50 < EMA200",
        "MACD Cross Down",
        "RSI < 45",
        "Vol Spike",
        "Bearish VSA",
        "No Demand Confirm",
        "Upthrust Confirm",
        "MACD Hist < 0",
        "Below VRVP POC",
        "Below Session POC",
        "Agg Delta Bearish",
        "Book Ask Dominant",
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Binance Futures Paper Trader Pro")
        self.resize(1920, 1120)

        self.df = pd.DataFrame()
        self.queue = queue.Queue()
        self.feed: Optional[BinanceFeed] = None
        self.depth_feed: Optional[BinanceDepthFeed] = None
        self.worker: Optional[AnalyticsWorker] = None
        self.history_worker: Optional[HistoryLoadWorker] = None

        self.running = False
        self.current_symbol = None
        self.current_interval = None
        self.last_closed_candle_time = None
        self.chart_ready = False
        self.pending_full_sync = False
        self.heatmap_ready = False
        self.profile_ready = False
        self.footprint_ready = False

        self.balance_total = 1000.0
        self.available_balance = 1000.0
        self.equity = 1000.0
        self.auto_enabled = False
        self.local_tz = datetime.now().astimezone().tzinfo
        self.session_peak_balance = self.balance_total
        self.daily_start_balance = self.balance_total
        self.daily_stats_date = pd.Timestamp.now(tz="UTC").tz_convert(self.local_tz).date()
        self.daily_trade_count = 0
        self.daily_net_pnl = 0.0
        self.consecutive_losses = 0
        self.last_loss_time: Optional[pd.Timestamp] = None

        self.trade_mode = "DEMO"
        self.saved_api_key = ""
        self.saved_api_secret = ""
        self.api_connection_ok = False
        self.active_trading_enabled = False
        self.real_wallet_balance = 0.0
        self.real_available_balance = 0.0
        self.real_margin_balance = 0.0
        self.real_unrealized_profit = 0.0

        self.positions_by_symbol: Dict[str, Position] = {}
        self.markers_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        self.closed_trades: List[ClosedTrade] = []

        self.signal_stats: Dict[str, Dict[str, Any]] = {}
        for key in self.LONG_KEYS:
            self.signal_stats[f"LONG | {key}"] = self._new_signal_stat()
        for key in self.SHORT_KEYS:
            self.signal_stats[f"SHORT | {key}"] = self._new_signal_stat()

        self.heatmap_acc = OrderBookHeatmapAccumulator(max_snapshots=250)
        self.depth_history: List[Dict[str, Any]] = []
        self.max_depth_history = 120
        self.latest_book_meta: Dict[str, Any] = {}
        self.latest_profile_payload: Dict[str, Any] = {"vrvp": None, "session": None, "last_price": None}
        self.latest_footprint_payload: Dict[str, Any] = {"levels": [], "buy_vol": [], "sell_vol": [], "delta": []}

        self.last_heatmap_push_ts = 0.0
        self.heatmap_push_interval_sec = 0.35
        self.last_analytics_start_ts = 0.0
        self.analytics_interval_sec = 4.0

        self.build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_queue)
        self.timer.start(POLL_MS)

        QTimer.singleShot(500, self.start_market)

    def _new_signal_stat(self):
        return {
            "participated": 0,
            "true_count": 0,
            "false_count": 0,
            "wins": 0,
            "losses": 0,
            "score": 0.0,
            "avg_score": 0.0,
            "net_pnl_contrib": 0.0,
            "sum_roi_contrib": 0.0,
            "power": 0.0,
        }

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        controls = QFrame()
        controls.setStyleSheet("""
            QFrame { background:#101a2f; border-radius:8px; }
            QLabel { color:#e5e7eb; }
            QLineEdit, QComboBox {
                background:#16233f; color:#e5e7eb; padding:6px;
                border:1px solid #24324d; border-radius:4px;
            }
            QPushButton, QToolButton {
                background:#16233f; color:#e5e7eb; padding:8px 12px;
                border:1px solid #24324d; border-radius:6px;
            }
            QPushButton:hover, QToolButton:hover { background:#1d2e4d; }
            QPushButton:pressed, QToolButton:pressed {
                background:#0f1b33;
                border:1px solid #60a5fa;
                padding-top:9px;
                padding-left:13px;
            }
            QCheckBox { color:#e5e7eb; }
        """)
        cg = QGridLayout(controls)

        self.symbol = QComboBox()
        self.symbol.addItems(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"])

        self.interval = QComboBox()
        self.interval.addItems(["1m", "3m", "5m", "15m"])
        self.interval.setCurrentText("5m")

        self.start_balance = QLineEdit("1000")
        self.capital_trade = QLineEdit("100")
        self.leverage = QLineEdit("5")
        self.tp = QLineEdit("1.0")
        self.sl = QLineEdit("0.5")
        self.max_hold = QLineEdit("16")
        self.fee = QLineEdit("0.04")
        self.min_score = QLineEdit("65")

        self.graph_range = QComboBox()
        self.graph_range.addItems(["100", "200", "300", "500", "800"])
        self.graph_range.setCurrentText("300")

        self.bot_type = QComboBox()
        self.bot_type.addItems([
            "Elirox Trend Robot",
            "Elirox Scalper Robot",
            "Elirox VSA Robot",
            "Elirox Breakout Robot",
            "Custom Strategy",
        ])
        self.bot_type.setCurrentText("Elirox Trend Robot")

        self.auto_follow = QCheckBox("Auto Follow Chart")
        self.auto_follow.setChecked(True)

        self.long_dropdown = QToolButton()
        self.long_dropdown.setPopupMode(QToolButton.InstantPopup)
        self.long_menu = QMenu(self)
        self.long_actions: Dict[str, QAction] = {}
        for key in self.LONG_KEYS:
            act = QAction(key, self)
            act.setCheckable(True)
            act.setChecked(key in ["EMA20 > EMA50", "MACD Cross Up", "RSI > 55", "Bullish VSA", "MACD Hist > 0"])
            act.toggled.connect(self.on_condition_changed)
            self.long_menu.addAction(act)
            self.long_actions[key] = act
        self.long_dropdown.setMenu(self.long_menu)

        self.short_dropdown = QToolButton()
        self.short_dropdown.setPopupMode(QToolButton.InstantPopup)
        self.short_menu = QMenu(self)
        self.short_actions: Dict[str, QAction] = {}
        for key in self.SHORT_KEYS:
            act = QAction(key, self)
            act.setCheckable(True)
            act.setChecked(key in ["EMA20 < EMA50", "MACD Cross Down", "RSI < 45", "Bearish VSA", "MACD Hist < 0"])
            act.toggled.connect(self.on_condition_changed)
            self.short_menu.addAction(act)
            self.short_actions[key] = act
        self.short_dropdown.setMenu(self.short_menu)

        fields = [
            ("Symbol", self.symbol),
            ("Interval", self.interval),
            ("Bot Type", self.bot_type),
            ("Start Balance", self.start_balance),
            ("Capital / Trade", self.capital_trade),
            ("Leverage", self.leverage),
            ("TP ROI %", self.tp),
            ("SL ROI %", self.sl),
            ("Max Hold Bars", self.max_hold),
            ("Fee % / side", self.fee),
            ("Min Score", self.min_score),
            ("Graph Range", self.graph_range),
            ("LONG Params", self.long_dropdown),
            ("SHORT Params", self.short_dropdown),
        ]

        for i, (label, widget) in enumerate(fields):
            cg.addWidget(QLabel(label), 0, i)
            cg.addWidget(widget, 1, i)

        cg.addWidget(self.auto_follow, 1, len(fields))

        btn_row = QHBoxLayout()
        self.btn_apply = QPushButton("Apply Symbol / Interval")
        self.btn_refresh = QPushButton("Refresh History")
        self.btn_auto_on = QPushButton("Start Auto")
        self.btn_auto_off = QPushButton("Stop Auto")
        self.btn_long = QPushButton("Manual LONG")
        self.btn_short = QPushButton("Manual SHORT")
        self.btn_toggle_trade_mode = QPushButton("Switch to REAL Trade")
        self.btn_close = QPushButton("Close Position")
        self.btn_reset_stats = QPushButton("Reset Signal Stats")
        self.trade_mode_label = QLabel("Trading Mode: DEMO")
        self.trade_mode_label.setStyleSheet("color:#a5b4fc; font-weight:600;")

        for b in [
            self.btn_apply,
            self.btn_refresh,
            self.btn_auto_on,
            self.btn_auto_off,
            self.btn_long,
            self.btn_short,
            self.btn_toggle_trade_mode,
            self.btn_close,
            self.btn_reset_stats,
        ]:
            btn_row.addWidget(b)
        btn_row.addWidget(self.trade_mode_label)

        cg.addLayout(btn_row, 2, 0, 1, len(fields) + 1)
        root.addWidget(controls)

        splitter = QSplitter()
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.web = QWebEngineView()
        ws = self.web.settings()
        ws.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        self.web.loadFinished.connect(self.on_chart_loaded)
        self.web.setHtml(CHART_HTML, QUrl("https://unpkg.com/"))
        left_layout.addWidget(self.web)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: 0; }
            QTabBar::tab {
                background: #16233f;
                color: #e5e7eb;
                padding: 8px 14px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 2px;
            }
            QTabBar::tab:selected { background: #1d2e4d; }
        """)

        self.dashboard_tab = QWidget()
        self.trades_tab = QWidget()
        self.signal_stats_tab = QWidget()
        self.strategy_tab = QWidget()
        self.api_tab = QWidget()
        self.orderflow_tab = QWidget()
        self.psychology_tab = QWidget()

        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.trades_tab, "Trades")
        self.tabs.addTab(self.signal_stats_tab, "Signal Stats")
        self.tabs.addTab(self.strategy_tab, "Strategy")
        self.tabs.addTab(self.api_tab, "Binance API")
        self.tabs.addTab(self.orderflow_tab, "Order Flow")
        self.tabs.addTab(self.psychology_tab, "Psychology Params")

        mono_style = """
            background:#101a2f;
            color:#e5e7eb;
            font-family: Consolas, 'Courier New', monospace;
            font-size: 15px;
        """

        dash_layout = QVBoxLayout(self.dashboard_tab)
        self.dashboard_subtabs = QTabWidget()
        self.dashboard_subtabs.setStyleSheet(self.tabs.styleSheet())
        self.dashboard_status_tab = QWidget()
        self.dashboard_checklist_tab = QWidget()
        self.dashboard_indicators_tab = QWidget()
        self.dashboard_subtabs.addTab(self.dashboard_status_tab, "Status")
        self.dashboard_subtabs.addTab(self.dashboard_checklist_tab, "Checklist")
        self.dashboard_subtabs.addTab(self.dashboard_indicators_tab, "Indicators")
        dash_layout.addWidget(self.dashboard_subtabs)

        self.lbl_status = QLabel("Starting...")
        self.lbl_auto = QLabel("Auto Trading: OFF")
        self.lbl_balance = QLabel("Total Balance: 1000.00")
        self.lbl_available = QLabel("Available Balance: 1000.00")
        self.lbl_equity = QLabel("Equity: 1000.00")
        self.lbl_position = QLabel("Position: Flat")
        self.lbl_signal = QLabel("Signal: Waiting")
        self.lbl_live_pnl = QLabel("Live Position PnL: 0.00 | ROI: 0.00%")
        self.lbl_score = QLabel("Signal Score | Long: 0.00 | Short: 0.00")

        status_tab_layout = QVBoxLayout(self.dashboard_status_tab)
        status_overview_frame = QFrame()
        status_overview_frame.setStyleSheet("""
            QFrame { background:#0f172a; border:1px solid #334155; border-radius:8px; }
            QLabel { color:#e5e7eb; }
        """)
        status_overview_layout = QVBoxLayout(status_overview_frame)
        status_overview_layout.setContentsMargins(10, 8, 10, 8)
        status_overview_layout.setSpacing(4)
        status_overview_title = QLabel("Account / Signal Overview")
        status_overview_title.setStyleSheet("font-weight:800; color:#22d3ee;")
        status_overview_layout.addWidget(status_overview_title)
        self.lbl_status_overview = QLabel("Loading...")
        self.lbl_status_overview.setWordWrap(True)
        self.lbl_status_overview.setTextFormat(Qt.RichText)
        self.lbl_status_overview.setStyleSheet("color:#e5e7eb;")
        self.lbl_status_overview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        status_overview_layout.addWidget(self.lbl_status_overview)
        status_tab_layout.addWidget(status_overview_frame)

        status_panel = QFrame()
        status_panel.setStyleSheet("""
            QFrame { background:#0f172a; border:1px solid #334155; border-radius:8px; }
            QLabel { color:#e5e7eb; }
        """)
        status_panel_layout = QVBoxLayout(status_panel)
        status_panel_layout.setContentsMargins(10, 8, 10, 8)
        status_panel_layout.setSpacing(4)
        status_panel_title = QLabel("Trade Status")
        status_panel_title.setStyleSheet("font-weight:800; color:#93c5fd;")
        status_panel_layout.addWidget(status_panel_title)
        self.lbl_trade_status = QLabel("Evaluating...")
        self.lbl_trade_status.setWordWrap(True)
        self.lbl_trade_status.setTextFormat(Qt.RichText)
        self.lbl_trade_status.setStyleSheet("color:#f8fafc;")
        self.lbl_trade_status.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        status_panel_layout.addWidget(self.lbl_trade_status)
        status_tab_layout.addWidget(status_panel)
        status_tab_layout.addStretch(1)

        checklist_tab_layout = QVBoxLayout(self.dashboard_checklist_tab)
        checklist_tab_layout.addWidget(QLabel("Signal + VSA Checklist"))
        self.checklist_frame = QFrame()
        self.checklist_frame.setStyleSheet("""
            QFrame { background:#0f172a; border:1px solid #334155; border-radius:8px; }
            QLabel { color:#e5e7eb; }
        """)
        checklist_layout = QVBoxLayout(self.checklist_frame)
        checklist_layout.setContentsMargins(10, 8, 10, 8)
        checklist_layout.setSpacing(4)
        self.lbl_checklist = QLabel("Checklist loading...")
        self.lbl_checklist.setTextFormat(Qt.RichText)
        self.lbl_checklist.setWordWrap(True)
        self.lbl_checklist.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.lbl_checklist.setStyleSheet("font-size:13px; line-height:1.2;")
        checklist_layout.addWidget(self.lbl_checklist)
        checklist_tab_layout.addWidget(self.checklist_frame)
        checklist_tab_layout.addStretch(1)

        indicators_tab_layout = QVBoxLayout(self.dashboard_indicators_tab)
        indicators_tab_layout.addWidget(QLabel("Indicator Info"))
        self.txt_indicators_tab = QTextBrowser()
        self.txt_indicators_tab.setOpenExternalLinks(False)
        self.txt_indicators_tab.setStyleSheet("""
            QTextBrowser {
                background:#0f172a;
                color:#e5e7eb;
                border:1px solid #334155;
                border-radius:8px;
                padding:8px;
                font-family: 'Segoe UI';
                font-size: 13px;
            }
        """)
        indicators_tab_layout.addWidget(self.txt_indicators_tab)
        self.indicator_text_cache = "No indicator data yet."

        trades_layout = QVBoxLayout(self.trades_tab)
        trades_layout.addWidget(QLabel("Open Trades"))
        self.txt_open_trades = QPlainTextEdit()
        self.txt_open_trades.setReadOnly(True)
        self.txt_open_trades.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.txt_open_trades.setStyleSheet(mono_style)
        trades_layout.addWidget(self.txt_open_trades)

        trades_layout.addWidget(QLabel("Closed Trades"))
        self.txt_closed_trades = QPlainTextEdit()
        self.txt_closed_trades.setReadOnly(True)
        self.txt_closed_trades.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.txt_closed_trades.setStyleSheet(mono_style)
        trades_layout.addWidget(self.txt_closed_trades)

        stats_layout = QVBoxLayout(self.signal_stats_tab)
        stats_layout.addWidget(QLabel("Marginal Signal Accuracy / Performance"))
        self.tbl_signal_stats = QTableWidget()
        self.tbl_signal_stats.setColumnCount(11)
        self.tbl_signal_stats.setHorizontalHeaderLabels([
            "Side",
            "Condition",
            "Part",
            "True",
            "False",
            "Wins",
            "Loss",
            "Score",
            "Avg",
            "PnL",
            "Power",
        ])
        self.tbl_signal_stats.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_signal_stats.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_signal_stats.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_signal_stats.setAlternatingRowColors(True)
        self.tbl_signal_stats.setWordWrap(False)
        self.tbl_signal_stats.setTextElideMode(Qt.ElideNone)
        self.tbl_signal_stats.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        self.tbl_signal_stats.verticalHeader().setVisible(False)
        self.tbl_signal_stats.horizontalHeader().setStretchLastSection(False)
        self.tbl_signal_stats.horizontalHeader().setTextElideMode(Qt.ElideNone)
        self.tbl_signal_stats.horizontalHeader().setMinimumSectionSize(70)
        self.tbl_signal_stats.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_signal_stats.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        for col in range(2, 11):
            self.tbl_signal_stats.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tbl_signal_stats.setColumnWidth(1, 420)
        self.tbl_signal_stats.setStyleSheet("""
            QTableWidget {
                background:#0f172a;
                color:#e5e7eb;
                gridline-color:#22304a;
                border:1px solid #334155;
                border-radius:8px;
                selection-background-color:#1d4ed8;
                alternate-background-color:#111827;
            }
            QHeaderView::section {
                background:#111827;
                color:#93c5fd;
                padding:6px;
                border:0;
                font-weight:700;
            }
        """)
        stats_layout.addWidget(self.tbl_signal_stats)

        strategy_layout = QVBoxLayout(self.strategy_tab)
        strategy_layout.addWidget(QLabel("Strategy Runner"))
        self.btn_run_strategy = QPushButton("Run Strategy Now")
        self.btn_run_strategy.setToolTip("Evaluate current candle and display strategy signals and readiness.")
        strategy_layout.addWidget(self.btn_run_strategy)
        self.txt_strategy_report = QTextBrowser()
        self.txt_strategy_report.setOpenExternalLinks(False)
        self.txt_strategy_report.setStyleSheet("""
            QTextBrowser {
                background:#0f172a;
                color:#e5e7eb;
                border:1px solid #334155;
                border-radius:8px;
                padding:10px;
                font-family: 'Segoe UI';
                font-size: 13px;
            }
        """)
        self.txt_strategy_report.setHtml("<i>Press \"Run Strategy Now\" to evaluate the current market signal.</i>")
        strategy_layout.addWidget(self.txt_strategy_report)
        strategy_layout.addStretch(1)

        api_layout = QVBoxLayout(self.api_tab)
        api_layout.addWidget(QLabel("Binance API Settings"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("API Key")
        self.api_secret_input = QLineEdit()
        self.api_secret_input.setPlaceholderText("API Secret")
        self.api_secret_input.setEchoMode(QLineEdit.Password)
        self.btn_save_api = QPushButton("Save API Keys")
        self.lbl_api_status = QLabel("API Mode: Demo (keys not saved)")
        self.lbl_api_status.setWordWrap(True)
        self.lbl_api_status.setStyleSheet("color:#93c5fd;")
        api_layout.addWidget(QLabel("API Key"))
        api_layout.addWidget(self.api_key_input)
        api_layout.addWidget(QLabel("API Secret"))
        api_layout.addWidget(self.api_secret_input)
        api_layout.addWidget(self.btn_save_api)
        self.btn_test_connection = QPushButton("Test Connection")
        self.btn_active_trading = QPushButton("Enable Active Trading")
        self.btn_active_trading.setEnabled(False)
        self.lbl_active_trading = QLabel("Active Trading: OFF")
        self.lbl_active_trading.setStyleSheet("color:#f8fafc;")
        api_layout.addWidget(self.btn_test_connection)
        api_layout.addWidget(self.btn_active_trading)
        api_layout.addWidget(self.lbl_active_trading)
        api_layout.addWidget(self.lbl_api_status)
        self.api_details_display = QPlainTextEdit()
        self.api_details_display.setReadOnly(True)
        self.api_details_display.setStyleSheet("background:#0f172a; color:#e5e7eb; border:1px solid #334155; border-radius:8px; padding:8px;")
        self.api_details_display.setPlainText("Binance connection details will appear here.")
        api_layout.addWidget(self.api_details_display)
        api_layout.addStretch(1)

        orderflow_layout = QVBoxLayout(self.orderflow_tab)
        self.orderflow_tabs = QTabWidget()
        self.orderflow_tabs.setStyleSheet(self.tabs.styleSheet())

        self.heatmap_view = QWebEngineView()
        self.profile_view = QWebEngineView()
        self.footprint_view = QWebEngineView()

        for view, html, cb in [
            (self.heatmap_view, HEATMAP_HTML, self.on_heatmap_loaded),
            (self.profile_view, PROFILE_HTML, self.on_profile_loaded),
            (self.footprint_view, FOOTPRINT_HTML, self.on_footprint_loaded),
        ]:
            s = view.settings()
            s.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            s.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            s.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
            view.loadFinished.connect(cb)
            view.setHtml(html)

        self.orderflow_tabs.addTab(self.heatmap_view, "Book Heatmap")
        self.orderflow_tabs.addTab(self.profile_view, "VRVP / Session")
        self.orderflow_tabs.addTab(self.footprint_view, "Agg Footprint")
        orderflow_layout.addWidget(self.orderflow_tabs)

        psychology_layout = QVBoxLayout(self.psychology_tab)
        psychology_layout.addWidget(QLabel("Trader Psychology / Risk Guardrails"))

        psychology_sub = QLabel(
            "User-side limits for discipline, risk pacing, and trade selection."
        )
        psychology_sub.setStyleSheet("color:#9ca3af;")
        psychology_layout.addWidget(psychology_sub)

        self.psychology_subtabs = QTabWidget()
        self.psychology_subtabs.setStyleSheet(self.tabs.styleSheet())
        self.psychology_settings_tab = QWidget()
        self.psychology_display_tab = QWidget()
        self.psychology_subtabs.addTab(self.psychology_settings_tab, "Settings")
        self.psychology_subtabs.addTab(self.psychology_display_tab, "Display")
        psychology_layout.addWidget(self.psychology_subtabs)

        psychology_settings_layout = QVBoxLayout(self.psychology_settings_tab)

        psychology_frame = QFrame()
        psychology_frame.setStyleSheet("""
            QFrame { background:#101a2f; border-radius:8px; }
            QLabel { color:#e5e7eb; }
            QLineEdit {
                background:#16233f; color:#e5e7eb; padding:6px;
                border:1px solid #24324d; border-radius:4px;
            }
        """)
        psychology_grid = QGridLayout(psychology_frame)

        psychology_defaults = [
            ("max_loss_per_trade_pct", "1.5", "Maximum allowed loss per trade as a percent of capital."),
            ("daily_loss_limit_pct", "4.0", "Stop trading for the day after this loss percentage."),
            ("drawdown_circuit_breaker_pct", "10.0", "Hard drawdown limit that acts as a circuit breaker."),
            ("max_consecutive_losses", "3", "Pause trading after this many losses in a row."),
            ("post_loss_cooldown_min", "30", "Cooldown time after a losing trade."),
            ("profit_taking_threshold_pct", "3.0", "Profit level that should trigger profit protection."),
            ("trailing_stop_pct", "1.0", "Trailing stop distance after profit protection is active."),
            ("max_open_positions", "3", "Maximum number of open positions allowed."),
            ("signal_age_limit_sec", "60", "Ignore stale signals older than this many seconds."),
            ("require_indicators_agree", "2", "Minimum number of indicators that must agree."),
            ("trade_cap_per_day", "10", "Maximum number of trades the user can take in one day."),
            ("news_blackout_minutes", "15", "Minutes to avoid new trades around high-impact news."),
        ]
        self.psychological_inputs: Dict[str, QLineEdit] = {}

        for idx, (key, default, tip) in enumerate(psychology_defaults):
            label = QLabel(key)
            label.setToolTip(tip)
            field = QLineEdit(default)
            field.setToolTip(tip)
            field.textChanged.connect(self.on_psych_param_changed)
            self.psychological_inputs[key] = field

            col = idx % 2
            row = (idx // 2) * 2
            base_col = col * 2
            psychology_grid.addWidget(label, row, base_col)
            psychology_grid.addWidget(field, row + 1, base_col)

        psychology_settings_layout.addWidget(psychology_frame)

        psychology_display_layout = QVBoxLayout(self.psychology_display_tab)
        psychology_display_layout.addWidget(QLabel("Evaluated Values"))
        self.txt_psychology_eval = QTextBrowser()
        self.txt_psychology_eval.setOpenExternalLinks(False)
        self.txt_psychology_eval.setStyleSheet("""
            QTextBrowser {
                background:#0f172a;
                color:#e5e7eb;
                border:1px solid #334155;
                border-radius:8px;
                padding:8px;
                font-family:'Segoe UI';
                font-size:13px;
            }
        """)
        psychology_display_layout.addWidget(self.txt_psychology_eval)

        right_layout.addWidget(self.tabs)
        splitter.addWidget(right)
        splitter.setSizes([1180, 700])

        self.setStyleSheet("QMainWindow{background:#0b1220;} QLabel{color:#e5e7eb;}")

        self.btn_apply.clicked.connect(self.restart_market)
        self.btn_refresh.clicked.connect(self.load_history)
        self.btn_auto_on.clicked.connect(self.start_auto)
        self.btn_auto_off.clicked.connect(self.stop_auto)
        self.btn_long.clicked.connect(self.manual_long)
        self.btn_short.clicked.connect(self.manual_short)
        self.btn_toggle_trade_mode.clicked.connect(self.toggle_trade_mode)
        self.btn_close.clicked.connect(self.manual_close)
        self.btn_reset_stats.clicked.connect(self.reset_signal_stats)
        self.btn_save_api.clicked.connect(self.save_api_keys)
        self.btn_test_connection.clicked.connect(self.test_binance_connection)
        self.btn_active_trading.clicked.connect(self.toggle_active_trading)
        self.btn_run_strategy.clicked.connect(self.run_strategy)

        for btn in [
            self.btn_apply,
            self.btn_refresh,
            self.btn_auto_on,
            self.btn_auto_off,
            self.btn_long,
            self.btn_short,
            self.btn_toggle_trade_mode,
            self.btn_close,
            self.btn_reset_stats,
            self.btn_save_api,
            self.btn_test_connection,
            self.btn_active_trading,
            self.btn_run_strategy,
        ]:
            btn.clicked.connect(self.play_click_sound)

        self.refresh_condition_button_texts()
        self.refresh_psychology_tab()
        self.load_saved_api_keys()
        self.update_trade_mode_button()
        self.update_active_trading_button()
        self.update_real_mode_styles()

    def current_symbol_key(self) -> str:
        return self.symbol.currentText().strip().upper()

    def current_markers(self) -> List[Dict[str, Any]]:
        return self.markers_by_symbol.get(self.current_symbol_key(), [])

    def set_current_markers(self, markers: List[Dict[str, Any]]):
        self.markers_by_symbol[self.current_symbol_key()] = markers

    def current_position(self) -> Optional[Position]:
        return self.positions_by_symbol.get(self.current_symbol_key())

    def set_current_position(self, pos: Optional[Position]):
        key = self.current_symbol_key()
        if pos is None:
            self.positions_by_symbol.pop(key, None)
        else:
            self.positions_by_symbol[key] = pos

    def on_chart_loaded(self, ok: bool):
        self.chart_ready = ok
        if ok and self.pending_full_sync:
            self.pending_full_sync = False
            self.sync_chart_full()

    def on_heatmap_loaded(self, ok: bool):
        self.heatmap_ready = ok
        if ok:
            self.push_heatmap()

    def on_profile_loaded(self, ok: bool):
        self.profile_ready = ok
        if ok:
            self.push_profile()

    def on_footprint_loaded(self, ok: bool):
        self.footprint_ready = ok
        if ok:
            self.push_footprint()

    def js_chart(self, script: str):
        if self.chart_ready:
            self.web.page().runJavaScript(script)

    def js_heatmap(self, script: str):
        if self.heatmap_ready:
            self.heatmap_view.page().runJavaScript(script)

    def js_profile(self, script: str):
        if self.profile_ready:
            self.profile_view.page().runJavaScript(script)

    def js_footprint(self, script: str):
        if self.footprint_ready:
            self.footprint_view.page().runJavaScript(script)

    def set_text_preserve_scroll(self, widget: QPlainTextEdit, text: str):
        vbar = widget.verticalScrollBar()
        old_v = vbar.value()
        at_bottom = old_v >= max(0, vbar.maximum() - 2)
        widget.blockSignals(True)
        widget.setPlainText(text)
        widget.blockSignals(False)
        if at_bottom:
            vbar.setValue(vbar.maximum())
        else:
            vbar.setValue(min(old_v, vbar.maximum()))

    def refresh_condition_button_texts(self):
        long_on = sum(1 for a in self.long_actions.values() if a.isChecked())
        short_on = sum(1 for a in self.short_actions.values() if a.isChecked())
        self.long_dropdown.setText(f"LONG Params ({long_on})")
        self.short_dropdown.setText(f"SHORT Params ({short_on})")

    def on_condition_changed(self):
        self.refresh_condition_button_texts()
        self.refresh_panels()

    def refresh_trade_status_bar(self, scored: Optional[Dict[str, Any]] = None):
        payload = self.build_trade_status_payload(scored)
        self.lbl_trade_status.setText(self.build_trade_status_html(payload))
        self.refresh_status_overview()

    def on_psych_param_changed(self):
        self.refresh_psychology_tab()
        self.refresh_trade_status_bar()

    def status_value(self, text: str, prefix: str) -> str:
        needle = prefix + ":"
        if text.startswith(needle):
            return text.split(":", 1)[1].strip()
        return text

    def refresh_status_overview(self):
        status_val = self.status_value(self.lbl_status.text(), "Market stream live")
        auto_val = self.status_value(self.lbl_auto.text(), "Auto Trading")
        bal_val = self.status_value(self.lbl_balance.text(), "Total Balance")
        avail_val = self.status_value(self.lbl_available.text(), "Available Balance")
        eq_val = self.status_value(self.lbl_equity.text(), "Equity")
        pos_val = self.status_value(self.lbl_position.text(), "Position")
        sig_val = self.status_value(self.lbl_signal.text(), "Signal")
        pnl_val = self.status_value(self.lbl_live_pnl.text(), "Live Position PnL")
        score_val = self.status_value(self.lbl_score.text(), "Signal Score")

        auto_color = "#22c55e" if auto_val.upper() == "ON" else "#f97316"
        sig_color = "#22c55e" if "READY" in sig_val.upper() else "#fbbf24"
        pos_color = "#38bdf8" if "FLAT" not in pos_val.upper() else "#94a3b8"

        cards = [
            ("Market", status_val, "#60a5fa"),
            ("Auto", auto_val, auto_color),
            ("Position", pos_val, pos_color),
            ("Total Balance", bal_val, "#34d399"),
            ("Available", avail_val, "#2dd4bf"),
            ("Equity", eq_val, "#22d3ee"),
            ("Signal", sig_val, sig_color),
            ("Live PnL / ROI", pnl_val, "#f59e0b"),
            ("Long / Short Score", score_val, "#c084fc"),
        ]

        html = ["<table width='100%' cellspacing='6' cellpadding='0'>"]
        for i in range(0, len(cards), 3):
            html.append("<tr>")
            for title, value, color in cards[i:i + 3]:
                html.append(
                    "<td style='background:#111827; border:1px solid #334155; padding:6px;'>"
                    f"<div style='color:{color}; font-weight:700; font-size:11px;'>{escape(title)}</div>"
                    f"<div style='color:#e2e8f0; font-size:14px;'>{escape(value)}</div>"
                    "</td>"
                )
            if len(cards[i:i + 3]) < 3:
                for _ in range(3 - len(cards[i:i + 3])):
                    html.append("<td></td>")
            html.append("</tr>")
        html.append("</table>")
        self.lbl_status_overview.setText("".join(html))

    def fee_rate(self):
        try:
            return max(0.0, float(self.fee.text()) / 100.0)
        except Exception:
            return 0.0004

    def capital_per_trade(self):
        try:
            return max(1.0, float(self.capital_trade.text()))
        except Exception:
            return 100.0

    def leverage_val(self):
        try:
            return max(1.0, float(self.leverage.text()))
        except Exception:
            return 5.0

    def min_score_val(self):
        try:
            return max(1.0, float(self.min_score.text()))
        except Exception:
            return 65.0

    def max_hold_bars_val(self):
        try:
            return max(1, int(self.max_hold.text()))
        except Exception:
            return 16

    def psychological_params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        int_keys = {
            "max_consecutive_losses",
            "post_loss_cooldown_min",
            "max_open_positions",
            "signal_age_limit_sec",
            "require_indicators_agree",
            "trade_cap_per_day",
            "news_blackout_minutes",
        }

        for key, field in self.psychological_inputs.items():
            raw = field.text().strip()
            try:
                params[key] = int(raw) if key in int_keys else float(raw)
            except Exception:
                params[key] = raw

        return params

    def psych_float(self, params: Dict[str, Any], key: str, default: float) -> float:
        try:
            return float(params.get(key, default))
        except Exception:
            return default

    def psych_int(self, params: Dict[str, Any], key: str, default: int) -> int:
        try:
            return int(params.get(key, default))
        except Exception:
            return default

    def local_now(self) -> pd.Timestamp:
        return pd.Timestamp.now(tz="UTC").tz_convert(self.local_tz)

    def to_local_ts(self, ts: Optional[pd.Timestamp]) -> Optional[pd.Timestamp]:
        if ts is None:
            return None
        out = pd.Timestamp(ts)
        if out.tzinfo is None:
            out = out.tz_localize("UTC")
        return out.tz_convert(self.local_tz)

    def ensure_daily_stats_current(self, now: Optional[pd.Timestamp] = None):
        if now is None:
            now = self.local_now()
        today = now.date()
        if self.daily_stats_date != today:
            self.daily_stats_date = today
            self.daily_trade_count = 0
            self.daily_net_pnl = 0.0
            self.daily_start_balance = self.balance_total

    def selected_indicator_state(self, side: str, scored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if side == "long":
            selected = self.selected_long_checks()
            checks = scored["long_checks"] if scored else {}
            score_val = scored.get("long_score_user", scored.get("long_score", 0.0)) if scored else 0.0
            setup_ready = bool(scored and scored.get("long_ready_user"))
        else:
            selected = self.selected_short_checks()
            checks = scored["short_checks"] if scored else {}
            score_val = scored.get("short_score_user", scored.get("short_score", 0.0)) if scored else 0.0
            setup_ready = bool(scored and scored.get("short_ready_user"))

        enabled = [k for k, v in selected.items() if v]
        true_keys = [k for k in enabled if checks.get(k, False)]
        false_keys = [k for k in enabled if not checks.get(k, False)]
        return {
            "selected_count": len(enabled),
            "agree_count": len(true_keys),
            "true_keys": true_keys,
            "false_keys": false_keys,
            "score": score_val,
            "setup_ready": setup_ready,
        }

    def evaluate_psychology(self, scored: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = self.local_now()
        self.ensure_daily_stats_current(now)

        params = self.psychological_params()
        pos = self.current_position()
        long_state = self.selected_indicator_state("long", scored)
        short_state = self.selected_indicator_state("short", scored)
        live_roi = None
        if pos is not None and not self.df.empty:
            live_roi = self.current_live_roi_pct(
                pos.side,
                pos.entry_price,
                float(self.df.iloc[-1]["close"]),
                pos.leverage,
                self.fee_rate(),
            )

        daily_base = max(1e-9, float(self.daily_start_balance))
        daily_loss_pct = max(0.0, (-self.daily_net_pnl / daily_base) * 100.0)
        peak_balance = max(1e-9, float(self.session_peak_balance))
        drawdown_pct = max(0.0, ((peak_balance - float(self.balance_total)) / peak_balance) * 100.0)

        signal_age_sec = None
        if self.last_closed_candle_time is not None:
            last_closed_local = self.to_local_ts(self.last_closed_candle_time)
            if last_closed_local is not None:
                signal_age_sec = max(0.0, (now - last_closed_local).total_seconds())

        cooldown_remaining_min = 0.0
        if self.last_loss_time is not None:
            last_loss_local = self.to_local_ts(self.last_loss_time)
            if last_loss_local is not None:
                elapsed_min = max(0.0, (now - last_loss_local).total_seconds() / 60.0)
                cooldown_remaining_min = max(
                    0.0,
                    self.psych_float(params, "post_loss_cooldown_min", 30.0) - elapsed_min,
                )

        sl_pct = 0.0
        try:
            sl_pct = max(0.0, float(self.sl.text()))
        except Exception:
            sl_pct = 0.0

        required_agree = max(0, self.psych_int(params, "require_indicators_agree", 2))
        gates = {
            "max_loss_per_trade_pct": {
                "pass": sl_pct <= self.psych_float(params, "max_loss_per_trade_pct", 1.5),
                "detail": (
                    f"max_loss_per_trade_pct exceeded "
                    f"({sl_pct:.2f}% configured vs {self.psych_float(params, 'max_loss_per_trade_pct', 1.5):.2f}% limit)"
                ),
            },
            "daily_loss_limit_pct": {
                "pass": daily_loss_pct < self.psych_float(params, "daily_loss_limit_pct", 4.0),
                "detail": (
                    f"daily_loss_limit_pct hit "
                    f"({daily_loss_pct:.2f}% today vs {self.psych_float(params, 'daily_loss_limit_pct', 4.0):.2f}% limit)"
                ),
            },
            "drawdown_circuit_breaker_pct": {
                "pass": drawdown_pct < self.psych_float(params, "drawdown_circuit_breaker_pct", 10.0),
                "detail": (
                    f"drawdown_circuit_breaker_pct hit "
                    f"({drawdown_pct:.2f}% drawdown vs {self.psych_float(params, 'drawdown_circuit_breaker_pct', 10.0):.2f}% limit)"
                ),
            },
            "max_consecutive_losses": {
                "pass": self.consecutive_losses < self.psych_int(params, "max_consecutive_losses", 3),
                "detail": (
                    f"max_consecutive_losses hit "
                    f"({self.consecutive_losses} losses vs {self.psych_int(params, 'max_consecutive_losses', 3)} limit)"
                ),
            },
            "post_loss_cooldown_min": {
                "pass": cooldown_remaining_min <= 0.0,
                "detail": (
                    f"post_loss_cooldown_min active "
                    f"({cooldown_remaining_min:.1f} min remaining)"
                ),
            },
            "profit_taking_threshold_pct": {
                "pass": True,
                "detail": (
                    f"profit_taking_threshold_pct tracking "
                    f"({0.0 if live_roi is None else live_roi:.2f}% live ROI vs "
                    f"{self.psych_float(params, 'profit_taking_threshold_pct', 3.0):.2f}% threshold)"
                ),
                "enforced": False,
            },
            "trailing_stop_pct": {
                "pass": True,
                "detail": (
                    f"trailing_stop_pct tracking "
                    f"({self.psych_float(params, 'trailing_stop_pct', 1.0):.2f}% trailing distance configured)"
                ),
                "enforced": False,
            },
            "max_open_positions": {
                "pass": len(self.positions_by_symbol) < self.psych_int(params, "max_open_positions", 3),
                "detail": (
                    f"max_open_positions reached "
                    f"({len(self.positions_by_symbol)} open vs {self.psych_int(params, 'max_open_positions', 3)} limit)"
                ),
            },
            "signal_age_limit_sec": {
                "pass": signal_age_sec is None or signal_age_sec <= self.psych_float(params, "signal_age_limit_sec", 60.0),
                "detail": (
                    f"signal_age_limit_sec exceeded "
                    f"({0.0 if signal_age_sec is None else signal_age_sec:.1f}s vs {self.psych_float(params, 'signal_age_limit_sec', 60.0):.1f}s limit)"
                ),
            },
            "trade_cap_per_day": {
                "pass": self.daily_trade_count < self.psych_int(params, "trade_cap_per_day", 10),
                "detail": (
                    f"trade_cap_per_day reached "
                    f"({self.daily_trade_count} trades vs {self.psych_int(params, 'trade_cap_per_day', 10)} limit)"
                ),
            },
            "news_blackout_minutes": {
                "pass": True,
                "detail": (
                    f"news_blackout_minutes monitor only "
                    f"({self.psych_int(params, 'news_blackout_minutes', 15)} min, no news feed connected)"
                ),
                "enforced": False,
            },
        }

        sides = {
            "long": {
                **long_state,
                "indicator_gate_pass": long_state["agree_count"] >= required_agree,
                "indicator_detail": (
                    f"require_indicators_agree needs {required_agree}, current long agree={long_state['agree_count']}"
                ),
            },
            "short": {
                **short_state,
                "indicator_gate_pass": short_state["agree_count"] >= required_agree,
                "indicator_detail": (
                    f"require_indicators_agree needs {required_agree}, current short agree={short_state['agree_count']}"
                ),
            },
        }

        return {
            "now": str(now),
            "auto_enabled": self.auto_enabled,
            "position_live": pos is not None,
            "live_roi": live_roi,
            "daily_trade_count": self.daily_trade_count,
            "daily_net_pnl": self.daily_net_pnl,
            "daily_loss_pct": daily_loss_pct,
            "daily_start_balance": self.daily_start_balance,
            "drawdown_pct": drawdown_pct,
            "session_peak_balance": self.session_peak_balance,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_remaining_min": cooldown_remaining_min,
            "open_positions": len(self.positions_by_symbol),
            "signal_age_sec": signal_age_sec,
            "gates": gates,
            "sides": sides,
            "params": params,
        }

    def entry_blockers_for_side(
        self,
        side: str,
        scored: Optional[Dict[str, Any]] = None,
        evaluation: Optional[Dict[str, Any]] = None,
        include_auto_state: bool = True,
        include_position_state: bool = True,
    ) -> List[str]:
        blockers: List[str] = []
        if include_auto_state and not self.auto_enabled:
            blockers.append("auto trading OFF")
        if include_position_state and self.current_position() is not None:
            blockers.append("trade already live")
        if self.capital_per_trade() > self.available_balance:
            blockers.append(
                f"capital/trade {self.capital_per_trade():.2f} exceeds available {self.available_balance:.2f}"
            )
        if scored is None:
            blockers.append("waiting for more candles")
            return blockers

        side_info = side.lower()
        if not scored.get(f"{side_info}_ready_user", False):
            blockers.append("selected setup not ready")

        if evaluation is None:
            evaluation = self.evaluate_psychology(scored)

        for key, gate in evaluation["gates"].items():
            if gate.get("enforced", True) and not gate["pass"]:
                blockers.append(gate["detail"])

        side_eval = evaluation["sides"][side_info]
        if not side_eval["indicator_gate_pass"]:
            blockers.append(side_eval["indicator_detail"])

        return blockers

    def unique_reasons(self, values: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for v in values:
            key = str(v).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    def build_trade_status_payload(self, scored: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        pos = self.current_position()
        if pos is not None:
            return {
                "state": "LIVE",
                "summary": f"{pos.side.upper()} | {pos.source.upper()} | Entry {pos.entry_price:.4f} | Bars {pos.bars_held}",
                "scores": None,
                "global_blockers": [],
                "long_blockers": [],
                "short_blockers": [],
            }

        if not self.auto_enabled:
            return {
                "state": "IDLE",
                "summary": "Auto trading OFF",
                "scores": None,
                "global_blockers": [],
                "long_blockers": ["auto trading OFF"],
                "short_blockers": ["auto trading OFF"],
            }

        if len(self.df) < 220:
            return {
                "state": "WAITING",
                "summary": "Need more candles before evaluation",
                "scores": None,
                "global_blockers": [],
                "long_blockers": ["waiting for more candles"],
                "short_blockers": ["waiting for more candles"],
            }

        if scored is None:
            scored = self.score_current_row()
        if scored is None:
            return {
                "state": "WAITING",
                "summary": "Signal data not ready",
                "scores": None,
                "global_blockers": [],
                "long_blockers": ["signal data not ready"],
                "short_blockers": ["signal data not ready"],
            }

        evaluation = self.evaluate_psychology(scored)
        global_blockers = self.unique_reasons([
            gate["detail"]
            for gate in evaluation["gates"].values()
            if gate.get("enforced", True) and not gate["pass"]
        ])

        long_blockers = self.unique_reasons(
            self.entry_blockers_for_side("long", scored, evaluation, include_auto_state=False, include_position_state=False)
        )
        short_blockers = self.unique_reasons(
            self.entry_blockers_for_side("short", scored, evaluation, include_auto_state=False, include_position_state=False)
        )

        long_ready = scored["long_ready_user"] and not long_blockers
        short_ready = scored["short_ready_user"] and not short_blockers

        if long_ready or short_ready:
            ready_parts = []
            if long_ready:
                ready_parts.append(f"LONG {scored.get('long_score_user', scored['long_score']):.2f}")
            if short_ready:
                ready_parts.append(f"SHORT {scored.get('short_score_user', scored['short_score']):.2f}")
            summary = "Ready: " + ", ".join(ready_parts)
            return {
                "state": "READY",
                "summary": summary,
                "scores": {
                    "long": scored.get("long_score_user", scored["long_score"]),
                    "short": scored.get("short_score_user", scored["short_score"]),
                },
                "global_blockers": global_blockers,
                "long_blockers": [] if long_ready else long_blockers,
                "short_blockers": [] if short_ready else short_blockers,
            }

        return {
            "state": "BLOCKED",
            "summary": (
                f"Not activated | "
                f"L={scored.get('long_score_user', scored['long_score']):.2f} "
                f"S={scored.get('short_score_user', scored['short_score']):.2f}"
            ),
            "scores": {
                "long": scored.get("long_score_user", scored["long_score"]),
                "short": scored.get("short_score_user", scored["short_score"]),
            },
            "global_blockers": global_blockers,
            "long_blockers": long_blockers,
            "short_blockers": short_blockers,
        }

    def build_trade_status_html(self, payload: Dict[str, Any]) -> str:
        state = payload.get("state", "WAITING")
        summary = payload.get("summary", "Evaluating...")
        global_blockers = payload.get("global_blockers", [])
        long_blockers = payload.get("long_blockers", [])
        short_blockers = payload.get("short_blockers", [])

        state_colors = {
            "LIVE": ("#052e16", "#4ade80"),
            "READY": ("#082f49", "#38bdf8"),
            "BLOCKED": ("#450a0a", "#fca5a5"),
            "WAITING": ("#3f1d0c", "#fdba74"),
            "IDLE": ("#1f2937", "#cbd5e1"),
        }
        bg, fg = state_colors.get(state, ("#1f2937", "#e2e8f0"))
        badge = (
            f"<span style='background:{bg}; color:{fg}; "
            "padding:3px 10px; border-radius:10px; font-weight:700;'>"
            f"{escape(state)}</span>"
        )

        lines = [
            f"{badge} <span style='color:#f8fafc; font-weight:600; margin-left:8px;'>{escape(summary)}</span>"
        ]

        def add_block(title: str, color: str, reasons: List[str], max_items: int = 2):
            if not reasons:
                return
            shown = reasons[:max_items]
            lines.append(f"<span style='color:{color}; font-weight:700;'>{escape(title)}:</span>")
            for reason in shown:
                lines.append(f"<span style='color:#e2e8f0;'>- {escape(str(reason))}</span>")
            if len(reasons) > len(shown):
                lines.append(f"<span style='color:#94a3b8;'>+{len(reasons) - len(shown)} more...</span>")

        add_block("Global Blocks", "#f87171", global_blockers)
        add_block("Long Blocks", "#f59e0b", long_blockers)
        add_block("Short Blocks", "#a78bfa", short_blockers)

        if not global_blockers and not long_blockers and not short_blockers and state in {"LIVE", "READY"}:
            lines.append("<span style='color:#86efac;'>- No active blockers</span>")

        return "<br/>".join(lines)
    def build_trade_status_text(self, scored: Optional[Dict[str, Any]] = None) -> str:
        payload = self.build_trade_status_payload(scored)
        parts = [f"{payload['state']} | {payload['summary']}"]
        if payload["global_blockers"]:
            parts.append("GLOBAL: " + "; ".join(payload["global_blockers"]))
        if payload["long_blockers"]:
            parts.append("LONG: " + "; ".join(payload["long_blockers"]))
        if payload["short_blockers"]:
            parts.append("SHORT: " + "; ".join(payload["short_blockers"]))
        return " | ".join(parts)

    def refresh_psychology_tab(self):
        if not hasattr(self, "txt_psychology_eval"):
            return

        params = self.psychological_params()
        scored = self.score_current_row() if len(self.df) >= 220 else None
        evaluation = self.evaluate_psychology(scored)

        def fmt_val(v: Any) -> str:
            if isinstance(v, float):
                return f"{v:.4f}"
            return str(v)

        try:
            current_sl = max(0.0, float(self.sl.text()))
        except Exception:
            current_sl = 0.0

        max_loss_limit = self.psych_float(params, "max_loss_per_trade_pct", 1.5)
        daily_loss_limit = self.psych_float(params, "daily_loss_limit_pct", 4.0)
        drawdown_limit = self.psych_float(params, "drawdown_circuit_breaker_pct", 10.0)
        max_cons_losses = self.psych_int(params, "max_consecutive_losses", 3)
        cooldown_limit = self.psych_float(params, "post_loss_cooldown_min", 30.0)
        profit_threshold = self.psych_float(params, "profit_taking_threshold_pct", 3.0)
        trailing_stop = self.psych_float(params, "trailing_stop_pct", 1.0)
        max_positions = self.psych_int(params, "max_open_positions", 3)
        signal_age_limit = self.psych_float(params, "signal_age_limit_sec", 60.0)
        require_agree = self.psych_int(params, "require_indicators_agree", 2)
        trade_cap_limit = self.psych_int(params, "trade_cap_per_day", 10)
        news_blackout = self.psych_int(params, "news_blackout_minutes", 15)

        live_roi = evaluation["live_roi"]
        profit_reached = live_roi is not None and live_roi >= profit_threshold
        long_agree = evaluation["sides"]["long"]["agree_count"]
        short_agree = evaluation["sides"]["short"]["agree_count"]

        gates = evaluation["gates"]
        gate_state = lambda key: "PASS" if gates[key]["pass"] else "BLOCK"
        signal_age = evaluation["signal_age_sec"]
        rows = [
            ("max_loss_per_trade_pct", f"Limit {max_loss_limit:.2f}% | Current SL {current_sl:.2f}%", gate_state("max_loss_per_trade_pct")),
            ("daily_loss_limit_pct", f"Limit {daily_loss_limit:.2f}% | Current {evaluation['daily_loss_pct']:.2f}%", gate_state("daily_loss_limit_pct")),
            ("drawdown_circuit_breaker_pct", f"Limit {drawdown_limit:.2f}% | Current {evaluation['drawdown_pct']:.2f}%", gate_state("drawdown_circuit_breaker_pct")),
            ("max_consecutive_losses", f"Limit {max_cons_losses} | Current {evaluation['consecutive_losses']}", gate_state("max_consecutive_losses")),
            ("post_loss_cooldown_min", f"Limit {cooldown_limit:.1f}m | Remaining {evaluation['cooldown_remaining_min']:.1f}m", gate_state("post_loss_cooldown_min")),
            ("profit_taking_threshold_pct", f"Threshold {profit_threshold:.2f}% | Live ROI {fmt_val(live_roi) if live_roi is not None else 'NA'}%", "PASS" if profit_reached else "INFO"),
            ("trailing_stop_pct", f"Trailing {trailing_stop:.2f}% | Armed {str(profit_reached)}", "INFO"),
            ("max_open_positions", f"Limit {max_positions} | Current {evaluation['open_positions']}", gate_state("max_open_positions")),
            ("signal_age_limit_sec", f"Limit {signal_age_limit:.1f}s | Current {fmt_val(signal_age) if signal_age is not None else 'NA'}s", gate_state("signal_age_limit_sec")),
            ("require_indicators_agree", f"Need {require_agree} | Long {long_agree} | Short {short_agree}", "PASS" if (long_agree >= require_agree and short_agree >= require_agree) else "BLOCK"),
            ("trade_cap_per_day", f"Limit {trade_cap_limit} | Current {evaluation['daily_trade_count']}", gate_state("trade_cap_per_day")),
            ("news_blackout_minutes", f"Setting {news_blackout}m | News feed not connected", "INFO"),
        ]

        status_chip = {
            "PASS": ("#052e16", "#4ade80"),
            "BLOCK": ("#450a0a", "#fca5a5"),
            "INFO": ("#1e293b", "#93c5fd"),
        }

        html = [
            "<h3 style='color:#93c5fd; margin:0 0 8px 0;'>Psychology Evaluation</h3>",
            "<table width='100%' cellspacing='6' cellpadding='0'>",
        ]
        for key, details, status in rows:
            bg, fg = status_chip.get(status, ("#1e293b", "#93c5fd"))
            html.append("<tr>")
            html.append(
                "<td style='background:#111827; border:1px solid #334155; border-radius:8px; padding:8px;'>"
                f"<div style='color:#22d3ee; font-weight:700; font-size:12px;'>{escape(key)}</div>"
                f"<div style='color:#e2e8f0; font-size:13px; margin-top:2px;'>{escape(details)}</div>"
                "</td>"
            )
            html.append(
                "<td width='90' style='text-align:center;'>"
                f"<span style='background:{bg}; color:{fg}; padding:3px 10px; border-radius:10px; font-weight:700;'>{escape(status)}</span>"
                "</td>"
            )
            html.append("</tr>")
        html.append("</table>")
        self.txt_psychology_eval.setHtml("".join(html))

    def refresh_strategy_tab(self, scored: Optional[Dict[str, Any]] = None):
        if not hasattr(self, "txt_strategy_report"):
            return

        if scored is None:
            scored = self.score_current_row()
        if scored is None:
            self.txt_strategy_report.setHtml(
                "<i>No signal data available yet. Load history or wait for more candles.</i>"
            )
            return

        long_reasons = scored.get("long_reasons_user", scored.get("long_reasons", []))
        short_reasons = scored.get("short_reasons_user", scored.get("short_reasons", []))
        long_score = scored.get("long_score_user", scored.get("long_score", 0.0))
        short_score = scored.get("short_score_user", scored.get("short_score", 0.0))
        long_ready = scored.get("long_ready_user", False)
        short_ready = scored.get("short_ready_user", False)

        html = [
            f"<h3 style='color:#93c5fd; margin:0 0 10px 0;'>Strategy Run Result</h3>",
            f"<div style='color:#c7d2fe; margin-bottom:6px;'>Bot Type: <strong>{escape(self.bot_type.currentText())}</strong></div>",
            f"<div style='color:#e2e8f0; margin-bottom:8px;'>Long Score: <strong>{long_score:.2f}</strong> | Short Score: <strong>{short_score:.2f}</strong></div>",
            f"<div style='color:#38bdf8; margin-bottom:12px;'>Long Ready: <strong>{'YES' if long_ready else 'NO'}</strong> | Short Ready: <strong>{'YES' if short_ready else 'NO'}</strong></div>",
            "<table width='100%' cellspacing='6' cellpadding='0'>",
            "<tr><td width='50%' style='vertical-align:top; padding-right:8px;'>",
            "<div style='color:#22d3ee; font-weight:700; margin-bottom:4px;'>Long Conditions Met</div>",
        ]

        if long_reasons:
            html.extend([f"<div style='color:#e2e8f0; margin-bottom:2px;'>- {escape(str(item))}</div>" for item in long_reasons])
        else:
            html.append("<div style='color:#94a3b8;'>No long conditions met.</div>")

        html.extend([
            "</td><td width='50%' style='vertical-align:top; padding-left:8px;'>",
            "<div style='color:#22d3ee; font-weight:700; margin-bottom:4px;'>Short Conditions Met</div>",
        ])

        if short_reasons:
            html.extend([f"<div style='color:#e2e8f0; margin-bottom:2px;'>- {escape(str(item))}</div>" for item in short_reasons])
        else:
            html.append("<div style='color:#94a3b8;'>No short conditions met.</div>")

        html.extend(["</td></tr></table>"])
        self.txt_strategy_report.setHtml("".join(html))

    def run_strategy(self):
        scored = self.score_current_row()
        if scored is None:
            self.txt_strategy_report.setHtml(
                "<i>Cannot run strategy until sufficient candle history is loaded.</i>"
            )
            return

        report = self.build_trade_status_text(scored)
        self.refresh_strategy_tab(scored)
        self.lbl_signal.setText(f"Signal: {report}")
        self.lbl_score.setText(
            f"Signal Score | Long: {scored.get('long_score_user', scored.get('long_score', 0.0)):.2f} | "
            f"Short: {scored.get('short_score_user', scored.get('short_score', 0.0)):.2f}"
        )

    def set_demo_trade_mode(self):
        self.trade_mode = "DEMO"
        self.trade_mode_label.setText("Trading Mode: DEMO")
        self.lbl_status.setText("Trading mode set to DEMO")
        self.update_trade_mode_button()

    def set_real_trade_mode(self):
        self.trade_mode = "REAL"
        self.trade_mode_label.setText("Trading Mode: REAL")
        self.lbl_status.setText("Trading mode set to REAL")
        self.update_trade_mode_button()
        if self.api_connection_ok:
            self.refresh_real_asset_labels()

    def save_api_keys(self):
        self.saved_api_key = self.api_key_input.text().strip()
        self.saved_api_secret = self.api_secret_input.text().strip()
        if self.saved_api_key and self.saved_api_secret:
            try:
                with open(self.credentials_path(), "w", encoding="utf-8") as f:
                    f.write(f"api_key={self.saved_api_key}\n")
                    f.write(f"api_secret={self.saved_api_secret}\n")
                self.lbl_api_status.setText("API keys saved to file. Please press Test Connection.")
                self.api_connection_ok = False
            except Exception as exc:
                self.lbl_api_status.setText(f"Failed to save credentials: {exc}")
        else:
            self.lbl_api_status.setText("API keys missing or invalid. Please enter both key and secret.")
            self.api_connection_ok = False
        self.update_trade_mode_button()
        self.update_active_trading_button()
        self.refresh_trade_status_bar()

    def credentials_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "binance_credentials.txt")

    def load_saved_api_keys(self):
        path = self.credentials_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if "=" not in line:
                        continue
                    key, value = line.strip().split("=", 1)
                    if key == "api_key":
                        self.saved_api_key = value
                    elif key == "api_secret":
                        self.saved_api_secret = value
            if self.saved_api_key:
                self.api_key_input.setText(self.saved_api_key)
            if self.saved_api_secret:
                self.api_secret_input.setText(self.saved_api_secret)
            if self.saved_api_key and self.saved_api_secret:
                self.lbl_api_status.setText("Loaded saved API keys. Press Test Connection.")
        except Exception as exc:
            self.lbl_api_status.setText(f"Unable to load saved keys: {exc}")

    def sign_query(self, params: Dict[str, Any]) -> str:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        return hmac.new(self.saved_api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    def build_signed_request(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        signature = self.sign_query(params)
        params["signature"] = signature
        headers = {"X-MBX-APIKEY": self.saved_api_key}
        resp = requests.get(f"{REST_BASE}{path}", params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def test_binance_connection(self):
        self.api_details_display.clear()
        lines = []
        self.api_connection_ok = False
        try:
            exchange_resp = requests.get(f"{REST_BASE}/fapi/v1/exchangeInfo", timeout=15)
            exchange_resp.raise_for_status()
            exchange_info = exchange_resp.json()
            symbols = len(exchange_info.get("symbols", []))
            lines.append(f"Public Binance connection OK. Symbols available: {symbols}")
            if self.saved_api_key and self.saved_api_secret:
                account_info = self.build_signed_request("/fapi/v2/account")
                can_trade = account_info.get("canTrade", False)
                positions = account_info.get("positions", [])
                positions_open = sum(1 for pos in positions if float(pos.get("positionAmt", 0)) != 0)
                self.real_wallet_balance = float(account_info.get("totalWalletBalance", 0.0))
                self.real_available_balance = float(account_info.get("availableBalance", account_info.get("availableMargin", 0.0)))
                self.real_margin_balance = float(account_info.get("totalMarginBalance", 0.0))
                self.real_unrealized_profit = float(account_info.get("totalUnrealizedProfit", 0.0))
                lines.append(f"Authenticated account OK. canTrade={can_trade}, open positions={positions_open}")
                if self.real_wallet_balance is not None:
                    lines.append(f"Wallet balance: {self.real_wallet_balance:.4f} USDT")
                lines.append(f"Available margin: {self.real_available_balance:.4f} USDT")
                self.api_connection_ok = True
                assets = [a for a in account_info.get("assets", []) if float(a.get("walletBalance", 0)) != 0]
                if assets:
                    lines.append("Non-zero asset balances:")
                    for asset in assets[:6]:
                        lines.append(f"  {asset.get('asset')}: {float(asset.get('walletBalance',0)):.4f}")
            else:
                lines.append("No API credentials available. Only public data fetched.")
                self.api_connection_ok = False
            self.lbl_api_status.setText("Binance test connection completed.")
            if self.trade_mode == "REAL":
                self.refresh_real_asset_labels()
        except Exception as exc:
            lines.append(f"Connection failed: {exc}")
            self.lbl_api_status.setText(f"Connection failed: {exc}")
            self.api_connection_ok = False
        self.api_details_display.setPlainText("\n".join(lines))
        self.update_trade_mode_button()
        self.update_active_trading_button()

    def toggle_active_trading(self):
        if self.trade_mode == "REAL" and not self.api_connection_ok:
            self.lbl_api_status.setText("Unable to enable active trading: real mode requires a successful Binance connection.")
            return
        self.active_trading_enabled = not self.active_trading_enabled
        self.lbl_active_trading.setText(
            "Active Trading: ON" if self.active_trading_enabled else "Active Trading: OFF"
        )
        self.btn_active_trading.setText(
            "Disable Active Trading" if self.active_trading_enabled else "Enable Active Trading"
        )
        self.lbl_api_status.setText(
            "Active trading enabled." if self.active_trading_enabled else "Active trading disabled."
        )

    def update_trade_mode_button(self):
        if self.trade_mode == "DEMO":
            self.btn_toggle_trade_mode.setText("Switch to REAL Trade")
        else:
            self.btn_toggle_trade_mode.setText("Switch to DEMO Trade")
        self.btn_toggle_trade_mode.setEnabled(
            (self.trade_mode == "DEMO" and bool(self.saved_api_key and self.saved_api_secret and self.api_connection_ok))
            or self.trade_mode == "REAL"
        )
        self.update_real_mode_styles()

    def update_real_mode_styles(self):
        if self.trade_mode == "REAL":
            style = "background:#7f1d1d; color:#fde2e2; border:1px solid #f87171;"
        else:
            style = ""
        for btn in [
            self.btn_apply,
            self.btn_refresh,
            self.btn_auto_on,
            self.btn_auto_off,
            self.btn_long,
            self.btn_short,
            self.btn_toggle_trade_mode,
            self.btn_close,
            self.btn_reset_stats,
            self.btn_save_api,
            self.btn_test_connection,
            self.btn_active_trading,
            self.btn_run_strategy,
        ]:
            btn.setStyleSheet(style)

    def refresh_real_asset_labels(self):
        self.lbl_balance.setText(f"Total Balance: {self.real_wallet_balance:.2f} USDT")
        self.lbl_available.setText(f"Available: {self.real_available_balance:.2f} USDT")
        self.lbl_equity.setText(f"Equity: {self.real_margin_balance:.2f} USDT")
        self.lbl_live_pnl.setText(f"Unrealized PnL: {self.real_unrealized_profit:.2f} USDT")

    def play_click_sound(self):
        try:
            if hasattr(winsound, 'MessageBeep'):
                winsound.MessageBeep(winsound.MB_OK)
        except Exception:
            pass

    def update_active_trading_button(self):
        self.btn_active_trading.setEnabled(
            self.trade_mode == "DEMO" or self.api_connection_ok
        )

    def toggle_trade_mode(self):
        if self.trade_mode == "DEMO":
            if not (self.saved_api_key and self.saved_api_secret):
                self.lbl_api_status.setText("Enter and save Binance API keys before switching to REAL mode.")
                return
            if not self.api_connection_ok:
                self.lbl_api_status.setText("Please test Binance connection before switching to REAL mode.")
                return
            self.set_real_trade_mode()
        else:
            self.set_demo_trade_mode()

    def marker_time(self, ts: pd.Timestamp):
        return int(ts.timestamp())

    def current_live_roi_pct(self, side: str, entry_price: float, current_price: float, leverage: float, fee_rate: float) -> float:
        if entry_price <= 0:
            return 0.0
        if side == "long":
            price_move_pct = (current_price - entry_price) / entry_price
        else:
            price_move_pct = (entry_price - current_price) / entry_price
        gross_roi_pct = price_move_pct * leverage * 100.0
        fee_roi_pct = 2.0 * fee_rate * leverage * 100.0
        return gross_roi_pct - fee_roi_pct

    def build_chart_payload(self):
        visible = min(len(self.df), int(self.graph_range.currentText()))
        plot_df = self.df.iloc[-visible:].copy()

        candles, e20, e50, e200 = [], [], [], []
        for _, row in plot_df.iterrows():
            t = self.marker_time(row["open_time"])
            candles.append({
                "time": t,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
            if pd.notna(row["ema20"]):
                e20.append({"time": t, "value": float(row["ema20"])})
            if pd.notna(row["ema50"]):
                e50.append({"time": t, "value": float(row["ema50"])})
            if pd.notna(row["ema200"]):
                e200.append({"time": t, "value": float(row["ema200"])})

        return candles, e20, e50, e200

    def sync_chart_full(self):
        if self.df.empty:
            return
        if not self.chart_ready:
            self.pending_full_sync = True
            return

        candles, e20, e50, e200 = self.build_chart_payload()
        auto_follow = "true" if self.auto_follow.isChecked() else "false"

        self.js_chart(
            f"window.chartApi.setAutoFollow({auto_follow});"
            f"window.chartApi.setAll({json.dumps(candles)}, {json.dumps(e20)}, {json.dumps(e50)}, {json.dumps(e200)}, {json.dumps(self.current_markers()[-300:])});"
        )

    def sync_chart_update(self):
        if self.df.empty or not self.chart_ready:
            return

        row = self.df.iloc[-1]
        bar = {
            "time": self.marker_time(row["open_time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        e20v = float(row["ema20"]) if pd.notna(row["ema20"]) else None
        e50v = float(row["ema50"]) if pd.notna(row["ema50"]) else None
        e200v = float(row["ema200"]) if pd.notna(row["ema200"]) else None
        auto_follow = "true" if self.auto_follow.isChecked() else "false"

        self.js_chart(
            f"window.chartApi.setAutoFollow({auto_follow});"
            f"window.chartApi.updateOne({json.dumps(bar)}, {json.dumps(e20v)}, {json.dumps(e50v)}, {json.dumps(e200v)}, {json.dumps(self.current_markers()[-300:])});"
        )

    def selected_long_checks(self):
        return {k: a.isChecked() for k, a in self.long_actions.items()}

    def selected_short_checks(self):
        return {k: a.isChecked() for k, a in self.short_actions.items()}

    def score_current_row(self):
        if len(self.df) < 220:
            return None

        if "ema20" not in self.df.columns or "ema50" not in self.df.columns or "ema200" not in self.df.columns:
            self.df = compute_indicators(self.df)

        scored = score_row(self.df, len(self.df) - 1)
        long_selected = self.selected_long_checks()
        short_selected = self.selected_short_checks()

        long_checks = scored["long_checks"]
        short_checks = scored["short_checks"]

        long_selected_keys = [k for k, v in long_selected.items() if v]
        short_selected_keys = [k for k, v in short_selected.items() if v]

        long_filtered = [k for k in long_selected_keys if long_checks.get(k, False)]
        short_filtered = [k for k in short_selected_keys if short_checks.get(k, False)]

        long_selected_score = round(
            (100.0 * len(long_filtered) / len(long_selected_keys)),
            2
        ) if long_selected_keys else 0.0
        short_selected_score = round(
            (100.0 * len(short_filtered) / len(short_selected_keys)),
            2
        ) if short_selected_keys else 0.0

        long_ready = len(long_selected_keys) > 0 and long_selected_score >= self.min_score_val()
        short_ready = len(short_selected_keys) > 0 and short_selected_score >= self.min_score_val()

        scored["long_ready_user"] = long_ready
        scored["short_ready_user"] = short_ready
        scored["long_score_user"] = long_selected_score
        scored["short_score_user"] = short_selected_score
        scored["long_reasons_user"] = long_filtered
        scored["short_reasons_user"] = short_filtered
        return scored

    def get_entry_snapshot(self, side: str, scored: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
        if scored is None:
            scored = self.score_current_row()
        if scored is None:
            return {"true_signals": [], "false_signals": []}

        if side == "long":
            selected = self.selected_long_checks()
            checks = scored["long_checks"]
            prefix = "LONG | "
        else:
            selected = self.selected_short_checks()
            checks = scored["short_checks"]
            prefix = "SHORT | "

        true_signals, false_signals = [], []
        for key, enabled in selected.items():
            if not enabled:
                continue
            full = prefix + key
            if checks.get(key, False):
                true_signals.append(full)
            else:
                false_signals.append(full)

        return {"true_signals": true_signals, "false_signals": false_signals}

    def load_history(self):
        symbol = self.current_symbol_key()
        interval = self.interval.currentText().strip()
        self.lbl_status.setText(f"Loading {symbol} {interval}...")
        self.ensure_daily_stats_current()

        if self.history_worker is not None and self.history_worker.isRunning():
            return

        self.history_worker = HistoryLoadWorker(symbol, interval, HISTORY_LIMIT)
        self.history_worker.loaded.connect(self.on_history_loaded)
        self.history_worker.error.connect(self.on_history_error)
        self.history_worker.finished.connect(self.history_worker.deleteLater)
        self.history_worker.start()

    def on_history_loaded(self, df: pd.DataFrame):
        self.history_worker = None
        self.df = df
        self.last_closed_candle_time = self.df.iloc[-1]["open_time"] if not self.df.empty else None

        if not self.closed_trades and not self.positions_by_symbol:
            self.balance_total = float(self.start_balance.text())
            self.available_balance = self.balance_total
            self.equity = self.balance_total
            self.session_peak_balance = self.balance_total
            self.daily_start_balance = self.balance_total
            self.daily_trade_count = 0
            self.daily_net_pnl = 0.0
            self.consecutive_losses = 0
            self.last_loss_time = None

        self.lbl_status.setText(f"Market stream live: {self.current_symbol_key()} {self.interval.currentText().strip()}")
        self.lbl_signal.setText("Signal: Waiting")
        self.refresh_panels()
        self.sync_chart_full()
        self.start_analytics_worker(force=True)

    def on_history_error(self, message: str):
        self.history_worker = None
        self.lbl_status.setText("History load failed")
        self.lbl_signal.setText(f"Signal: Error")
        self.api_details_display.setPlainText(f"History load error: {message}")
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "history_error.log"), "a", encoding="utf-8") as f:
                f.write("\n--- HistoryLoadWorker error ---\n")
                f.write(message)
                f.write("\n")
        except Exception:
            pass

    def start_market(self):
        self.stop_market()
        self.load_history()

        symbol = self.current_symbol_key().lower()
        interval = self.interval.currentText().strip()
        self.current_symbol = symbol
        self.current_interval = interval

        self.feed = BinanceFeed(symbol, interval, self.queue)
        self.feed.start()

        self.depth_feed = BinanceDepthFeed(symbol, self.queue, levels=20)
        self.depth_feed.start()

        self.running = True
        self.lbl_status.setText(f"Market stream live: {symbol.upper()} {interval}")

    def restart_market(self):
        self.start_market()

    def stop_market(self):
        self.running = False
        if self.feed:
            self.feed.stop()
            self.feed = None
        if self.depth_feed:
            self.depth_feed.stop()
            self.depth_feed = None

        self.current_symbol = None
        self.current_interval = None
        self.depth_history.clear()
        self.heatmap_acc = OrderBookHeatmapAccumulator(max_snapshots=250)
        self.last_heatmap_push_ts = 0.0
        self.last_analytics_start_ts = 0.0

    def start_auto(self):
        self.auto_enabled = True
        self.lbl_auto.setText("Auto Trading: ON")
        self.refresh_panels()

    def stop_auto(self):
        self.auto_enabled = False
        self.lbl_auto.setText("Auto Trading: OFF")
        self.refresh_panels()

    def manual_long(self):
        if self.df.empty or self.current_position() is not None:
            return
        score = self.score_current_row()
        entry_score = score.get("long_score_user", score["long_score"]) if score else 0.0
        snapshot = self.get_entry_snapshot("long", score)
        self.enter_position("long", float(self.df.iloc[-1]["close"]), self.df.iloc[-1]["open_time"], "manual", entry_score, snapshot)

    def manual_short(self):
        if self.df.empty or self.current_position() is not None:
            return
        score = self.score_current_row()
        entry_score = score.get("short_score_user", score["short_score"]) if score else 0.0
        snapshot = self.get_entry_snapshot("short", score)
        self.enter_position("short", float(self.df.iloc[-1]["close"]), self.df.iloc[-1]["open_time"], "manual", entry_score, snapshot)

    def manual_close(self):
        if self.df.empty or self.current_position() is None:
            return
        self.exit_position("manual_close", float(self.df.iloc[-1]["close"]), self.df.iloc[-1]["open_time"])

    def enter_position(self, side: str, price: float, tstamp: pd.Timestamp, source: str, score_at_entry: float = 0.0, entry_snapshot: Optional[Dict[str, List[str]]] = None):
        self.ensure_daily_stats_current(self.to_local_ts(tstamp))
        capital = self.capital_per_trade()
        if capital > self.available_balance:
            self.lbl_status.setText("Not enough available balance")
            self.refresh_trade_status_bar()
            self.refresh_status_overview()
            return

        symbol = self.current_symbol_key()
        leverage = self.leverage_val()
        notional = capital * leverage
        qty = notional / price
        entry_fee = notional * self.fee_rate()

        if entry_snapshot is None:
            entry_snapshot = {"true_signals": [], "false_signals": []}

        pos = Position(
            symbol=symbol,
            side=side,
            entry_time=tstamp,
            entry_price=price,
            capital_usdt=capital,
            leverage=leverage,
            notional_usdt=notional,
            qty=qty,
            entry_fee=entry_fee,
            bars_held=0,
            source=f"{source}-{self.trade_mode.lower()}",
            score_at_entry=score_at_entry,
            entry_signals=entry_snapshot["true_signals"],
            entry_snapshot=entry_snapshot,
        )

        self.available_balance -= capital
        self.daily_trade_count += 1
        self.set_current_position(pos)

        markers = self.current_markers()
        markers.append({
            "time": self.marker_time(tstamp),
            "position": "belowBar" if side == "long" else "aboveBar",
            "color": "#2563eb" if source == "manual" else "#f59e0b",
            "shape": "arrowUp" if side == "long" else "arrowDown",
            "text": f"{source[:1].upper()}-{side[:1].upper()} {score_at_entry:.0f}",
        })
        self.set_current_markers(markers)

        self.update_equity()
        self.refresh_panels()
        self.sync_chart_full()

    def update_signal_stats_from_trade(self, trade: ClosedTrade, entry_snapshot: Dict[str, List[str]]):
        true_signals = entry_snapshot.get("true_signals", [])
        false_signals = entry_snapshot.get("false_signals", [])
        n_true = max(1, len(true_signals))
        n_false = max(1, len(false_signals))
        roi_mag = abs(trade.roi_pct)
        pnl_mag = abs(trade.net_pnl)
        won = trade.net_pnl > 0
        lost = trade.net_pnl < 0

        if won:
            true_reward = roi_mag / n_true
            false_penalty = (roi_mag * 0.50) / n_false
            for sig in true_signals:
                s = self.signal_stats.setdefault(sig, self._new_signal_stat())
                s["participated"] += 1
                s["true_count"] += 1
                s["wins"] += 1
                s["score"] += true_reward
                s["net_pnl_contrib"] += pnl_mag / n_true
                s["sum_roi_contrib"] += true_reward
            for sig in false_signals:
                s = self.signal_stats.setdefault(sig, self._new_signal_stat())
                s["participated"] += 1
                s["false_count"] += 1
                s["score"] -= false_penalty
                s["net_pnl_contrib"] -= (pnl_mag * 0.50) / n_false
                s["sum_roi_contrib"] -= false_penalty

        elif lost:
            true_penalty = roi_mag / n_true
            false_reward = (roi_mag * 0.25) / n_false
            for sig in true_signals:
                s = self.signal_stats.setdefault(sig, self._new_signal_stat())
                s["participated"] += 1
                s["true_count"] += 1
                s["losses"] += 1
                s["score"] -= true_penalty
                s["net_pnl_contrib"] -= pnl_mag / n_true
                s["sum_roi_contrib"] -= true_penalty
            for sig in false_signals:
                s = self.signal_stats.setdefault(sig, self._new_signal_stat())
                s["participated"] += 1
                s["false_count"] += 1
                s["score"] += false_reward
                s["net_pnl_contrib"] += (pnl_mag * 0.25) / n_false
                s["sum_roi_contrib"] += false_reward

        for sig in set(true_signals + false_signals):
            s = self.signal_stats[sig]
            if s["participated"] > 0:
                s["avg_score"] = s["score"] / s["participated"]
                s["power"] = s["score"]

    def exit_position(self, reason: str, exit_price: float, tstamp: pd.Timestamp):
        pos = self.current_position()
        if pos is None:
            return

        self.ensure_daily_stats_current(self.to_local_ts(tstamp))

        gross_pnl = ((exit_price - pos.entry_price) * pos.qty) if pos.side == "long" else ((pos.entry_price - exit_price) * pos.qty)
        exit_fee = pos.notional_usdt * self.fee_rate()
        total_fees = pos.entry_fee + exit_fee
        net_pnl = gross_pnl - total_fees
        roi_pct = (net_pnl / pos.capital_usdt) * 100.0 if pos.capital_usdt > 0 else 0.0

        self.available_balance += pos.capital_usdt
        self.balance_total += net_pnl
        self.available_balance += net_pnl
        self.daily_net_pnl += net_pnl
        self.session_peak_balance = max(self.session_peak_balance, self.balance_total)
        if net_pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_time = tstamp
        elif net_pnl > 0:
            self.consecutive_losses = 0

        markers = self.current_markers()
        markers.append({
            "time": self.marker_time(tstamp),
            "position": "aboveBar" if pos.side == "long" else "belowBar",
            "color": "#ef4444",
            "shape": "circle",
            "text": reason,
        })
        self.set_current_markers(markers)

        entry_snapshot = getattr(pos, "entry_snapshot", {"true_signals": [], "false_signals": []})
        trade = ClosedTrade(
            symbol=pos.symbol,
            side=pos.side,
            source=pos.source,
            entry_time=str(pos.entry_time),
            exit_time=str(tstamp),
            entry_price=pos.entry_price,
            exit_price=exit_price,
            capital_usdt=pos.capital_usdt,
            leverage=pos.leverage,
            qty=pos.qty,
            gross_pnl=gross_pnl,
            fees=total_fees,
            net_pnl=net_pnl,
            roi_pct=roi_pct,
            exit_reason=reason,
            hold_bars=pos.bars_held,
            score_at_entry=pos.score_at_entry,
            entry_signals=entry_snapshot.get("true_signals", []),
        )

        self.closed_trades.append(trade)
        self.update_signal_stats_from_trade(trade, entry_snapshot)

        self.set_current_position(None)
        self.update_equity()
        self.refresh_panels()
        self.sync_chart_full()

    def reset_signal_stats(self):
        self.signal_stats = {}
        for key in self.LONG_KEYS:
            self.signal_stats[f"LONG | {key}"] = self._new_signal_stat()
        for key in self.SHORT_KEYS:
            self.signal_stats[f"SHORT | {key}"] = self._new_signal_stat()
        self.refresh_signal_stats_tab()

    def refresh_trade_tab(self):
        open_lines = []
        for sym, pos in self.positions_by_symbol.items():
            snap = getattr(pos, "entry_snapshot", {"true_signals": [], "false_signals": []})
            open_lines.append(
                f"{sym} | {pos.side.upper()} | {pos.source.upper()} | "
                f"Entry={pos.entry_price:.4f} | Capital={pos.capital_usdt:.2f} | "
                f"Lev={pos.leverage:.1f}x | Qty={pos.qty:.6f} | Bars={pos.bars_held} | "
                f"Score={pos.score_at_entry:.2f} | "
                f"TRUE={', '.join(snap.get('true_signals', []))} | "
                f"FALSE={', '.join(snap.get('false_signals', []))}"
            )
        if not open_lines:
            open_lines = ["No open trades"]

        closed_lines = []
        for t in reversed(self.closed_trades[-300:]):
            closed_lines.append(
                f"{t.symbol} | {t.side.upper()} | {t.source.upper()} | "
                f"Entry={t.entry_price:.4f} | Exit={t.exit_price:.4f} | "
                f"PnL={t.net_pnl:.4f} | ROI={t.roi_pct:.2f}% | "
                f"Reason={t.exit_reason} | Score={t.score_at_entry:.2f} | "
                f"TRUE={', '.join(t.entry_signals)}"
            )
        if not closed_lines:
            closed_lines = ["No closed trades yet"]

        self.set_text_preserve_scroll(self.txt_open_trades, "\n".join(open_lines))
        self.set_text_preserve_scroll(self.txt_closed_trades, "\n".join(closed_lines))

    def refresh_signal_stats_tab(self):
        items = sorted(
            self.signal_stats.items(),
            key=lambda kv: (kv[1]["score"], kv[1]["avg_score"], kv[1]["participated"]),
            reverse=True,
        )

        self.tbl_signal_stats.setRowCount(len(items))
        for r, (sig, s) in enumerate(items):
            side = "LONG" if sig.startswith("LONG | ") else ("SHORT" if sig.startswith("SHORT | ") else "-")
            condition = sig.split(" | ", 1)[1] if " | " in sig else sig
            row_vals = [
                side,
                condition,
                str(s["participated"]),
                str(s["true_count"]),
                str(s["false_count"]),
                str(s["wins"]),
                str(s["losses"]),
                f"{s['score']:.2f}",
                f"{s['avg_score']:.2f}",
                f"{s['net_pnl_contrib']:.2f}",
                f"{s.get('power', 0.0):.2f}",
            ]
            for c, val in enumerate(row_vals):
                item = QTableWidgetItem(val)
                item.setToolTip(str(val))
                if c > 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0:
                    if side == "LONG":
                        item.setForeground(QBrush(QColor("#22d3ee")))
                    elif side == "SHORT":
                        item.setForeground(QBrush(QColor("#c084fc")))
                if c in (7, 8, 9, 10):
                    num = float(val)
                    if num > 0:
                        item.setForeground(QBrush(QColor("#22c55e")))
                    elif num < 0:
                        item.setForeground(QBrush(QColor("#ef4444")))
                self.tbl_signal_stats.setItem(r, c, item)

    def update_equity(self):
        pos = self.current_position()
        if self.df.empty:
            return

        if pos is None:
            self.equity = self.balance_total
            self.lbl_live_pnl.setText("Live Position PnL: 0.00 | ROI: 0.00%")
        else:
            last_price = float(self.df.iloc[-1]["close"])
            if pos.side == "long":
                unreal = (last_price - pos.entry_price) * pos.qty
            else:
                unreal = (pos.entry_price - last_price) * pos.qty

            close_fee = pos.notional_usdt * self.fee_rate()
            live_net = unreal - pos.entry_fee - close_fee
            live_roi = (live_net / pos.capital_usdt) * 100.0 if pos.capital_usdt > 0 else 0.0

            self.equity = self.available_balance + pos.capital_usdt + live_net
            self.lbl_live_pnl.setText(f"Live Position PnL: {live_net:.4f} | ROI: {live_roi:.2f}%")

        self.lbl_balance.setText(f"Total Balance: {self.balance_total:.2f}")
        self.lbl_available.setText(f"Available Balance: {self.available_balance:.2f}")
        self.lbl_equity.setText(f"Equity: {self.equity:.2f}")

    def strategy_checklist(self) -> str:
        if len(self.df) < 220:
            return "Waiting for more candles..."

        scored = self.score_current_row()
        if scored is None:
            return "Waiting for more candles..."

        long_selected = self.selected_long_checks()
        short_selected = self.selected_short_checks()
        raw = score_row(self.df, len(self.df) - 1)

        def fmt(title, checks, selected, score_val, ready_user):
            lines = [title]
            for k, v in checks.items():
                sel = "[ON]" if selected.get(k, False) else "[OFF]"
                mark = "✓" if v else "✗"
                lines.append(f"{sel} {mark} {k}")
            lines.append(f"Weighted Score: {score_val:.2f}")
            lines.append(f"Ready: {'YES' if ready_user else 'NO'}")
            return "\n".join(lines)

        return (
            fmt("LONG Setup", raw["long_checks"], long_selected, raw["long_score"], scored["long_ready_user"])
            + "\n\n" +
            fmt("SHORT Setup", raw["short_checks"], short_selected, raw["short_score"], scored["short_ready_user"])
        )

    def build_checklist_html(self, scored: Optional[Dict[str, Any]] = None) -> str:
        if len(self.df) < 220:
            return "<span style='color:#fbbf24; font-weight:700;'>Waiting for more candles...</span>"

        if scored is None:
            scored = self.score_current_row()
        if scored is None:
            return "<span style='color:#fbbf24; font-weight:700;'>Signal data not ready...</span>"

        raw = score_row(self.df, len(self.df) - 1)
        long_selected = self.selected_long_checks()
        short_selected = self.selected_short_checks()

        def section_html(
            title: str,
            checks: Dict[str, bool],
            selected: Dict[str, bool],
            score_val: float,
            ready_user: bool,
            side_color: str,
        ) -> str:
            rows = []
            for key, ok_raw in checks.items():
                enabled = bool(selected.get(key, False))
                ok = bool(checks.get(key, False))
                state = "PASS" if ok else "FAIL"
                state_color = "#22c55e" if ok else "#ef4444"
                enabled_text = "ON" if enabled else "OFF"
                enabled_color = "#38bdf8" if enabled else "#64748b"
                rows.append(
                    f"<div style='margin:1px 0;'>"
                    f"<span style='display:inline-block; min-width:34px; margin-right:6px; color:{enabled_color}; font-weight:700;'>{enabled_text}</span>"
                    f"<span style='display:inline-block; margin-right:6px; color:#475569;'>|</span>"
                    f"<span style='display:inline-block; min-width:44px; margin-right:8px; color:{state_color}; font-weight:700;'>{state}</span>"
                    f"<span style='display:inline-block; margin-right:8px; color:#475569;'>|</span>"
                    f"<span style='color:#e2e8f0;'>{escape(key)}</span>"
                    f"</div>"
                )

            if not rows:
                rows.append("<div style='color:#94a3b8;'>No checks available</div>")

            ready = "YES" if ready_user else "NO"
            ready_color = "#22c55e" if ready_user else "#ef4444"
            header = (
                "<table width='100%' cellspacing='0' cellpadding='0' style='margin-top:4px; margin-bottom:6px;'>"
                "<tr>"
                f"<td style='color:{side_color}; font-weight:800; white-space:nowrap;'>{escape(title)}</td>"
                f"<td align='right' style='color:{side_color}; font-weight:800; white-space:nowrap;'>Score {score_val:.2f}</td>"
                f"<td align='right' style='color:{side_color}; font-weight:800; white-space:nowrap;'>Ready&nbsp;<span style='color:{ready_color};'>{ready}</span></td>"
                "</tr>"
                "</table>"
            )
            return (
                "<div style='background:#0b1731; border:1px solid #334155; border-radius:8px; padding:8px;'>"
                + header + "".join(rows) +
                "</div>"
            )

        long_html = section_html(
            "LONG Setup",
            raw["long_checks"],
            long_selected,
            scored.get("long_score_user", raw["long_score"]),
            scored["long_ready_user"],
            "#38bdf8",
        )
        short_html = section_html(
            "SHORT Setup",
            raw["short_checks"],
            short_selected,
            scored.get("short_score_user", raw["short_score"]),
            scored["short_ready_user"],
            "#c084fc",
        )

        return (
            "<table width='100%' cellspacing='8' cellpadding='0'>"
            "<tr>"
            f"<td width='50%' valign='top'>{long_html}</td>"
            f"<td width='50%' valign='top'>{short_html}</td>"
            "</tr>"
            "</table>"
        )

    def refresh_panels(self):
        if self.df.empty:
            return

        self.ensure_daily_stats_current()
        row = self.df.iloc[-1]
        pos = self.current_position()
        scored = self.score_current_row()

        if pos:
            self.lbl_position.setText(
                f"Position: {pos.side.upper()} | {pos.source.upper()} | Entry {pos.entry_price:.4f} | Lev {pos.leverage:.1f}x"
            )
        else:
            self.lbl_position.setText("Position: Flat")

        if scored:
            self.lbl_score.setText(
                f"Signal Score | Long: {scored.get('long_score_user', scored['long_score']):.2f} | "
                f"Short: {scored.get('short_score_user', scored['short_score']):.2f}"
            )
        else:
            self.lbl_score.setText("Signal Score | Long: 0.00 | Short: 0.00")
        self.refresh_trade_status_bar(scored)

        def v(x, digits=6):
            try:
                if pd.isna(x):
                    return "NA"
                return f"{float(x):.{digits}f}"
            except Exception:
                return str(x)

        indicator_rows = [
            ("Symbol", self.current_symbol_key()),
            ("Open", v(row["open"])),
            ("High", v(row["high"])),
            ("Low", v(row["low"])),
            ("Close", v(row["close"])),
            ("EMA20", v(row["ema20"])),
            ("EMA50", v(row["ema50"])),
            ("EMA200", v(row["ema200"])),
            ("RSI14", v(row["rsi14"], 2)),
            ("MACD", v(row["macd"])),
            ("Signal", v(row["macd_signal"])),
            ("Hist", v(row["macd_hist"])),
            ("ATR14", v(row["atr14"])),
            ("VRVP POC", v(row.get("vrvp_poc"))),
            ("VRVP VAH", v(row.get("vrvp_vah"))),
            ("VRVP VAL", v(row.get("vrvp_val"))),
            ("Session POC", v(row.get("session_poc"))),
            ("Session VAH", v(row.get("session_vah"))),
            ("Session VAL", v(row.get("session_val"))),
            ("Agg Delta Ratio", v(row.get("agg_delta_ratio"), 4)),
            ("Book Imbalance", v(row.get("book_imbalance"), 4)),
            ("Spread bps", v(row.get("spread_bps"), 4)),
            ("Microprice", v(row.get("microprice"))),
            ("Bullish VSA", str(bool(row.get("vsa_bullish", False)))),
            ("Bearish VSA", str(bool(row.get("vsa_bearish", False)))),
            ("TP ROI%", self.tp.text()),
            ("SL ROI%", self.sl.text()),
        ]

        ind_text = "\n".join([f"{k:<16}: {val}" for k, val in indicator_rows])
        self.indicator_text_cache = ind_text

        html = [
            "<h3 style='color:#93c5fd; margin:0 0 8px 0;'>Indicator Dashboard</h3>",
            "<table width='100%' cellspacing='6' cellpadding='0'>",
        ]
        for i in range(0, len(indicator_rows), 3):
            html.append("<tr>")
            row_triplet = indicator_rows[i:i + 3]
            for key, val in row_triplet:
                html.append(
                    "<td style='background:#111827; border:1px solid #334155; border-radius:8px; padding:8px;'>"
                    f"<div style='color:#22d3ee; font-size:12px; font-weight:700;'>{escape(str(key))}</div>"
                    f"<div style='color:#e2e8f0; font-size:14px; margin-top:2px;'>{escape(str(val))}</div>"
                    "</td>"
                )
            if len(row_triplet) < 3:
                for _ in range(3 - len(row_triplet)):
                    html.append("<td></td>")
            html.append("</tr>")
        html.append("</table>")
        self.txt_indicators_tab.setHtml("".join(html))
        self.lbl_checklist.setText(self.build_checklist_html(scored))
        self.refresh_trade_tab()
        self.refresh_signal_stats_tab()
        self.refresh_psychology_tab()
        self.refresh_strategy_tab(scored)

    def check_exit(self, pos: Position, row: pd.Series):
        tp_pct = float(self.tp.text())
        sl_pct = float(self.sl.text())
        fee_rate = self.fee_rate()

        high_price = float(row["high"])
        low_price = float(row["low"])
        close_price = float(row["close"])

        if pos.side == "long":
            best_roi = self.current_live_roi_pct(pos.side, pos.entry_price, high_price, pos.leverage, fee_rate)
            worst_roi = self.current_live_roi_pct(pos.side, pos.entry_price, low_price, pos.leverage, fee_rate)
        else:
            best_roi = self.current_live_roi_pct(pos.side, pos.entry_price, low_price, pos.leverage, fee_rate)
            worst_roi = self.current_live_roi_pct(pos.side, pos.entry_price, high_price, pos.leverage, fee_rate)

        if best_roi >= tp_pct:
            return "take_profit", close_price
        if worst_roi <= -sl_pct:
            return "stop_loss", close_price
        if pos.bars_held >= self.max_hold_bars_val():
            return "time_exit", close_price
        return None, None

    def check_intrabar_exit(self):
        pos = self.current_position()
        if pos is None or self.df.empty:
            return

        row = self.df.iloc[-1]
        tp_pct = float(self.tp.text())
        sl_pct = float(self.sl.text())
        fee_rate = self.fee_rate()

        last_price = float(row["close"])
        live_roi = self.current_live_roi_pct(pos.side, pos.entry_price, last_price, pos.leverage, fee_rate)

        if live_roi >= tp_pct:
            self.exit_position("take_profit", last_price, row["open_time"])
            return
        if live_roi <= -sl_pct:
            self.exit_position("stop_loss", last_price, row["open_time"])
            return

    def apply_tick(self, row: dict):
        if row.get("symbol") and self.current_symbol and row["symbol"].lower() != self.current_symbol:
            return

        base = {
            "open_time": row["open_time"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "close_time": row["close_time"],
            "is_closed": row["is_closed"],
        }

        if self.df.empty:
            self.df = compute_indicators(pd.DataFrame([base]))
            self.refresh_trade_status_bar()
            return

        if self.df.iloc[-1]["open_time"] == row["open_time"]:
            for k, v in base.items():
                self.df.at[self.df.index[-1], k] = v
        elif row["open_time"] > self.df.iloc[-1]["open_time"]:
            self.df = pd.concat([self.df, pd.DataFrame([base])], ignore_index=True)
            if len(self.df) > MAX_POINTS:
                self.df = self.df.iloc[-MAX_POINTS:].copy()

        tail_keep = 420
        if len(self.df) > tail_keep:
            prefix = self.df.iloc[:-tail_keep].copy()
            tail = self.df.iloc[-tail_keep:].copy()
            tail = compute_indicators(tail)
            self.df = pd.concat([prefix, tail], ignore_index=True)
        else:
            self.df = compute_indicators(self.df)

        if bool(row["is_closed"]):
            closed_time = row["open_time"]
            if self.last_closed_candle_time is None or closed_time > self.last_closed_candle_time:
                self.last_closed_candle_time = closed_time
                self.on_closed_candle()

        self.update_equity()
        self.check_intrabar_exit()
        self.refresh_trade_status_bar()

    def handle_depth(self, item: Dict[str, Any]):
        if item.get("symbol") != self.current_symbol_key():
            return

        bids = item.get("bids", [])
        asks = item.get("asks", [])
        if not bids or not asks:
            return

        try:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            mid = (best_bid + best_ask) / 2.0
            spread_bps = ((best_ask - best_bid) / max(1e-12, mid)) * 10000.0

            bid_vol = sum(float(q) for _, q in bids[:20])
            ask_vol = sum(float(q) for _, q in asks[:20])
            book_imb = (bid_vol - ask_vol) / max(1e-12, bid_vol + ask_vol)

            self.latest_book_meta = {
                "mid_price": round(mid, 4),
                "spread_bps": round(spread_bps, 4),
                "book_imbalance": round(book_imb, 4),
            }

            self.heatmap_acc.update_from_depth(bids, asks, price_round=2)

            levels = []
            for p, q in bids[:20]:
                levels.append({"price": round(float(p), 2), "heat": float(q), "side": "bid"})
            for p, q in asks[:20]:
                levels.append({"price": round(float(p), 2), "heat": float(q), "side": "ask"})

            self.depth_history.append({
                "ts": item.get("ts"),
                "mid_price": round(mid, 4),
                "levels": levels,
            })

            if len(self.depth_history) > self.max_depth_history:
                self.depth_history = self.depth_history[-self.max_depth_history:]

            now = time.time()
            if now - self.last_heatmap_push_ts >= self.heatmap_push_interval_sec:
                self.last_heatmap_push_ts = now
                self.push_heatmap()

        except Exception:
            pass

    def start_analytics_worker(self, force: bool = False):
        if self.df.empty:
            return

        now = time.time()
        if not force and (now - self.last_analytics_start_ts < self.analytics_interval_sec):
            return
        if self.worker is not None and self.worker.isRunning():
            return

        self.last_analytics_start_ts = now
        self.worker = AnalyticsWorker(self.current_symbol_key(), self.df.tail(320).copy())
        self.worker.data_ready.connect(self.on_analytics_ready)
        self.worker.start()

    def on_analytics_ready(self, data: Dict[str, Any]):
        try:
            if "profile" in data:
                self.latest_profile_payload = data["profile"]
                self.push_profile()

            if "footprint" in data:
                self.latest_footprint_payload = data["footprint"]
                self.push_footprint()

            if "book" in data:
                meta = data["book"]
                if meta:
                    best_bid = meta.get("best_bid")
                    best_ask = meta.get("best_ask")
                    mid = ((best_bid + best_ask) / 2.0) if best_bid and best_ask else None
                    self.latest_book_meta = {
                        "mid_price": round(mid, 4) if mid is not None else self.latest_book_meta.get("mid_price"),
                        "spread_bps": round(float(meta["spread_bps"]), 4) if meta.get("spread_bps") is not None else self.latest_book_meta.get("spread_bps"),
                        "book_imbalance": round(float(meta["book_imbalance"]), 4) if meta.get("book_imbalance") is not None else self.latest_book_meta.get("book_imbalance"),
                    }

            self.push_heatmap()
        except Exception:
            pass

    def push_heatmap(self):
        payload = {
            "history": self.depth_history[-self.max_depth_history:],
            "meta": self.latest_book_meta,
        }
        self.js_heatmap(f"window.heatmapApi.update({json.dumps(payload)});")

    def push_profile(self):
        self.js_profile(f"window.profileApi.update({json.dumps(self.latest_profile_payload)});")

    def push_footprint(self):
        self.js_footprint(f"window.footprintApi.update({json.dumps(self.latest_footprint_payload)});")

    def on_closed_candle(self):
        if len(self.df) < 220:
            self.lbl_signal.setText("Signal: Waiting for more candles")
            self.refresh_panels()
            self.start_analytics_worker()
            return

        row = self.df.iloc[-1]
        signal = "No selected setup"

        pos = self.current_position()
        if pos is not None:
            pos.bars_held += 1
            self.set_current_position(pos)
            reason, exit_price = self.check_exit(pos, row)
            if reason is not None:
                self.exit_position(reason, exit_price, row["open_time"])

        scored = self.score_current_row()
        evaluation = self.evaluate_psychology(scored) if scored is not None else None

        if self.current_position() is None and self.auto_enabled and scored is not None:
            long_blockers = self.entry_blockers_for_side("long", scored, evaluation, include_auto_state=False, include_position_state=False)
            short_blockers = self.entry_blockers_for_side("short", scored, evaluation, include_auto_state=False, include_position_state=False)

            if scored["long_ready_user"] and not long_blockers:
                signal = f"LONG READY ({scored.get('long_score_user', scored['long_score']):.2f})"
                snapshot = self.get_entry_snapshot("long", scored)
                self.enter_position(
                    "long",
                    float(row["close"]),
                    row["open_time"],
                    "auto",
                    scored.get("long_score_user", scored["long_score"]),
                    snapshot,
                )
            elif scored["short_ready_user"] and not short_blockers:
                signal = f"SHORT READY ({scored.get('short_score_user', scored['short_score']):.2f})"
                snapshot = self.get_entry_snapshot("short", scored)
                self.enter_position(
                    "short",
                    float(row["close"]),
                    row["open_time"],
                    "auto",
                    scored.get("short_score_user", scored["short_score"]),
                    snapshot,
                )
            elif scored["long_ready_user"] or scored["short_ready_user"]:
                preferred_side = (
                    "long"
                    if scored.get("long_score_user", scored["long_score"]) >= scored.get("short_score_user", scored["short_score"])
                    else "short"
                )
                blockers = long_blockers if preferred_side == "long" else short_blockers
                signal = f"{preferred_side.upper()} BLOCKED | {'; '.join(blockers[:2])}"
            else:
                signal = (
                    f"No setup | "
                    f"L={scored.get('long_score_user', scored['long_score']):.2f} "
                    f"S={scored.get('short_score_user', scored['short_score']):.2f}"
                )

        self.lbl_signal.setText(f"Signal: {signal}")
        self.refresh_panels()
        self.start_analytics_worker()

    def poll_queue(self):
        changed = False
        try:
            while True:
                item = self.queue.get_nowait()

                if item.get("type") == "error":
                    self.lbl_status.setText(f"WS Error: {item.get('message', '')}")
                    self.refresh_status_overview()
                    continue

                if item.get("type") == "depth":
                    self.handle_depth(item)
                    continue

                self.apply_tick(item)
                changed = True

        except queue.Empty:
            pass

        if changed:
            self.sync_chart_update()

    def closeEvent(self, event):
        self.stop_market()
        if self.worker is not None and self.worker.isRunning():
            self.worker.quit()
            self.worker.wait(1000)
        super().closeEvent(event)

