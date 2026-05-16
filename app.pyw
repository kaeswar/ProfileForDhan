"""Desktop Market Profile Creator (PySide6).

Run: python app.pyw
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime

import pandas as pd
from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QLineEdit, QFileDialog, QComboBox, QDoubleSpinBox, QSpinBox,
    QListWidgetItem, QCheckBox, QGroupBox, QMessageBox, QStatusBar,
    QDialog, QDialogButtonBox, QGridLayout,
)

from engine import (
    PERIOD_OPTIONS, ProfileResult, compute_composite,
    compute_profile, minute_key_levels,
)
from chart_unified import UnifiedChart
from dhan_live import DhanLiveFetcher, DEFAULT_SECURITY_ID


class ProfileApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Once a Wanderer's Contribution - Simple Profile - எளியவர்களின் முயற்சி")

        self.files: list[tuple] = []  # [(datetime, path)]

        # Cache for re-rendering without re-fetching
        self._cached_result: ProfileResult | None = None
        self._cached_bin_size: float = 10.0
        self._cached_candle_df: pd.DataFrame | None = None
        self._cached_raw_dfs: list[pd.DataFrame] = []  # raw per-day DFs for candle recompute
        self._cached_view_mode: str = "merged"
        self._cached_count_metric: str = "tpo"
        self._cached_style: str = "merged"

        # Build UI
        central = QWidget()
        self.setCentralWidget(central)
        main_lay = QVBoxLayout(central)
        main_lay.setContentsMargins(5, 5, 5, 5)
        main_lay.setSpacing(5)

        # Initialize parameters with defaults (needed by _sync_mode)
        self._init_params()

        # Top toolbar
        self._build_toolbar(main_lay)

        # Chart (takes remaining space)
        self.chart = UnifiedChart()
        main_lay.addWidget(self.chart, stretch=1)

        self.setStatusBar(QStatusBar())

    def show(self):
        super().showMaximized()

    # ---------- Top Toolbar ----------
    def _build_toolbar(self, parent_lay):
        toolbar = QWidget()
        toolbar.setStyleSheet("background:#1e222d;border-radius:4px;")
        tlay = QHBoxLayout(toolbar)
        tlay.setContentsMargins(8, 4, 8, 4)
        tlay.setSpacing(10)

        # Mode label
        tlay.addWidget(QLabel("<b>Live Profile (Dhan API)</b>"))

        # Live controls in toolbar
        lbl = QLabel("Security:")
        self.live_sec_id = QLineEdit(DEFAULT_SECURITY_ID)
        self.live_sec_id.setFixedWidth(70)
        tlay.addWidget(lbl)
        tlay.addWidget(self.live_sec_id)

        lbl2 = QLabel("Periods:")
        self.live_n_periods = QSpinBox()
        self.live_n_periods.setRange(1, 52)
        self.live_n_periods.setValue(4)
        self.live_n_periods.valueChanged.connect(self._update_live_days_label)
        self.live_days_label = QLabel("(4 days)")
        tlay.addWidget(lbl2)
        tlay.addWidget(self.live_n_periods)
        tlay.addWidget(self.live_days_label)

        lbl3 = QLabel("Profile:")
        self.live_profile_type = QComboBox()
        self.live_profile_type.addItems(["Daily", "Weekly"])
        self.live_profile_type.setFixedWidth(80)
        self.live_profile_type.currentTextChanged.connect(self._on_live_profile_type_changed)
        tlay.addWidget(lbl3)
        tlay.addWidget(self.live_profile_type)

        self.live_status = QLabel("Ready")
        self.live_status.setStyleSheet("color: #4ecdc4; font-size: 11px;")
        tlay.addWidget(self.live_status)

        self.live_auto_cb = QCheckBox("Auto 3min")
        self.live_auto_cb.setChecked(True)
        self.live_auto_cb.toggled.connect(self._toggle_live_timer)
        tlay.addWidget(self.live_auto_cb)

        self._update_live_days_label()

        tlay.addStretch()

        # Action buttons
        self.draw_btn = QPushButton("Draw Profile")
        self.draw_btn.setStyleSheet("""
            QPushButton { background:#4B7BE5; color:white; padding:6px 16px;
                          border-radius:4px; font-weight:bold; }
            QPushButton:hover { background:#5B8BF5; }
        """)
        self.draw_btn.clicked.connect(self._on_draw)
        tlay.addWidget(self.draw_btn)

        btn_style = "QPushButton { background:#2B2B43; color:#d1d4dc; padding:5px 14px; border:1px solid #444; border-radius:4px; } QPushButton:hover { background:#3B3B53; }"

        settings_btn = QPushButton("Settings")
        settings_btn.setStyleSheet(btn_style)
        settings_btn.clicked.connect(self._on_settings)
        tlay.addWidget(settings_btn)

        about_btn = QPushButton("About")
        about_btn.setStyleSheet(btn_style)
        about_btn.clicked.connect(self._on_about)
        tlay.addWidget(about_btn)

        parent_lay.addWidget(toolbar)

    # ---------- Init params ----------
    def _init_params(self):
        """Initialize all parameter values (no widgets on main panel)."""
        settings = QSettings("Wanderer", "SimpleProfile")
        self._dhan_client_id = settings.value("dhan/client_id", "")
        self._dhan_access_token = settings.value("dhan/access_token", "")
        self._week_start_day = settings.value("data/week_start", "Wednesday")
        self._tick_size = 0.05
        self._bin_size = 10.0
        self._period_text = "30 min"
        self._va_pct = 0.68
        self._show_letters = True
        self._style_text = "Merged"
        self._metric_text = "TPO (brackets)"
        self._show_candle = False
        self._candle_tf = "5 min"
        self._visibility = {
            "poc": True, "vah": True, "val": True,
            "open": True, "close": True, "mid": True,
            "ib_high": True, "ib_low": True,
        }
        self._ib_minutes = 60

        # Live timer
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(3 * 60 * 1000)  # 3 minutes
        self._live_timer.timeout.connect(self._on_live_fetch)

    def _browse_folder_from_settings(self, folder_edit):
        """Browse for folder from Settings dialog."""
        d = QFileDialog.getExistingDirectory(self, "Select data folder", folder_edit.text())
        if d:
            folder_edit.setText(d)

    # ---------- Helpers ----------
    def _resample_candles(self, df: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min OHLCV data to the selected candlestick timeframe."""
        tf_map = {"1 min": "1min", "5 min": "5min", "15 min": "15min",
                  "30 min": "30min", "1 hour": "1h", "4 hours": "4h", "1 day": "1D"}
        tf = tf_map.get(self._candle_tf, "5min")
        if tf == "1min":
            return df
        df = df.set_index("timestamp")
        resampled = df.resample(tf).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna(subset=["open"]).reset_index()
        return resampled

    def _group_into_weeks_live(self, day_data: list[tuple]) -> list[list[tuple]]:
        """Group (date, DataFrame) tuples into weeks based on configured start day.
        Same logic as _group_into_weeks but for live data format.
        """
        day_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2,
                   "Thursday": 3, "Friday": 4}
        week_start = getattr(self, '_week_start_day', "Wednesday")
        start_dow = day_map.get(week_start, 2)  # default to Wednesday (2)

        if not day_data:
            return []

        from datetime import timedelta

        weeks: list[list[tuple]] = []
        current_week: list[tuple] = []
        first_date = day_data[0][0]
        days_since_start = (first_date.weekday() - start_dow) % 7
        current_week_start = first_date - timedelta(days=days_since_start)

        for d, df in day_data:
            # Check if this date belongs to the current week
            days_since = (d - current_week_start).days
            if days_since >= 7:
                # Start new week(s) — advance week_start
                if current_week:
                    weeks.append(current_week)
                    current_week = []
                # Advance to the correct week
                while (d - current_week_start).days >= 7:
                    current_week_start += timedelta(days=7)
            current_week.append((d, df))

        if current_week:
            weeks.append(current_week)
        return weeks

    def _update_live_days_label(self):
        """Update the days label based on periods and profile type."""
        n_periods = self.live_n_periods.value()
        profile_type = self.live_profile_type.currentText()
        if profile_type == "Weekly":
            days_approx = n_periods * 7
            self.live_days_label.setText(f"(~{days_approx} days)")
        else:
            self.live_days_label.setText(f"({n_periods} day{'s' if n_periods > 1 else ''})")

    def _on_live_profile_type_changed(self, profile_type: str):
        """Handle profile type change - update label and set smart defaults for weekly."""
        self._update_live_days_label()
        if profile_type == "Weekly":
            # Set smart defaults for weekly: Bin size = 50, TPO period = 1 hour
            self._live_bin_size = self._bin_size
            self._live_period_text = self._period_text
            self._live_tick_size = self._tick_size
            self._live_va_pct = self._va_pct

            self._bin_size = 50.0
            self._period_text = "1 hour"
            self._tick_size = 0.05
            self._va_pct = 0.68
        else:
            # Restore previous settings if they were set
            if hasattr(self, "_live_bin_size"):
                self._bin_size = self._live_bin_size
                self._period_text = self._live_period_text
                self._tick_size = self._live_tick_size
                self._va_pct = self._live_va_pct

    # ---------- Draw ----------
    def _toggle_live_timer(self, checked: bool):
        if checked and self.mode_combo.currentText() == "Live (Dhan API)":
            self._live_timer.start()
            self.live_days_label2.setText("Auto: ON")
        else:
            self._live_timer.stop()
            self.live_days_label2.setText("Auto: OFF")

    def _on_live_fetch(self):
        """Fetch live data from Dhan API and draw profile."""
        sec_id = self.live_sec_id.text().strip() or DEFAULT_SECURITY_ID
        n_periods = self.live_n_periods.value()
        profile_type = self.live_profile_type.currentText()  # "Daily" or "Weekly"

        # Calculate actual days to fetch based on profile type
        if profile_type == "Weekly":
            n_days = n_periods * 7  # fetch enough days to cover N weeks
            status_text = f"{n_periods} week(s)"
        else:
            n_days = n_periods
            status_text = f"{n_days} day(s)"

        self.live_status.setText(f"Status: Fetching {status_text}...")
        self.live_status.repaint()
        QApplication.processEvents()

        try:
            if self._dhan_client_id and self._dhan_access_token:
                fetcher = DhanLiveFetcher(
                    client_id=self._dhan_client_id,
                    access_token=self._dhan_access_token,
                    security_id=sec_id)
            else:
                fetcher = DhanLiveFetcher.from_credentials_file(security_id=sec_id)
            if not fetcher.is_configured():
                QMessageBox.warning(self, "Credentials Missing",
                    "No Dhan API credentials found.\n"
                    "Go to Settings and enter your Client ID and Access Token.")
                self.live_status.setText("Status: No credentials")
                return

            tick = self._tick_size
            bin_sz = self._bin_size
            period = PERIOD_OPTIONS[self._period_text]
            va = self._va_pct
            ib_min = self._ib_minutes
            from engine import SESSION_START, SESSION_END

            # Fetch data
            day_data = fetcher.fetch_last_n_days(n_days)
            if not day_data:
                self.live_status.setText("Status: No data found")
                return

            # Process and filter data
            all_dfs = []
            processed_days = []
            for d, df in day_data:
                t = df["timestamp"].dt.time
                df = df[(t >= SESSION_START) & (t <= SESSION_END)].copy()
                if df.empty:
                    continue
                all_dfs.append(df)
                processed_days.append((d, df))

            if not processed_days:
                self.live_status.setText("Status: No data in session hours")
                return

            # Build profiles
            if profile_type == "Weekly":
                # Group days into weeks
                weeks = self._group_into_weeks_live(processed_days)
                if not weeks:
                    self.live_status.setText("Status: No weeks found")
                    return

                weekly_profiles = []
                for week_days in weeks:
                    week_daily = []
                    for d, df in week_days:
                        week_daily.append(compute_profile(
                            df, tick_size=tick, period_minutes=period,
                            value_area_pct=va, title=d.strftime("%d-%b"),
                            ib_minutes=ib_min))
                    if week_daily:
                        first_d = week_days[0][0].strftime("%d-%b")
                        last_d = week_days[-1][0].strftime("%d-%b")
                        wp = compute_composite(week_daily, value_area_pct=va,
                                               tick_size=tick,
                                               title=f"{first_d}→{last_d}")
                        weekly_profiles.append(wp)

                if not weekly_profiles:
                    self.live_status.setText("Status: No weekly data")
                    return

                # Meta-composite of weekly profiles
                title = f"LIVE Weekly ({n_periods} week{'s' if n_periods > 1 else ''})"
                result = compute_composite(weekly_profiles, value_area_pct=va,
                                           tick_size=tick, title=title)
                # Override components to be the weekly profiles (for continuous rendering)
                result = replace(result, components=weekly_profiles)
                view_mode = "continuous"
                total_bars = sum(len(df) for df in all_dfs)
            else:
                # Daily profile (single day or continuous)
                if n_periods == 1:
                    # Single day
                    d, df = processed_days[0]
                    result = compute_profile(df, tick_size=tick, period_minutes=period,
                                             value_area_pct=va, title="LIVE — Nifty Futures",
                                             ib_minutes=ib_min)
                    view_mode = "merged" if self._style_text == "Merged" else "expanded"
                    total_bars = len(df)
                else:
                    # Multi-day continuous
                    daily_profiles = []
                    for d, df in processed_days:
                        daily_profiles.append(compute_profile(
                            df, tick_size=tick, period_minutes=period,
                            value_area_pct=va, title=d.strftime("%d-%b"),
                            ib_minutes=ib_min))

                    first_d = processed_days[0][0].strftime("%d-%b")
                    last_d = processed_days[-1][0].strftime("%d-%b")
                    title = f"LIVE — {len(daily_profiles)} days ({first_d} → {last_d})"
                    result = compute_composite(daily_profiles, value_area_pct=va,
                                               tick_size=tick, title=title)
                    view_mode = "continuous"
                    total_bars = sum(len(df) for df in all_dfs)

            metric = "minute" if self._metric_text.startswith("Minute") else "tpo"
            style_merged = self._style_text == "Merged"
            cur_style = "merged" if style_merged else "expanded"

            candle_df = None
            if self._show_candle and all_dfs:
                candle_df = pd.concat(all_dfs, ignore_index=True).sort_values("timestamp")
                candle_df = self._resample_candles(candle_df)

            # Cache for re-rendering without re-fetch
            self._cached_result = result
            self._cached_bin_size = bin_sz
            self._cached_candle_df = candle_df
            self._cached_raw_dfs = all_dfs
            self._cached_view_mode = view_mode
            self._cached_count_metric = metric
            self._cached_style = cur_style

            self.chart.render(result, bin_sz, candle_df=candle_df,
                              show_letters=self._show_letters,
                              view_mode=view_mode, count_metric=metric,
                              style=cur_style,
                              visibility=self._visibility,
                              ib_minutes=ib_min)

            now = datetime.now().strftime("%H:%M:%S")
            self.live_status.setText(f"Status: OK — {total_bars} bars @ {now}")
            self.statusBar().showMessage(
                f"Live: {result.title} | POC={result.poc:.2f} | VAH={result.vah:.2f} | VAL={result.val:.2f} | TPOs={result.total_tpo} | {now}")

            # Start auto-refresh if enabled
            if self.live_auto_cb.isChecked() and not self._live_timer.isActive():
                self._live_timer.start()

        except Exception as e:
            self.live_status.setText(f"Status: Error — {str(e)[:60]}")
            self.chart.page().runJavaScript("hideLoading();")
            QMessageBox.critical(self, "Live Fetch Error", str(e))

    def _on_about(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("About - Simple Profile")
        dlg.setFixedSize(440, 520)
        vl = QVBoxLayout(dlg)
        vl.setSpacing(8)

        title = QLabel("Once a Wanderer's Contribution - Simple Profile")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#ffffff;")
        vl.addWidget(title)

        subtitle = QLabel("எளியவர்களின் முயற்சி")
        subtitle.setStyleSheet("font-size:12px; color:#888;")
        vl.addWidget(subtitle)

        desc = QLabel(
            "A desktop application for generating and visualizing Market Profile charts "
            "for Nifty Futures (and other instruments). Supports single-day, multi-day composite, "
            "continuous, and weekly profile modes with TPO and minute-density metrics. "
            "Includes TradingView-style candlestick overlay, Initial Balance, Value Area, "
            "and interactive pan/zoom/stretch."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#aaa;")
        vl.addWidget(desc)

        dev = QLabel("Developed with Claude Code (Anthropic AI) - the entire codebase was written and iterated through AI-assisted development.")
        dev.setWordWrap(True)
        dev.setStyleSheet("color:#aaa;")
        vl.addWidget(dev)

        free = QLabel("This software is free to use and free to enhance. It is provided as-is and may contain errors - use at your own discretion.")
        free.setWordWrap(True)
        free.setStyleSheet("color:#aaa;")
        vl.addWidget(free)

        driven = QLabel("This phase of development was driven by the need and desire of Kaeswar - kaeswar@gmail.com")
        driven.setWordWrap(True)
        driven.setStyleSheet("color:#aaa;")
        vl.addWidget(driven)

        donate = QLabel("If you find this useful, please consider contributing a little - it will be encouraging and help keep this project alive.")
        donate.setWordWrap(True)
        donate.setStyleSheet("color:#aaa;")
        vl.addWidget(donate)

        line = QLabel("")
        line.setStyleSheet("border:1px solid #444;")
        line.setFixedHeight(1)
        vl.addWidget(line)

        upi = QLabel("Donate via UPI: kaeswar@oksbi")
        upi.setStyleSheet("font-weight:bold; color:#4ecdc4;")
        vl.addWidget(upi)

        # QR Code
        qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "donate_qr.png")
        if os.path.exists(qr_path):
            from PySide6.QtGui import QPixmap
            qr_label = QLabel()
            pixmap = QPixmap(qr_path).scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            qr_label.setPixmap(pixmap)
            qr_label.setAlignment(Qt.AlignCenter)
            vl.addWidget(qr_label)
            scan_lbl = QLabel("Scan with GPay / PhonePe / Paytm")
            scan_lbl.setAlignment(Qt.AlignCenter)
            scan_lbl.setStyleSheet("color:#888; font-size:11px;")
            vl.addWidget(scan_lbl)

        vl.addStretch()

        footer = QLabel("Built with Python, PySide6, TradingView Lightweight Charts & HTML5 Canvas.")
        footer.setStyleSheet("color:#666; font-size:10px;")
        vl.addWidget(footer)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet("background:#4B7BE5; color:white; padding:6px 20px; border-radius:4px;")
        close_btn.clicked.connect(dlg.accept)
        vl.addWidget(close_btn)

        dlg.exec()

    def _on_settings(self):
        from PySide6.QtWidgets import QScrollArea, QWidget, QGridLayout

        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        dlg.setMinimumWidth(500)
        dlg.setMinimumHeight(600)

        main_lay = QVBoxLayout(dlg)

        # Scroll Area for content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        main_lay.addWidget(scroll)

        # Container for scroll area
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setSpacing(8)

        # --- Parameters (Compact 2-column grid) ---
        pbox = QGroupBox("Parameters")
        gl = QGridLayout(pbox)
        gl.setSpacing(8)

        gl.addWidget(QLabel("Tick:"), 0, 0)
        tick_spin = QDoubleSpinBox(); tick_spin.setDecimals(4)
        tick_spin.setRange(0.0001, 100.0); tick_spin.setSingleStep(0.05)
        tick_spin.setValue(self._tick_size); tick_spin.setFixedWidth(80)
        gl.addWidget(tick_spin, 0, 1)

        gl.addWidget(QLabel("Bin:"), 0, 2)
        bin_spin = QDoubleSpinBox(); bin_spin.setDecimals(2)
        bin_spin.setRange(0.05, 1000.0); bin_spin.setSingleStep(1.0)
        bin_spin.setValue(self._bin_size); bin_spin.setFixedWidth(80)
        gl.addWidget(bin_spin, 0, 3)

        gl.addWidget(QLabel("TPO:"), 1, 0)
        period_combo = QComboBox()
        for k in PERIOD_OPTIONS: period_combo.addItem(k)
        period_combo.setCurrentText(self._period_text); period_combo.setFixedWidth(100)
        gl.addWidget(period_combo, 1, 1)

        gl.addWidget(QLabel("VA%:"), 1, 2)
        va_spin = QDoubleSpinBox(); va_spin.setRange(0.50, 0.95)
        va_spin.setSingleStep(0.01); va_spin.setValue(self._va_pct); va_spin.setFixedWidth(80)
        gl.addWidget(va_spin, 1, 3)

        gl.addWidget(QLabel("Style:"), 2, 0)
        style_combo = QComboBox()
        style_combo.addItems(["Merged", "Expanded (time x-axis)"])
        style_combo.setCurrentText(self._style_text); style_combo.setFixedWidth(120)
        gl.addWidget(style_combo, 2, 1)

        gl.addWidget(QLabel("Metric:"), 2, 2)
        metric_combo = QComboBox()
        metric_combo.addItems(["TPO (brackets)", "Minute (1-min bars)"])
        metric_combo.setCurrentText(self._metric_text); metric_combo.setFixedWidth(120)
        gl.addWidget(metric_combo, 2, 3)

        gl.addWidget(QLabel("IB (min):"), 3, 0)
        ib_spin = QSpinBox(); ib_spin.setRange(15, 240)
        ib_spin.setValue(self._ib_minutes); ib_spin.setSingleStep(15); ib_spin.setFixedWidth(80)
        gl.addWidget(ib_spin, 3, 1)

        vl.addWidget(pbox)

        # --- Show/Hide Overlays (compact grid) ---
        ovbox = QGroupBox("Show / Hide")
        ovlayout = QGridLayout(ovbox)
        ovlayout.setSpacing(6)
        vis_cbs = {}
        overlays = [
            ("poc", "POC"), ("vah", "VAH"), ("val", "VAL"),
            ("open", "Open"), ("close", "Close"), ("mid", "Mid"),
            ("ib_high", "IB High"), ("ib_low", "IB Low"),
        ]
        for i, (key, label) in enumerate(overlays):
            cb = QCheckBox(label)
            cb.setChecked(self._visibility[key])
            vis_cbs[key] = cb
            ovlayout.addWidget(cb, i // 4, i % 4)
        vl.addWidget(ovbox)

        # --- Candlestick (compact) ---
        cbox = QGroupBox("Candlestick")
        cl = QHBoxLayout(cbox)
        candle_cb = QCheckBox("Show Chart")
        candle_cb.setChecked(self._show_candle)
        cl.addWidget(candle_cb)
        cl.addWidget(QLabel("TF:"))
        candle_tf_combo = QComboBox()
        candle_tf_combo.addItems(["1 min", "5 min", "15 min", "30 min", "1 hour", "4 hours", "1 day"])
        candle_tf_combo.setCurrentText(self._candle_tf)
        candle_tf_combo.setFixedWidth(90)
        cl.addWidget(candle_tf_combo)
        cl.addStretch()
        vl.addWidget(cbox)

        # --- Dhan API (compact) ---
        cred_box = QGroupBox("Dhan API Credentials")
        cred_layout = QGridLayout(cred_box)
        cred_layout.addWidget(QLabel("Client ID:"), 0, 0)
        client_id_edit = QLineEdit(self._dhan_client_id)
        client_id_edit.setFixedWidth(150)
        cred_layout.addWidget(client_id_edit, 0, 1)
        cred_layout.addWidget(QLabel("Access Token:"), 1, 0)
        token_edit = QLineEdit(self._dhan_access_token)
        token_edit.setEchoMode(QLineEdit.Password)
        token_edit.setFixedWidth(150)
        cred_layout.addWidget(token_edit, 1, 1)
        show_token_cb = QCheckBox("Show")
        show_token_cb.toggled.connect(
            lambda checked: token_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password))
        cred_layout.addWidget(show_token_cb, 1, 2)
        vl.addWidget(cred_box)

        scroll.setWidget(container)

        # Buttons at bottom
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        main_lay.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        if dlg.exec() == QDialog.Accepted:
            old_visibility = dict(self._visibility)
            self._tick_size = tick_spin.value()
            self._bin_size = bin_spin.value()
            self._period_text = period_combo.currentText()
            self._va_pct = va_spin.value()
            self._style_text = style_combo.currentText()
            self._metric_text = metric_combo.currentText()
            self._ib_minutes = ib_spin.value()
            self._show_candle = candle_cb.isChecked()
            self._candle_tf = candle_tf_combo.currentText()
            for key, cb in vis_cbs.items():
                self._visibility[key] = cb.isChecked()
            self._dhan_client_id = client_id_edit.text().strip()
            self._dhan_access_token = token_edit.text().strip()
            settings = QSettings("Wanderer", "SimpleProfile")
            settings.setValue("dhan/client_id", self._dhan_client_id)
            settings.setValue("dhan/access_token", self._dhan_access_token)

            # Save week start day
            settings.setValue("data/week_start", self._week_start_day)

            # Re-render from cache if visibility, style, or candle changed
            visibility_changed = old_visibility != self._visibility
            style_changed = style_combo.currentText() != self._cached_style.replace("merged", "Merged").replace("expanded", "Expanded (time x-axis)") if self._cached_style else False
            candle_changed = candle_cb.isChecked() != self._show_candle
            if self._cached_result and (visibility_changed or style_changed or candle_changed):
                self._show_candle = candle_cb.isChecked()
                self._render_from_cache()

    def _on_draw(self):
        # Only Live mode is supported now
        self.chart.page().runJavaScript("showLoading();")
        # Give JS a moment to execute, then fetch
        QTimer.singleShot(50, self._on_live_fetch)

    def _render_from_cache(self):
        """Re-render chart from cached data (no re-fetch needed)."""
        if not self._cached_result:
            return
        # Recompute candle_df if candle was just enabled
        candle_df = self._cached_candle_df
        if self._show_candle and not candle_df and self._cached_raw_dfs:
            candle_df = pd.concat(self._cached_raw_dfs, ignore_index=True).sort_values("timestamp")
            candle_df = self._resample_candles(candle_df)
        elif not self._show_candle:
            candle_df = None
        self.chart.render(
            self._cached_result,
            self._cached_bin_size,
            candle_df=candle_df,
            show_letters=self._show_letters,
            view_mode=self._cached_view_mode,
            count_metric=self._cached_count_metric,
            style=self._cached_style,
            visibility=self._visibility,
            ib_minutes=self._ib_minutes
        )
        self.statusBar().showMessage(f"Re-rendered: {self._cached_result.title} (from cache)")


def main():
    app = QApplication(sys.argv)
    w = ProfileApp()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
