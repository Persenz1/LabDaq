"""Chinese desktop workbench for simulated voltage and original HPF inputs."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import argparse
import json
import math
from pathlib import Path
import shutil
import sys

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W

from . import theme
from .ui_kit import (CollapsibleSection, MetricCard, card, chip_button, divider,
                     field, field_label, label, legend_chip, section_title,
                     status_chip, status_dot)
from .engine import AcquisitionEngine
from .core_client import DEFAULT_ENDPOINT, RemoteEngine
from .inputs import HpfReplaySession
from .signals import SimulationSettings
from .input_dialog import SignalDialog
from .output_panel import OutputPanel, DEFAULT_SETTINGS as DEFAULT_OUTPUT, validate_settings as validate_output
from .processing import ChannelSettings, convert_volts, spectrum, frequency_response, filter_window
from .protocol import AcquisitionConfig, plan_clock
from .recording import load_session, next_recording_path
from .data_dialog import ExportDialog, StatisticsDialog
from .recording_library import RecordingLibraryDialog
from .plot_measurements import PlotMeasurements
from .transport import enumerate_usb_devices

ROOT = Path(__file__).resolve().parents[1]
NAV_TITLES = ["实时采集", "信号输出", "信号分析", "数据回看", "设备与协议"]
NAV_ICONS = ["wave", "export", "stats", "history", "chip"]
PAGE_SUBTITLES = [
    "连接设备，配置采集参数，实时查看多通道信号波形与数值。",
    "设置 AO 波形与输出参数，控制信号输出和停止归零。",
    "从时间波形到频域，明确每一次分析的来源与单位。",
    "保留原始信号，让数据可以重新查看、分析和导出。",
    "查看设备描述符、协议命令和离线冷启动流程。",
]


def button(text: str, callback=None, name="") -> W.QPushButton:
    result = W.QPushButton(text)
    result.setObjectName(name)
    result.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    if callback is not None:
        result.clicked.connect(callback)
    return result


def plot_widget(ylabel="力", unit="N") -> pg.PlotWidget:
    p = theme.current()
    plot = pg.PlotWidget(background=p.plot_bg)
    plot.showGrid(x=True, y=True, alpha=0.10)
    plot.setLabel("bottom", "时间", units="s")
    plot.setLabel("left", ylabel, units=unit)
    # Prefixing a compound unit such as V²/Hz would visually turn milli(V²)
    # into mV², which denotes a different scale. Keep y units explicit.
    plot.getAxis("left").enableAutoSIPrefix(False)
    plot.setMenuEnabled(False)
    plot.hideButtons()
    theme.style_plot(plot, p)
    return plot


def configure_application(app):
    """Apply the LabDAQ palette, fonts and pyqtgraph defaults."""
    palette = theme.apply(app)
    theme.configure_pyqtgraph(palette)
    return palette


class DeviceConnectionWorker(QtCore.QThread):
    """Discover and connect the device without blocking the Qt event loop."""

    def __init__(self, parameters, scan_first, parent):
        super().__init__(parent)
        self.parameters = parameters
        self.scan_first = scan_first
        self.engine = None
        self.error = ""

    def run(self):
        try:
            if self.scan_first and not enumerate_usb_devices():
                return
            self.engine = RemoteEngine.start_new(**self.parameters, source="usb", connect_only=True)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error = str(exc)


class ChannelDialog(W.QDialog):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.setWindowTitle("通道与传感器配置")
        self.resize(1180, 470)
        self.window = window
        p = theme.current()
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(label("通道与传感器", "pageTitle"))
        layout.addWidget(label("当前 10 mV/N 为演示灵敏度。实际测量请填写传感器与调理器的换算参数；"
                               "原始电压始终保留。", "notice", True))
        table_card, tl = card(margins=(10, 10, 10, 10), spacing=8)
        self.table = W.QTableWidget(4, 9)
        self.table.setHorizontalHeaderLabels(["启用", "通道名称", "单位", "mV / 单位", "零点 / V",
                                              "反向", "量程", "耦合", "IEPE"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, W.QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((66, 290, 86, 118, 106, 62, 104, 92, 100)):
            self.table.setColumnWidth(column, width)
        self.table.setFixedHeight(4 * 48 + 40)
        self.cells = []
        for i, settings in enumerate(window.channels):
            enabled = W.QCheckBox(f"AI {i}")
            enabled.setChecked(i in window.active_channels)
            name = W.QLineEdit(settings.name)
            unit = W.QComboBox()
            unit.setEditable(True)
            unit.addItems(["N", "V", "Pa", "g", "mm", "°C"])
            unit.setCurrentText(settings.unit)
            sensitivity = W.QDoubleSpinBox()
            sensitivity.setRange(0.000001, 1e6)
            sensitivity.setDecimals(6)
            sensitivity.setValue(settings.sensitivity_mv_per_unit)
            zero = W.QDoubleSpinBox()
            zero.setRange(-10, 10)
            zero.setDecimals(6)
            zero.setValue(settings.zero_volts)
            inverted = W.QCheckBox()
            inverted.setChecked(settings.inverted)
            ranges = W.QComboBox()
            ranges.addItems(["±10 V", "±1 V"])
            ranges.setCurrentIndex(0 if window.ranges[i] == 10 else 1)
            coupling = W.QComboBox()
            coupling.addItems(["DC", "AC"])
            coupling.setCurrentText(window.coupling[i])
            coupling.setToolTip("模拟器的 AC 为 1 Hz 一阶高通演示模型；实际硬件频响仍需测量。")
            iepe = W.QCheckBox("2 mA")
            iepe.setChecked(window.iepe[i])
            iepe.setToolTip("模拟器验证激励设置的寄存器命令，不模拟传感器的供电与电气响应。")
            row = (enabled, name, unit, sensitivity, zero, inverted, ranges, coupling, iepe)
            self.cells.append(row)
            for j, widget in enumerate(row):
                self.table.setCellWidget(i, j, widget)
            self.table.setRowHeight(i, 48)
        tl.addWidget(self.table)
        layout.addWidget(table_card)
        layout.addWidget(label("换算：工程值 = 1000 × (原始电压[V] − 零点[V]) ÷ 灵敏度[mV/单位]。"
                               "单位为 V 时不使用灵敏度。", "muted", True))
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok |
                                     W.QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(W.QDialogButtonBox.StandardButton.Ok)
        ok.setText("应用设置")
        ok.setObjectName("primary")
        buttons.button(W.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self):
        active = tuple(i for i, row in enumerate(self.cells) if row[0].isChecked())
        if not active:
            W.QMessageBox.information(self, "通道设置", "至少启用一个通道。")
            return
        w = self.window
        for i, row in enumerate(self.cells):
            _, name, unit, sensitivity, zero, inverted, ranges, coupling, iepe = row
            w.channels[i] = ChannelSettings(i, name.text().strip() or f"通道 {i}",
                                            unit.currentText().strip() or "N",
                                            sensitivity.value(), zero.value(), inverted.isChecked())
            w.ranges[i] = 10.0 if ranges.currentIndex() == 0 else 1.0
            w.coupling[i] = coupling.currentText()
            w.iepe[i] = iepe.isChecked()
        w.active_channels = active
        self.accept()


class MainWindow(W.QMainWindow):
    def __init__(self, *, core_endpoint=DEFAULT_ENDPOINT):
        app = W.QApplication.instance()
        if app is not None and not hasattr(app, "_labdaq_palette"):
            configure_application(app)
        super().__init__()
        self.p = theme.current()
        self.setWindowTitle("LabDAQ · 实验采集原型")
        self.resize(1450, 940)
        self.setMinimumSize(1180, 780)
        self.channels = [ChannelSettings(i, n) for i, n in enumerate(["切向力", "法向力", "侧向力", "参考通道"])]
        self.active_channels = (0, 1, 2, 3)
        self.ranges = [10.0] * 4
        self.coupling = ["DC"] * 4
        self.iepe = [False] * 4
        self.signal_settings = SimulationSettings()
        self.input_hpf = None
        self.input_hpf_config = None
        self.engine: AcquisitionEngine | RemoteEngine | None = None
        self.core_endpoint = core_endpoint
        self.recording_root = ROOT / "recordings"
        self.latest_recording: Path | None = None
        self.recording_duration_seconds: float | None = None
        self._completion_shown: Path | None = None
        self._previous_zero: list[float] | None = None
        self._live_cache = None
        self._live_display_key = None
        self._preview_invalidated = False
        self._buttons_refresh_key = None
        self._live_follow = True
        self._connecting = False
        self._device_worker = None
        self._close_after_connect = False
        self._device_error = ""
        self.replay = None
        self.replay_path: Path | None = None
        self.replay_is_hpf = False
        self._error_shown = ""
        self._tick = 0
        self._build()
        self.scenario.currentIndexChanged.connect(self._input_changed)
        self._input_changed()
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(16)
        try:
            self.engine = RemoteEngine.reconnect(self.core_endpoint)
            if self.engine:
                self._restore_core_session()
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell(f"无法连接采集核心：{exc}")

    # ------------------------------------------------------------------
    # Window skeleton

    def _restore_core_session(self):
        p = self.engine.profile
        self.channels = [ChannelSettings(**c) for c in p["channels"]]
        config = self.engine.config
        self.active_channels = tuple(config.channels)
        self.ranges, self.coupling, self.iepe = list(config.ranges), list(config.coupling), list(config.iepe)
        self.signal_settings = SimulationSettings.from_dict(p["signal_settings"])
        if self.engine.output.get("settings"):
            self.output_panel.set_settings(self.engine.output["settings"])
        self._set_sample_rate(config.sample_rate)
        self.experiment.setText(p["experiment"])
        self._set_recording_options(p["recording_options"])
        self.recording_root = Path(p["recording_root"])
        self.directory_label.setText(str(self.recording_root))
        self.directory_label.setToolTip(str(self.recording_root))
        self.latest_recording = self.engine.last_recording
        # Reconnect is read-only: changing the input selector must not stop the core.
        self.scenario.blockSignals(True)
        self.scenario.setCurrentIndex(self.scenario.findData("usb" if self.engine.source == "usb" else "rust"))
        self.scenario.blockSignals(False)
        self.input_button.setVisible(self.engine.source != "usb")
        self.hpf_loop.setVisible(False)
        self.actual_rate.setText(f"实际时钟：{self.engine.sample_rate:,.3f} S/s")
        self._sync_channel_labels()
        self._refresh_input_info()
        self._refresh_buttons()
        self.tell(f"已接回独立核心 · {'正在记录' if self.engine.recording else '预览中' if self.engine.running else '已连接 · 未采集' if self.engine.device_connected else '已停止'}"
                  f" · {self.latest_recording or '尚无记录'}")

    def _build(self):
        central = W.QWidget()
        self.setCentralWidget(central)
        outer = W.QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._sidebar())
        work = W.QWidget()
        work.setObjectName("workspace")
        body = W.QVBoxLayout(work)
        body.setContentsMargins(22, 18, 22, 14)
        body.setSpacing(12)
        body.addLayout(self._header())
        body.addWidget(self._error_banner())
        self.pages = W.QStackedWidget()
        self.pages.setObjectName("pages")
        self.pages.addWidget(self._live_page())
        self.pages.addWidget(self._output_page())
        self.pages.addWidget(self._analysis_page())
        self.pages.addWidget(self._replay_page())
        self.pages.addWidget(self._device_page())
        body.addWidget(self.pages, 1)
        body.addWidget(self._notice_strip())
        outer.addWidget(work, 1)
        self.nav.currentRowChanged.connect(self._page_changed)
        self.nav.setCurrentRow(0)
        self._sync_channel_labels()

    def _sidebar(self):
        p = self.p
        side = W.QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(212)
        sl = W.QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(0)
        brand_row = W.QHBoxLayout()
        brand_row.setSpacing(10)
        logo = W.QLabel()
        logo.setPixmap(theme.logo_pixmap(p, 34))
        logo.setFixedSize(34, 34)
        titles = W.QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("LabDAQ", "brand"))
        titles.addWidget(label("实验室信号工作台", "sideCaption"))
        brand_row.addWidget(logo)
        brand_row.addLayout(titles)
        brand_row.addStretch()
        sl.addLayout(brand_row)
        sl.addSpacing(22)
        self.nav = W.QListWidget()
        self.nav.setObjectName("nav")
        self.nav.addItems(NAV_TITLES)
        self.nav.setIconSize(QtCore.QSize(18, 18))
        self.nav.setSpacing(2)
        self.nav.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        for index, item in enumerate([self.nav.item(i) for i in range(self.nav.count())]):
            item.setIcon(theme.icon(NAV_ICONS[index], p.sidebar_icon, 18))
        sl.addWidget(self.nav)
        sl.addStretch()
        device = W.QFrame()
        device.setObjectName("deviceCard")
        dl = W.QVBoxLayout(device)
        dl.setContentsMargins(11, 10, 11, 10)
        dl.setSpacing(5)
        device_top = W.QHBoxLayout()
        device_top.setSpacing(8)
        chip = W.QLabel()
        chip.setPixmap(theme.icon("chip", p.sidebar_icon, 17).pixmap(17, 17))
        names = W.QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("DT9837C", "deviceName"))
        names.addWidget(label("4 通道 · 24 bit · USB 2.0", "deviceMeta"))
        device_top.addWidget(chip)
        device_top.addLayout(names)
        device_top.addStretch()
        dl.addLayout(device_top)
        status_row = W.QHBoxLayout()
        status_row.setSpacing(6)
        self.side_device_dot = status_dot(p.sidebar_muted, 8)
        self.side_device_status = label("未连接", "deviceMeta")
        status_row.addWidget(self.side_device_dot)
        status_row.addWidget(self.side_device_status)
        status_row.addStretch()
        dl.addLayout(status_row)
        self.side_core_status = label("核心 · 未启动", "deviceMeta")
        dl.addWidget(self.side_core_status)
        self.reconnect_button = chip_button("重新连接", "link", self.reconnect_device)
        self.reconnect_button.setFixedHeight(26)
        self.reconnect_button.setToolTip("重新连接数采并进入待机，不会开始采集或记录。")
        dl.addWidget(self.reconnect_button)
        sl.addWidget(device)
        sl.addSpacing(10)
        sl.addWidget(label("内部开发版 v0.1", "sideCaption"))
        return side

    def _header(self):
        header = W.QHBoxLayout()
        header.setSpacing(12)
        titles = W.QVBoxLayout()
        titles.setSpacing(2)
        self.title = label("实时采集", "pageTitle")
        self.subtitle = label(PAGE_SUBTITLES[0], "pageSubtitle")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.mode_badge = label("独立核心模拟", "badge")
        header.addWidget(self.mode_badge, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        return header

    def _error_banner(self):
        self.error_banner = W.QFrame()
        self.error_banner.setObjectName("errorBanner")
        self.error_banner.setStyleSheet(
            f"QFrame#errorBanner {{ background: {self.p.danger_soft}; border: 1px solid {self.p.danger}; border-radius: 10px; }}")
        row = W.QHBoxLayout(self.error_banner)
        row.setContentsMargins(14, 10, 14, 10)
        mark = W.QLabel()
        mark.setPixmap(theme.icon("error", self.p.danger, 23).pixmap(23, 23))
        row.addWidget(mark)
        texts = W.QVBoxLayout()
        texts.setSpacing(3)
        self.error_title = label("操作失败")
        self.error_title.setStyleSheet(f"color: {self.p.danger}; font-size: 15px; font-weight: 600;")
        self.error_details = label("", "muted", True)
        self.error_details.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.error_details.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        texts.addWidget(self.error_title)
        texts.addWidget(self.error_details)
        row.addLayout(texts, 1)
        self.error_banner.hide()
        return self.error_banner

    def _notice_strip(self):
        strip = W.QFrame()
        strip.setObjectName("statusStrip")
        layout = W.QHBoxLayout(strip)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(8)
        self.notice_icon = W.QLabel()
        self.notice_icon.setPixmap(theme.icon("info", self.p.text_muted, 14).pixmap(14, 14))
        self.notice = label("准备就绪 · 连接 DT9837C 后开始预览，点击“开始记录”保存原始数据。",
                            "statusStripText", True)
        layout.addWidget(self.notice_icon)
        layout.addWidget(self.notice, 1)
        return strip

    def _set_nav_icons(self, index):
        p = self.p
        for row in range(self.nav.count()):
            color = p.sidebar_icon_active if row == index else p.sidebar_icon
            self.nav.item(row).setIcon(theme.icon(NAV_ICONS[row], color, 18))

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _page_changed(self, index):
        if index < 0:
            return
        self._set_nav_icons(index)
        self.pages.setCurrentIndex(index)
        self.title.setText(NAV_TITLES[index])
        self.subtitle.setText(PAGE_SUBTITLES[index])
        self._update_mode_badge()
        if index == 2:
            self.refresh_analysis()
        if index == 4:
            self.refresh_protocol()

    # ------------------------------------------------------------------
    # Live acquisition page

    def _live_page(self):
        page = W.QWidget()
        page.setObjectName("pageHost")
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        content = W.QHBoxLayout()
        content.setSpacing(12)
        left = W.QVBoxLayout()
        left.setSpacing(12)
        left.addLayout(self._metric_row())
        left.addWidget(self._live_plot_card(), 1)
        content.addLayout(left, 1)
        content.addWidget(self._live_panel())
        layout.addLayout(content, 1)
        layout.addWidget(self._command_bar())
        return page

    def _metric_row(self):
        row = W.QHBoxLayout()
        row.setSpacing(12)
        self.metric_cards = []
        self.live_channel_checks = []
        self.metric_titles, self.metric_values, self.metric_subs = [], [], []
        for i in range(4):
            metric = MetricCard(f"AI {i}", self.p.channels[i])
            row.addWidget(metric)
            self.metric_cards.append(metric)
            self.metric_titles.append(metric.title_label)
            self.metric_values.append(metric.value_label)
            self.metric_subs.append(metric.sub_label)
            self.live_channel_checks.append(metric.visibility_button)
            metric.visibility_button.toggled.connect(self._live_visibility_changed)
        return row

    def _live_plot_card(self):
        plot_card, pl = card(margins=(15, 12, 15, 12), spacing=8)
        header = W.QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(section_title("实时波形", "wave"))
        header.addStretch()
        self.zero_button = chip_button("当前载荷归零", "zero", self.zero_channels)
        self.zero_button.setToolTip("对所有已启用通道，以最近最多 1 秒原始电压的平均值设置零点，原始记录不变。")
        header.addWidget(self.zero_button)
        self.undo_zero_button = chip_button("撤销调零", "undo", self.undo_zero)
        self.undo_zero_button.setToolTip("恢复上次调零前的零点；停止采集后仍可撤销，记录过程中不可修改。")
        self.undo_zero_button.setEnabled(False)
        header.addWidget(self.undo_zero_button)
        header.addWidget(chip_button("保存图像", "save",
                                     lambda: self.save_plot_image(self.live_plot)))
        header.addWidget(chip_button("适应窗口", "fit", self._fit_live_plot))
        pl.addLayout(header)
        controls = W.QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(field_label("时间轴"))
        self.live_time_axis = W.QComboBox()
        for title, mode in (("相对时间", "relative"), ("绝对时间", "absolute"), ("样本号", "samples")):
            self.live_time_axis.addItem(title, mode)
        self.live_time_axis.currentIndexChanged.connect(lambda: self._time_axis_changed("live"))
        controls.addWidget(self.live_time_axis)
        controls.addSpacing(6)
        controls.addWidget(field_label("读数"))
        self.live_value_mode = W.QComboBox()
        for title, mode in (("最新值", "last"), ("首值", "first"), ("均值", "mean"), ("最小值", "min"),
                            ("最大值", "max"), ("RMS", "rms"), ("标准差", "std"), ("峰峰值", "delta")):
            self.live_value_mode.addItem(title, mode)
        controls.addWidget(self.live_value_mode)
        controls.addSpacing(6)
        controls.addWidget(field_label("小数位"))
        self.value_decimals = W.QSpinBox()
        self.value_decimals.setRange(0, 9)
        self.value_decimals.setValue(3)
        self.value_decimals.setFixedWidth(50)
        controls.addWidget(self.value_decimals)
        controls.addWidget(field_label("窗口"))
        self.live_seconds = W.QDoubleSpinBox()
        self.live_seconds.setDecimals(1)
        self.live_seconds.setRange(.1, 60)
        self.live_seconds.setValue(10)
        self.live_seconds.setSingleStep(1)
        self.live_seconds.setSuffix(" s")
        self.live_seconds.setFixedWidth(90)
        self.live_seconds.valueChanged.connect(self._live_window_changed)
        controls.addWidget(self.live_seconds)
        controls.addStretch()
        self.pause_plot = W.QCheckBox("暂停滚动")
        self.pause_plot.setToolTip("只暂停曲线显示；采集和文件记录会继续。")
        pl.addLayout(controls)
        self.live_plot = plot_widget()
        self.live_plot.setXRange(0, self.live_seconds.value(), padding=0)
        self.live_plot.getViewBox().sigRangeChangedManually.connect(self._live_range_changed)
        self.live_plot.plotItem.setDownsampling(auto=True, mode="peak")
        self.live_curves = [self.live_plot.plot(pen=pg.mkPen(c, width=1), name=f"AI {i}",
                                                autoDownsample=True, autoDownsampleFactor=1.0,
                                                downsampleMethod="peak")
                            for i, c in enumerate(self.p.channels)]
        pl.addWidget(self.live_plot, 1)
        footer = W.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(self.pause_plot)
        pl.addLayout(footer)
        self.live_info = label("时间轴来自样本序号 · 鼠标拖动平移，滚轮缩放", "muted", True)
        pl.addWidget(self.live_info)
        return plot_card

    def _live_panel(self):
        scroll = W.QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(320)
        scroll.setSizePolicy(W.QSizePolicy.Policy.Expanding, W.QSizePolicy.Policy.Expanding)
        scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = W.QWidget()
        content.setObjectName("panelContent")
        column = W.QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addWidget(self._acquisition_section())
        column.addWidget(self._channel_section())
        column.addWidget(self._config_section())
        column.addStretch()
        scroll.setWidget(content)
        return scroll

    def _output_page(self):
        scroll = W.QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        content = W.QWidget()
        content.setObjectName("pageHost")
        layout = W.QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        self.output_panel = OutputPanel(self.start_output, self.stop_output)
        self.output_panel.setFixedWidth(560)
        self.output_panel.set_expanded(True)
        layout.addWidget(self.output_panel, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        scroll.setWidget(content)
        return scroll

    def _acquisition_section(self):
        section = CollapsibleSection("采集参数", "settings", expanded=True)
        self.experiment = W.QLineEdit("力传感器实验")
        self.experiment.setToolTip("用于记录文件夹名称、自动分组编号和实验信息，不改变采样或传感器参数。")
        section.add(field("实验名称", self.experiment))
        # Retain internal sources for tests and --demo; the acquisition panel is hardware-only.
        self.input_controls = W.QWidget(self)
        self.input_controls.hide()
        inputs = W.QVBoxLayout(self.input_controls)
        self.scenario = W.QComboBox()
        self.scenario.addItem("拖拽载荷 · 周期接触", "drag")
        self.scenario.addItem("正弦 · 频谱验证", "sine")
        self.scenario.addItem("阶跃 · 零点与响应", "step")
        self.scenario.addItem("可配置电信号 · 电压模拟", "custom")
        self.scenario.addItem("原始 HPF · 按时间回放", "hpf")
        self.scenario.addItem("独立核心 · 电信号模拟", "rust")
        self.scenario.addItem("DT9837C · 真实 USB 输入", "usb")
        self.scenario.setCurrentIndex(self.scenario.findData("usb"))
        inputs.addWidget(field("输入来源", self.scenario))
        self.input_button = button("配置电信号", self.edit_input)
        self.input_button.setIcon(theme.icon("settings", self.p.text_muted, 15))
        self.input_button.setIconSize(QtCore.QSize(15, 15))
        inputs.addWidget(self.input_button)
        self.hpf_loop = W.QCheckBox("循环回放（首尾会产生接缝）")
        inputs.addWidget(self.hpf_loop)
        self.input_info = label("", "muted", True)
        inputs.addWidget(self.input_info)
        self.rate = W.QComboBox()
        for rate in (500, 1000, 2000, 5000, 10000, 20000, 50000, 100000):
            self.rate.addItem(f"{rate / 1000:g} kS/s" + (" · 推荐" if rate == 5000 else ""), float(rate))
        self.rate.setCurrentIndex(self.rate.findData(5000.0))
        self.rate.currentIndexChanged.connect(self._update_live_window_limit)
        self.rate.setToolTip("每通道同时采样。5 kS/s 为通用默认档；更高采样率适用于更快的信号，也会增加记录文件大小。")
        section.add(field("采样率 / 每通道", self.rate))
        self.actual_rate = label("实际时钟：开始后显示", "muted")
        self.actual_rate.setToolTip("此处显示 PLL 计算出的名义采样率；晶振误差和真实时序尚未测量。")
        section.add(self.actual_rate)
        self.device_button = button("连接设备", self.toggle_device_connection, "ghost")
        self.device_button.setIcon(theme.icon("link", self.p.text_muted, 15))
        self.device_button.setIconSize(QtCore.QSize(15, 15))
        section.add(self.device_button)
        return section

    def _channel_section(self):
        section = CollapsibleSection("通道与传感器", "chip", expanded=True)
        self.channel_summary_rows, self.channel_summary_names, self.channel_summary_meta = [], [], []
        for i in range(4):
            row = W.QWidget()
            row.setObjectName("transparentHost")
            layout = W.QHBoxLayout(row)
            layout.setContentsMargins(0, 1, 0, 1)
            layout.setSpacing(8)
            dot = status_dot(self.p.channels[i], 8)
            name = label(f"AI {i}", "muted")
            meta = label("", "faint")
            layout.addWidget(dot)
            layout.addWidget(name, 1)
            layout.addWidget(meta)
            section.add(row)
            self.channel_summary_rows.append(row)
            self.channel_summary_names.append(name)
            self.channel_summary_meta.append(meta)
        self.channel_button = button("编辑通道与传感器", self.edit_channels)
        self.channel_button.setIcon(theme.icon("settings", self.p.text_muted, 15))
        self.channel_button.setIconSize(QtCore.QSize(15, 15))
        section.add(self.channel_button)
        section.add(label("波形区可统一调零或撤销调零；原始电压不变。", "faint", True))
        return section

    def _config_section(self):
        section = CollapsibleSection("配置与存储", "folder", expanded=False)
        buttons = W.QHBoxLayout()
        buttons.setSpacing(8)
        self.save_config_button = button("保存配置", self.save_profile, "ghost")
        self.save_config_button.setIcon(theme.icon("save", self.p.text_muted, 15))
        self.load_config_button = button("载入配置", self.load_profile, "ghost")
        self.load_config_button.setIcon(theme.icon("open", self.p.text_muted, 15))
        for widget in (self.save_config_button, self.load_config_button):
            widget.setIconSize(QtCore.QSize(15, 15))
        buttons.addWidget(self.save_config_button)
        buttons.addWidget(self.load_config_button)
        section.add_layout(buttons)
        section.add(divider())
        section.add(field_label("记录目录"))
        self.directory_label = label(str(self.recording_root), "muted", True)
        self.directory_label.setToolTip(str(self.recording_root))
        section.add(self.directory_label)
        self.disk_space_label = label("", "muted")
        section.add(self.disk_space_label)
        self._refresh_disk_space()
        self.directory_button = button("更改目录", self.choose_directory, "ghost")
        self.directory_button.setIcon(theme.icon("folder", self.p.text_muted, 15))
        self.directory_button.setIconSize(QtCore.QSize(15, 15))
        section.add(self.directory_button)
        return section

    def _transport_button(self, text, icon_name, color, name="", checkable=False):
        result = W.QPushButton(text)
        result.setObjectName(name or "transport")
        result.setIcon(theme.icon(icon_name, color, 16))
        result.setIconSize(QtCore.QSize(16, 16))
        result.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        result.setCheckable(checkable)
        result.setMinimumHeight(36)
        return result

    def _command_bar(self):
        bar = W.QFrame()
        bar.setObjectName("commandBar")
        layout = W.QVBoxLayout(bar)
        layout.setContentsMargins(12, 9, 16, 9)
        layout.setSpacing(7)
        row = W.QHBoxLayout()
        row.setSpacing(8)
        self.preview_button = self._transport_button("开始预览", "play", self.p.accent_text, "primary")
        self.preview_button.clicked.connect(self.start_preview)
        self.record_button = self._transport_button("开始记录", "record", self.p.danger, "record", True)
        self.record_button.clicked.connect(self.toggle_record)
        self.pause_record_button = self._transport_button("暂停记录", "pause", self.p.text_muted)
        self.pause_record_button.clicked.connect(self.toggle_record_pause)
        self.stop_button = self._transport_button("停止采集", "stop", self.p.text_muted)
        self.stop_button.clicked.connect(self.stop_acquisition)
        for widget in (self.preview_button, self.record_button, self.pause_record_button, self.stop_button):
            row.addWidget(widget)
        row.addSpacing(4)
        row.addWidget(theme.vdivider())
        row.addSpacing(4)
        row.addStretch()
        row.addWidget(field_label("记录时长"))
        self.duration_mode = W.QComboBox()
        for text, seconds in (("手动结束", None), ("10 秒", 10), ("30 秒", 30),
                              ("1 分钟", 60), ("5 分钟", 300), ("10 分钟", 600), ("自定义", "custom")):
            self.duration_mode.addItem(text, seconds)
        self.duration_mode.setFixedWidth(110)
        self.duration_input = W.QDoubleSpinBox()
        self.duration_input.setDecimals(3)
        self.duration_input.setRange(.001, 999999999)
        self.duration_input.setValue(60)
        self.duration_input.setSuffix(" s")
        self.duration_input.setFixedWidth(125)
        self.duration_input.hide()
        self.duration_mode.setToolTip("设置本组记录时长；达到时长后自动保存，预览继续。暂停记录期间不计时。")
        self.duration_mode.currentIndexChanged.connect(self._duration_changed)
        self.duration_input.valueChanged.connect(self._duration_changed)
        row.addWidget(self.duration_mode)
        row.addWidget(self.duration_input)
        layout.addLayout(row)
        summary = W.QHBoxLayout()
        self.status_dot_label = status_dot(self.p.text_faint, 8)
        self.status_label = status_chip("未开始", "neutral")
        self.count_label = label("0 扫描 · 0.00 s", "muted")
        summary.addWidget(self.status_dot_label)
        summary.addWidget(self.status_label)
        summary.addStretch()
        summary.addWidget(self.count_label)
        layout.addLayout(summary)
        theme.soft_shadow(bar, blur=24, dy=7, strength=16)
        return bar

    # ------------------------------------------------------------------
    # Analysis page

    def _analysis_page(self):
        page = W.QWidget()
        page.setObjectName("pageHost")
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        controls, cl = card(margins=(16, 13, 16, 14), spacing=10)
        header = W.QHBoxLayout()
        header.addWidget(section_title("分析设置", "stats"))
        header.addStretch()
        header.addWidget(label("窗口低通只影响分析，不修改记录", "faint"))
        cl.addLayout(header)
        self.analysis_source = W.QComboBox()
        self.analysis_source.addItems(["实时采样窗口", "回看中的数据窗口"])
        self.analysis_channel = W.QComboBox()
        self.reference_channel = W.QComboBox()
        for channel_combo in (self.analysis_channel, self.reference_channel):
            channel_combo.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.analysis_mode = W.QComboBox()
        self.analysis_mode.addItems(["幅值谱 · 峰值", "功率谱密度 PSD", "频率响应 H1 / 相干性", "时域波形 / 测量游标"])
        self.analysis_mode.setCurrentIndex(3)
        self.window_type = W.QComboBox()
        self.window_type.addItems(["hann", "boxcar", "hamming", "blackman", "flattop"])
        top_row = W.QHBoxLayout()
        top_row.setSpacing(12)
        top_row.addWidget(field("数据来源", self.analysis_source), 1)
        top_row.addWidget(field("分析通道", self.analysis_channel), 1)
        top_row.addWidget(field("H1 参考通道", self.reference_channel), 1)
        top_row.addWidget(field("分析类型", self.analysis_mode), 1)
        top_row.addWidget(field("窗函数", self.window_type), 1)
        cl.addLayout(top_row)
        second = W.QHBoxLayout()
        second.setSpacing(10)
        self.lowpass = W.QCheckBox("窗口零相位低通")
        self.lowpass.setToolTip("4 阶 Butterworth 前后向零相位滤波：有效阶数 8，填写频率处总响应约 −6 dB。"
                                "仅分析窗口使用，不修改记录。")
        second.addWidget(self.lowpass)
        self.cutoff = W.QDoubleSpinBox()
        self.cutoff.setRange(0.1, 50000)
        self.cutoff.setValue(100)
        self.cutoff.setSuffix(" Hz")
        second.addWidget(self.cutoff)
        second.addStretch()
        cl.addLayout(second)
        layout.addWidget(controls)
        plot_card, pl = card(margins=(15, 12, 15, 12), spacing=8)
        plot_header = W.QHBoxLayout()
        plot_header.addWidget(section_title("分析结果", "wave"))
        plot_header.addStretch()
        plot_header.addWidget(label("原始文件保持不变", "faint"))
        pl.addLayout(plot_header)
        self.analysis_plot = plot_widget("幅值", "N")
        self.analysis_plot.setLabel("bottom", "频率", units="Hz")
        self.analysis_curve = self.analysis_plot.plot(pen=pg.mkPen(self.p.channels[0], width=1))
        pl.addWidget(self.analysis_plot, 1)
        self.coherence_plot = plot_widget("相干性", "")
        self.coherence_plot.setLabel("bottom", "频率", units="Hz")
        self.coherence_plot.setMaximumHeight(170)
        self.coherence_plot.setYRange(0, 1)
        self.coherence_curve = self.coherence_plot.plot(pen=self.p.channels[1])
        pl.addWidget(self.coherence_plot)
        self.coherence_plot.hide()
        self.analysis_measurements = PlotMeasurements(self.analysis_plot)
        self.analysis_measurements.channel_combo.hide()
        self.analysis_freeze = W.QCheckBox("冻结测量窗口")
        self.analysis_freeze.setToolTip("冻结此页的时域波形，方便拖动游标；采集和记录继续。")
        self.analysis_freeze.toggled.connect(self.refresh_analysis)
        self.analysis_measurements.layout().itemAt(0).layout().addWidget(self.analysis_freeze)
        self.analysis_measurements.hide()
        pl.addWidget(self.analysis_measurements)
        self.analysis_info = label("开始预览后可分析。幅值谱保留 DC；低通仅用于此页，不改变记录的原始电压。",
                                   "muted", True)
        pl.addWidget(self.analysis_info)
        layout.addWidget(plot_card, 1)
        for widget in [self.analysis_channel, self.reference_channel, self.analysis_mode, self.window_type]:
            widget.currentIndexChanged.connect(self.refresh_analysis)
        self.lowpass.toggled.connect(self.refresh_analysis)
        self.cutoff.valueChanged.connect(self.refresh_analysis)
        self.analysis_source.currentIndexChanged.connect(self._analysis_source_changed)
        return page

    # ------------------------------------------------------------------
    # Replay page

    def _replay_page(self):
        page = W.QWidget()
        page.setObjectName("pageHost")
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        file_card, fl = card(margins=(16, 13, 16, 13), spacing=10)
        header = W.QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(section_title("记录回看", "history"))
        header.addStretch()
        header.addWidget(chip_button("记录库", "library", self.open_recording_library))
        header.addWidget(chip_button("打开记录文件夹", "folder", self.choose_replay))
        header.addWidget(chip_button("打开 HPF", "open", self.choose_hpf))
        header.addWidget(chip_button("最近记录", "history", self.open_last_recording))
        fl.addLayout(header)
        self.replay_info = label("支持原始记录、同步电压 HPF 和已验证的 DT9837C 力记录；按 HPF 原配置换算。",
                                 "notice", True)
        fl.addWidget(self.replay_info)
        options_row = W.QHBoxLayout()
        options_row.setSpacing(10)
        options_row.addWidget(field_label("显示与取数通道"))
        self.replay_channel_checks = []
        for i in range(4):
            check = W.QCheckBox(f"AI {i}")
            check.setEnabled(False)
            check.toggled.connect(self.draw_replay)
            self.replay_channel_checks.append(check)
            options_row.addWidget(check)
        options_row.addStretch()
        options_row.addWidget(field_label("时间轴"))
        self.replay_time_axis = W.QComboBox()
        for title, mode in (("相对时间", "relative"), ("绝对时间（电脑时钟）", "absolute"), ("样本号", "samples")):
            self.replay_time_axis.addItem(title, mode)
        self.replay_time_axis.currentIndexChanged.connect(lambda: self._time_axis_changed("replay"))
        options_row.addWidget(self.replay_time_axis)
        options_row.addWidget(field_label("显示时长"))
        self.replay_seconds = W.QDoubleSpinBox()
        self.replay_seconds.setRange(.002, 60)
        self.replay_seconds.setDecimals(3)
        self.replay_seconds.setValue(10)
        self.replay_seconds.setSuffix(" s")
        self.replay_seconds.valueChanged.connect(self.draw_replay)
        options_row.addWidget(self.replay_seconds)
        fl.addLayout(options_row)
        layout.addWidget(file_card)
        plot_card, pl = card(margins=(15, 12, 15, 12), spacing=8)
        plot_header = W.QHBoxLayout()
        plot_header.setSpacing(8)
        plot_header.addWidget(section_title("回看波形", "wave"))
        plot_header.addStretch()
        self.statistics_button = chip_button("统计所选数据", "stats", self.show_replay_statistics)
        plot_header.addWidget(chip_button("导出数据", "export", self.export_replay))
        plot_header.addWidget(self.statistics_button)
        plot_header.addWidget(chip_button("保存图像", "save",
                                          lambda: self.save_plot_image(self.replay_plot)))
        plot_header.addWidget(chip_button("分析此窗口", "wave", self.analyze_replay))
        pl.addLayout(plot_header)
        self.replay_plot = plot_widget()
        replay_legend = self.replay_plot.addLegend(offset=(10, 8), **theme.legend_style(self.p))
        replay_legend.setLabelTextColor(self.p.text_muted)
        replay_legend.hide()
        self.replay_curves = [self.replay_plot.plot(pen=pg.mkPen(c, width=1), name=f"AI {i}",
                                                    autoDownsample=True, downsampleMethod="peak")
                              for i, c in enumerate(self.p.channels)]
        self.replay_region = pg.LinearRegionItem(values=(0, 1), brush=theme.alpha(self.p.accent, 0.12),
                                                 pen=pg.mkPen(self.p.accent, width=1))
        self.replay_plot.addItem(self.replay_region, ignoreBounds=True)
        self.replay_region.hide()
        self.replay_region.sigRegionChangeFinished.connect(self._replay_region_changed)
        pl.addWidget(self.replay_plot, 1)
        self.replay_legend_row = W.QHBoxLayout()
        self.replay_legend_row.setSpacing(16)
        pl.addLayout(self.replay_legend_row)
        self.replay_measurements = PlotMeasurements(self.replay_plot)
        pl.addWidget(self.replay_measurements)
        transport = W.QHBoxLayout()
        transport.setSpacing(8)
        transport.addWidget(chip_button("上一窗口", "chevron_left", lambda: self.step_replay(-1)))
        transport.addWidget(chip_button("下一窗口", "chevron_right", lambda: self.step_replay(1)))
        transport.addSpacing(4)
        transport.addWidget(field_label("起点"))
        self.replay_start = W.QDoubleSpinBox()
        self.replay_start.setDecimals(9)
        self.replay_start.setRange(0, 0)
        self.replay_start.setSuffix(" s")
        self.replay_start.valueChanged.connect(self.draw_replay)
        transport.addWidget(self.replay_start)
        self.replay_slider = W.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.replay_slider.setRange(0, 1000)
        self.replay_slider.valueChanged.connect(self._replay_slider_changed)
        transport.addWidget(self.replay_slider, 1)
        self.replay_position = label("0.00 s", "muted")
        transport.addWidget(self.replay_position)
        pl.addLayout(transport)
        selection = W.QHBoxLayout()
        selection.setSpacing(8)
        self.replay_select = W.QCheckBox("使用选区")
        self.replay_select.toggled.connect(self._replay_selection_changed)
        selection.addWidget(self.replay_select)
        self.selection_start, self.selection_stop = W.QDoubleSpinBox(), W.QDoubleSpinBox()
        for spin in (self.selection_start, self.selection_stop):
            spin.setDecimals(9)
            spin.setRange(0, 0)
            spin.setSuffix(" s")
            spin.setEnabled(False)
            spin.valueChanged.connect(self._replay_selection_changed)
        selection.addWidget(self.selection_start)
        selection.addWidget(label("至", "muted"))
        selection.addWidget(self.selection_stop)
        selection.addWidget(chip_button("选当前窗口", "fit", self.select_replay_window))
        selection.addStretch()
        selection.addWidget(label("统计与导出使用勾选通道；未启用选区时处理全记录。", "faint"))
        pl.addLayout(selection)
        layout.addWidget(plot_card, 1)
        return page

    # ------------------------------------------------------------------
    # Device page

    def _device_page(self):
        page = W.QWidget()
        page.setObjectName("pageHost")
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        device_card, dl = card(margins=(16, 13, 16, 14), spacing=10)
        dl.addWidget(section_title("设备连接", "usb"))
        device_body = W.QHBoxLayout()
        device_body.setSpacing(20)
        left = W.QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(label("真实输入由独立核心连接 DT9837C。选择真实输入后可先连接设备，再开始预览；"
                             "停止采集保持连接，断开设备关闭工作电源。扫描不改变设备配置。", "notice", True))
        row = W.QHBoxLayout()
        row.setSpacing(8)
        scan = button("查找 DT USB 设备", self.scan_devices, "primary")
        scan.setIcon(theme.icon("refresh", self.p.accent_text, 15))
        boot = button("冷启动离线验证", self.simulate_boot)
        boot.setIcon(theme.icon("play", self.p.text_muted, 15))
        notes = button("打开技术说明", lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(ROOT / "docs" / "开发文档" / "technical-notes.md"))), "ghost")
        notes.setIcon(theme.icon("open", self.p.text_muted, 15))
        for widget in (scan, boot, notes):
            widget.setIconSize(QtCore.QSize(15, 15))
        row.addWidget(scan)
        row.addWidget(boot)
        row.addWidget(notes)
        row.addStretch()
        left.addLayout(row)
        left.addStretch()
        right = W.QVBoxLayout()
        right.setSpacing(8)
        self.device_result = label("尚未扫描 · 扫描只读取设备描述符，不更换驱动或初始化设备。", "muted", True)
        right.addWidget(self.device_result)
        self.boot_result = label("冷启动验证使用已归档固件记录，只在内存中模拟 RAM 写入与重新枚举。", "muted", True)
        right.addWidget(self.boot_result)
        right.addStretch()
        device_body.addLayout(left, 3)
        device_body.addLayout(right, 2)
        dl.addLayout(device_body)
        layout.addWidget(device_card)
        comm_card, cl = card(margins=(16, 13, 16, 14), spacing=10)
        cl.addWidget(section_title("通信观察", "stats"))
        self.protocol_stats = label("开始预览后可查看二进制命令与传输计数。", "muted")
        cl.addWidget(self.protocol_stats)
        self.protocol_log = W.QPlainTextEdit()
        self.protocol_log.setReadOnly(True)
        cl.addWidget(self.protocol_log, 1)
        fault_row = W.QHBoxLayout()
        fault_row.setSpacing(8)
        fault_row.addWidget(field_label("模拟故障"))
        self.fault = W.QComboBox()
        self.fault.addItem("传输超时", "timeout")
        self.fault.addItem("设备断连", "disconnect")
        self.fault.addItem("数据溢出", "overrun")
        fault_row.addWidget(self.fault)
        self.fault_button = button("注入一次故障", self.inject_fault)
        self.fault_button.setIcon(theme.icon("warning", self.p.warning, 15))
        self.fault_button.setIconSize(QtCore.QSize(15, 15))
        fault_row.addWidget(self.fault_button)
        fault_row.addStretch()
        fault_row.addWidget(label("故障注入只适用于正在运行的协议模拟输入。", "faint"))
        cl.addLayout(fault_row)
        layout.addWidget(comm_card, 1)
        layout.addWidget(label("左侧“信号输出”页面提供 AO 控制；外部触发和多设备同步尚未开放。",
                               "faint", True))
        return page

    def _selected_settings(self):
        return [self.channels[i] for i in self.active_channels]

    def _input_changed(self, *_):
        self._discard_preview()
        mode = self.scenario.currentData()
        self.input_button.setVisible(mode in ("custom", "rust", "hpf"))
        self.input_button.setText("选择原始 HPF" if mode == "hpf" else "配置电信号")
        self.hpf_loop.setVisible(mode == "hpf")
        if mode == "hpf" and self.input_hpf is not None:
            self._apply_hpf_settings()
        self._refresh_input_info()
        self._update_mode_badge()
        self._update_live_window_limit()
        self._refresh_buttons()

    def _refresh_input_info(self):
        mode = self.scenario.currentData()
        if mode == "hpf":
            self.input_info.setText(f"{self.input_hpf.path.name}\n保留原始电压、采样率及换算参数。"
                                    if self.input_hpf else "请选择原始 HPF；默认播完即停。")
        elif mode == "usb":
            self.input_info.setText("连接设备后保持绿灯；预览或记录时采集。停止采集保持连接，断开设备后关电源。")
        elif mode == "rust":
            self.input_info.setText("独立核心持续采样和保存。关闭窗口后继续；重新打开可接回同组。")
        elif mode == "custom":
            self.input_info.setText("参数单位为 V；按通道设置换算显示。固定随机种子可重复生成。")
        else:
            self.input_info.setText("预设协议模拟；无硬件访问。")

    def edit_input(self):
        if self.scenario.currentData() == "hpf":
            path, _ = W.QFileDialog.getOpenFileName(self, "选择作为输入的原始 HPF", str(ROOT), "QuickDAQ 数据 (*.hpf)")
            if path:
                try:
                    self.load_input_hpf(Path(path))
                except (OSError, ValueError, KeyError) as exc:
                    self.tell(f"HPF 输入无法打开：{exc}")
        else:
            dialog = SignalDialog(self.signal_settings, self)
            if dialog.exec() == W.QDialog.DialogCode.Accepted:
                self._discard_preview()
                self.signal_settings = dialog.settings
                if dialog.as_voltage.isChecked():
                    for index in self.active_channels:
                        self.channels[index] = replace(self.channels[index], unit="V", zero_volts=0, inverted=False)
                self._sync_channel_labels()
                self.tell("模拟电信号已配置；开始预览或记录后生效。")

    @staticmethod
    def _hpf_config(reader):
        values = dict(reader.metadata.get("acquisition_config") or {})
        values["sample_rate"] = reader.sample_rate
        values["channels"] = tuple(c.index for c in reader.channels)
        for field in ("ranges", "coupling", "iepe"):
            if field in values:
                values[field] = tuple(values[field])
        return AcquisitionConfig(**{k: v for k, v in values.items() if k in AcquisitionConfig.__dataclass_fields__})

    def load_input_hpf(self, path):
        from .hpf import open_hpf
        reader = open_hpf(path, waveform_only=True)
        if not reader.voltage_compatible:
            raise ValueError(reader.voltage_replay_error)
        config = self._hpf_config(reader)
        if not reader.sample_count:
            raise ValueError("HPF 没有可回放的样本")
        self._discard_preview()
        self.input_hpf, self.input_hpf_config = reader, config
        self.scenario.setCurrentIndex(self.scenario.findData("hpf"))
        self._apply_hpf_settings()
        self._refresh_input_info()
        self._refresh_buttons()
        self.tell(f"HPF 输入已载入 · {reader.sample_count:,} 帧 · "
                  "只读波形与换算配置；附加显示/统计块保留说明，不作为新采集。")

    def _apply_hpf_settings(self):
        config = self.input_hpf_config
        self.active_channels = tuple(config.channels)
        for channel in self.input_hpf.channels:
            self.channels[channel.index] = replace(channel)
        self.ranges, self.coupling, self.iepe = list(config.ranges), list(config.coupling), list(config.iepe)
        self._set_sample_rate(config.sample_rate)  # Replay keeps the file's full precision.
        self._sync_channel_labels()

    def _sync_channel_labels(self):
        for i, c in enumerate(self.channels):
            card = self.metric_cards[i]
            card.title_label.setText(f"AI {i}")
            card.name_label.setText(c.name)
            card.unit_label.setText(c.unit)
            self.channel_summary_names[i].setText(f"AI {i} · {c.name}")
            self.channel_summary_meta[i].setText(f"{c.unit} · ±{self.ranges[i]:g} V")
            active = i in self.active_channels
            self.channel_summary_rows[i].setEnabled(active)
            check = self.live_channel_checks[i]
            previously_enabled = check.isEnabled()
            with QtCore.QSignalBlocker(check):
                check.setEnabled(active)
                if not active or not previously_enabled:
                    check.setChecked(active)
            check.setText("显示" if check.isChecked() else "隐藏")
            check.setVisible(active)
            check.setToolTip(f"{c.name} · {c.unit}。只控制曲线显示，不改变采集和记录。")
            self.live_curves[i].setVisible(active and check.isChecked())
            if active:
                card.sparkline.setVisible(True)
            else:
                self.metric_values[i].setText("—")
                self.metric_subs[i].setText("未启用")
                card.sparkline.setVisible(False)
                card.sparkline.set_values([])
        self._analysis_source_changed()

    def _analysis_source_changed(self, *_):
        self.analysis_freeze.setChecked(False)
        settings = self.replay.channels if self.analysis_source.currentIndex() == 1 and self.replay is not None else self._selected_settings()
        for combo in [self.analysis_channel, self.reference_channel]:
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for channel in settings:
                combo.addItem(f"AI {channel.index} · {channel.name}", channel.index)
            index = combo.findData(previous)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        if len(settings) > 1 and self.analysis_channel.currentIndex() == self.reference_channel.currentIndex():
            self.reference_channel.setCurrentIndex(1)
        self.refresh_analysis()
        self._update_mode_badge()

    def _update_mode_badge(self):
        index = self.pages.currentIndex()
        file_view = index == 3 or (index == 2 and self.analysis_source.currentIndex() == 1)
        if file_view and self.replay is not None:
            text = ("HPF 文件 · 离线回放" if self.replay_is_hpf else
                    "记录回放 · " + ("模拟数据" if self.replay.metadata.get("source") == "simulation" else "文件数据"))
            tone = "info"
        elif self.scenario.currentData() == "hpf":
            text, tone = "HPF 输入回放 · 非现场采集", "warning"
        elif self.scenario.currentData() == "rust":
            text, tone = "独立核心模拟 · 非真实测量", "info"
        elif self.scenario.currentData() == "usb":
            remote = self.engine if isinstance(self.engine, RemoteEngine) else None
            if self._connecting:
                text, tone = "DT9837C · 正在连接", "info"
            elif remote and remote.connection_error:
                text, tone = "DT9837C · 连接状态未知", "danger"
            elif self._device_error or (remote and remote.error):
                text, tone = "DT9837C · 连接异常", "danger"
            elif remote and remote.device_connected:
                text, tone = ("DT9837C · USB 采集中" if remote.running else "DT9837C · USB 待机"), "success"
            else:
                text, tone = "DT9837C · 未连接", "neutral"
        else:
            text, tone = "协议模拟 · 非真实测量", "warning"
        self.mode_badge.setText(text)
        theme.set_tone(self.mode_badge, tone)

    def tell(self, text, tone=None, *, error_title="操作失败"):
        self.notice.setText(text)
        if tone is None:
            tone = ("danger" if any(word in text for word in ("失败", "无法", "错误", "中止", "中断", "异常"))
                    else "warning" if any(word in text for word in ("请先", "尚未", "未", "不可", "不能"))
                    else "neutral")
        color = {"danger": self.p.danger, "warning": self.p.warning,
                 "success": self.p.success, "info": self.p.accent}.get(tone, self.p.text_muted)
        icon_name = {"danger": "error", "warning": "warning",
                     "success": "check", "info": "info"}.get(tone, "info")
        self.notice.setStyleSheet(f"color: {color};")
        self.notice_icon.setPixmap(theme.icon(icon_name, color, 14).pixmap(14, 14))
        if tone == "danger":
            self.error_title.setText(error_title)
            self.error_details.setText(text)
            self.error_banner.show()

    def start_preview(self):
        self._begin_input()

    def toggle_device_connection(self):
        try:
            if self.engine and self.engine.running:
                raise RuntimeError("请先停止采集，再断开设备")
            if isinstance(self.engine, RemoteEngine) and self.engine.device_connected:
                self.engine.disconnect()
                self.tell("设备已断开，工作电源已关闭。")
            else:
                self.reconnect_device()
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell(f"设备连接操作失败：{exc}")
        self._refresh_buttons()

    def auto_connect_device(self):
        if self.scenario.currentData() != "usb":
            return
        if self.engine is not None and (self.engine.running or self.engine.device_connected):
            return
        self._connect_device(scan_first=True)

    def reconnect_device(self):
        self._connect_device(scan_first=False)

    def _connect_device(self, *, scan_first):
        if self._connecting or (self.engine is not None and self.engine.running):
            return
        config = AcquisitionConfig(sample_rate=self.rate.currentData(), channels=self.active_channels,
                                   ranges=tuple(self.ranges), coupling=tuple(self.coupling), iepe=tuple(self.iepe))
        parameters = dict(config=config, channels=[replace(c) for c in self.channels],
                          signal_settings=self.signal_settings, experiment=self.experiment.text(),
                          recording_root=self.recording_root, endpoint=self.core_endpoint)
        self._connecting = True
        self._device_error = ""
        self.tell("正在扫描并连接 DT9837C，连接后保持待机，不会自动采集。", "info")
        self._device_worker = DeviceConnectionWorker(parameters, scan_first, self)
        self._device_worker.finished.connect(self._device_connection_finished)
        self._refresh_buttons()
        self._device_worker.start()

    def _device_connection_finished(self):
        worker = self._device_worker
        self._connecting = False
        if worker.engine is not None:
            previous = self.engine
            self.engine = worker.engine
            self._preview_invalidated = False
            if isinstance(previous, RemoteEngine):
                previous.detach()
            self._device_error = ""
            self._error_shown = ""
            self.error_banner.hide()
            self.actual_rate.setText(f"名义采样率：{self.engine.sample_rate:,.3f} S/s")
            self._update_live_window_limit()
            self.tell("设备已连接并待机。点击“开始预览”采集，点击“开始记录”保存数据。", "success")
        elif worker.error:
            self._device_error = worker.error
            self.tell(f"设备连接失败：{worker.error}", error_title="设备连接失败 · 检查 USB 后点击“重新连接”")
        else:
            self.tell("未检测到 DT9837C。插入设备后，点击左下角“重新连接”。", "info")
        self._device_worker = None
        worker.deleteLater()
        self._refresh_buttons()
        if self._close_after_connect:
            self.close()

    def _begin_input(self, recording_path=None, *, recording_options=None):
        if self._connecting or (self.engine is not None and self.engine.running):
            return
        previous = self.engine
        if self.scenario.currentData() == "usb" and not (
                isinstance(previous, RemoteEngine) and previous.device_connected):
            self.tell("请先点击左下角“重新连接”，设备进入绿色待机后再开始预览或记录。", "warning")
            return
        try:
            if previous is not None and not isinstance(previous, RemoteEngine):
                previous.stop()
            mode = self.scenario.currentData()
            session = None
            if mode == "hpf":
                if self.input_hpf is None:
                    raise ValueError("请先选择原始 HPF")
                config = self.input_hpf_config
                session = HpfReplaySession(self.input_hpf, loop=self.hpf_loop.isChecked())
            else:
                config = AcquisitionConfig(sample_rate=self.rate.currentData(), channels=self.active_channels,
                                           ranges=tuple(self.ranges), coupling=tuple(self.coupling), iepe=tuple(self.iepe))
            if mode in ("rust", "usb"):
                engine = RemoteEngine.start_new(config, self.channels, self.signal_settings,
                                                      self.experiment.text(), self.recording_root, recording_path,
                                                      endpoint=self.core_endpoint,
                                                      source="usb" if mode == "usb" else "simulation",
                                                      recording_options=recording_options or self._recording_options())
            else:
                engine = AcquisitionEngine(config, self._selected_settings(), mode,
                                                signal_settings=self.signal_settings if mode == "custom" else None,
                                                input_session=session)
                engine.start(recording_path=recording_path, recording_options=recording_options,
                                  experiment=self.experiment.text())
            self.engine = engine
            self._preview_invalidated = False
            if isinstance(previous, RemoteEngine):
                previous.detach()
            self._error_shown = ""
            self._device_error = ""
            self.error_banner.hide()
            if recording_path:
                self.latest_recording = recording_path
            self.actual_rate.setText(f"实际时钟：{self.engine.sample_rate:,.3f} S/s")
            self.actual_rate.setToolTip("HPF 文件保存的原始采样率，不重新取整。" if mode == "hpf" else "PLL 寄存器计算的名义采样率；未进行外部时钟校准。")
            self.pause_plot.setChecked(False)
            self._update_live_window_limit()
            self._fit_live_plot()
            origin = "HPF 输入回放" if mode == "hpf" else "DT9837C 真实输入" if mode == "usb" else "模拟信号"
            self.tell(f"正在记录{origin} · {recording_path}" if recording_path else
                      f"正在预览{origin} · 尚未保存。点击“开始记录”保存原始电压与本次配置。")
        except Exception as exc:
            self.tell(f"无法开始预览：{exc}", error_title="无法开始采集 · 请检查设备连接和采集设置")
        self._refresh_buttons()

    def stop_acquisition(self):
        try:
            if self.engine is not None:
                self.engine.stop()
            connected = isinstance(self.engine, RemoteEngine) and self.engine.device_connected
            self.tell("采集与 AO 已停止，AO 已下发 0 V，设备保持连接。" if connected else
                      "采集已停止。已记录的数据可在“数据回看”中打开。")
        except Exception as exc:
            self.tell(f"停止采集：{exc}")
        self._refresh_buttons()

    def start_output(self):
        try:
            if not isinstance(self.engine, RemoteEngine) or self.engine.source != "usb" or not self.engine.running:
                raise ValueError("请先开始真实 USB 预览")
            self.engine.start_output(self.output_panel.settings())
            self.tell("AO 已开始输出；可在 AI0 查看回环信号。关闭窗口后独立核心继续输出。")
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell(f"无法开始 AO：{exc}")
        self._refresh_buttons()

    def stop_output(self):
        try:
            if not isinstance(self.engine, RemoteEngine):
                return
            self.engine.stop_output()
            self.tell("AO 已停止并下发 0 V；输入预览和记录继续。")
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell(f"AO 停止归零失败：{exc}")
        self._refresh_buttons()

    def toggle_record(self):
        try:
            if self.engine is not None and self.engine.recording:
                self.engine.stop_recording()
                self.tell(f"记录已保存 · {self.latest_recording}")
            else:
                path, group_number = next_recording_path(self.recording_root, self.experiment.text())
                options = self._recording_options(group_number)
                if self.engine is None or not self.engine.running:
                    self._begin_input(recording_path=path, recording_options=options)
                    return
                if isinstance(self.engine, RemoteEngine):
                    self.engine.start_recording(path, self.channels, self.experiment.text(), recording_options=options)
                else:
                    self.engine.start_recording(path, self._selected_settings(), recording_options=options,
                                                experiment=self.experiment.text())
                self.latest_recording = path
                self.tell(f"正在记录当前输入 · 原始电压、来源和换算参数保存在 {path}")
        except Exception as exc:
            self.tell(f"记录操作失败：{exc}")
        self._refresh_buttons()

    def _refresh_buttons(self):
        running = self.engine is not None and self.engine.running
        recording = self.engine is not None and self.engine.recording
        connected = isinstance(self.engine, RemoteEngine) and self.engine.device_connected
        remote = isinstance(self.engine, RemoteEngine)
        self.output_panel.update_state(connected and running, self.engine.output if remote else {},
                                       bool(remote and self.engine.connection_error))
        self.device_button.setVisible(self.scenario.currentData() == "usb")
        self.device_button.setText("断开设备" if connected else "连接设备")
        self.device_button.setEnabled(not running)
        self.reconnect_button.setVisible(self.scenario.currentData() == "usb" and not connected)
        self.reconnect_button.setEnabled(not running and not self._connecting)
        from_hpf = self.scenario.currentData() == "hpf"
        hardware_ready = self.scenario.currentData() != "usb" or connected
        self.preview_button.setEnabled(not running and hardware_ready)
        self.record_button.setEnabled(recording or hardware_ready)
        self.stop_button.setEnabled(running)
        self.record_button.setChecked(recording)
        self.record_button.setText("结束记录" if recording else "开始记录")
        self.record_button.setIcon(theme.icon("stop" if recording else "record",
                                              self.p.accent_text if recording else self.p.danger, 16))
        paused = recording and self.engine.recording_paused
        self.pause_record_button.setEnabled(recording and running)
        self.pause_record_button.setText("继续记录" if paused else "暂停记录")
        self.pause_record_button.setIcon(theme.icon("play" if paused else "pause",
                                                    self.p.text_muted, 16))
        for widget in [self.scenario, self.load_config_button, self.input_button, self.hpf_loop]:
            widget.setEnabled(not running)
        self.rate.setEnabled(not running and not from_hpf)
        self.channel_button.setEnabled(not running and not from_hpf)
        self.zero_button.setEnabled(running and not recording and not from_hpf)
        self.undo_zero_button.setEnabled(self.engine is not None and not recording and not from_hpf
                                         and self._previous_zero is not None)
        self.fault_button.setEnabled(running and self.engine.transport is not None)
        self.directory_button.setEnabled(not recording)
        self.experiment.setEnabled(not recording)
        self.duration_mode.setEnabled(not recording)
        self.duration_input.setEnabled(not recording)
        ended = self.engine is not None and not self.engine.error and self.engine.termination_reason == "end_of_file"
        if isinstance(self.engine, RemoteEngine) and self.engine.connection_error:
            text, tone = "核心连接中断 · 状态未知", "danger"
            for widget in [self.preview_button, self.record_button, self.zero_button, self.scenario,
                           self.rate, self.channel_button, self.load_config_button, self.input_button,
                           self.device_button, self.duration_mode, self.duration_input, self.undo_zero_button,
                           self.pause_record_button]:
                widget.setEnabled(False)
        elif self._connecting:
            text, tone = "正在连接设备", "info"
        elif self._device_error or (self.engine is not None and self.engine.error):
            text, tone = "设备连接异常" if self.scenario.currentData() == "usb" else "采集异常", "danger"
        elif paused:
            text, tone = "记录已暂停 · 预览继续", "warning"
        elif recording:
            text, tone = "正在记录", "danger"
        elif running and self.engine.termination_reason == "duration_complete":
            text, tone = "定长已完成 · 预览中", "info"
        elif running:
            text, tone = "预览中 · 未记录", "info"
        elif connected:
            text, tone = "已连接 · 未采集", "success"
        elif ended:
            text, tone = "HPF 已播完", "warning"
        elif self.engine and self.engine.source == "usb":
            text, tone = "已断开", "neutral"
        elif self.engine:
            text, tone = "已停止", "neutral"
        else:
            text, tone = "未开始", "neutral"
        self.status_label.setText(text)
        theme.set_tone(self.status_label, tone)
        dot_color = theme.tone_color(self.p, tone)[0] if tone != "neutral" else self.p.text_faint
        self.status_dot_label.setPixmap(theme.dot_pixmap(dot_color, 8))
        if self._connecting:
            for widget in (self.preview_button, self.record_button, self.device_button, self.reconnect_button,
                           self.rate, self.channel_button, self.load_config_button, self.scenario):
                widget.setEnabled(False)
        self._update_side_status()
        self._update_mode_badge()

    def _update_side_status(self):
        engine = self.engine
        p = self.p
        if self._connecting:
            color, status, core = p.accent, "正在连接设备…", "连接后保持待机"
        elif self._device_error:
            color, status, core = p.danger, "设备连接失败", "检查 USB 后重新连接"
        elif engine is None:
            color, status, core = p.text_faint, "未连接", "核心 · 未启动"
        elif isinstance(engine, RemoteEngine) and engine.connection_error:
            color, status, core = p.danger, "核心连接中断", "状态未知"
        elif isinstance(engine, RemoteEngine):
            if engine.error:
                color, status = p.danger, "USB 已断开" if not engine.device_connected else "设备通信异常"
            elif engine.recording:
                color, status = p.danger, "正在记录"
            elif engine.running:
                color = p.success
                status = "采集运行中" if engine.source == "usb" else "模拟运行中"
            elif engine.device_connected:
                color, status = p.success, "USB 已连接 · 待机"
            else:
                color, status = p.warning, "已停止"
            core = "独立核心 · 已连接" if engine.running or engine.device_connected else "核心在线 · 等待重连"
        else:
            color, status = p.accent, "界面内采集"
            core = "Python 输入 · 预览"
        self.side_device_dot.setPixmap(theme.dot_pixmap(color, 8))
        self.side_device_status.setText(status)
        stylesheet = f"color: {color}; font-size: 11px;"
        if self.side_device_status.styleSheet() != stylesheet:
            self.side_device_status.setStyleSheet(stylesheet)
        self.side_core_status.setText(core)

    def toggle_record_pause(self):
        if self.engine is None or not self.engine.recording:
            return
        try:
            if self.engine.recording_paused:
                self.engine.resume_recording()
                self.tell("已继续本组记录；暂停区间保留时间缺口。")
            else:
                self.engine.pause_recording()
                self.tell("记录已暂停；输入和预览继续，定长剩余帧数保持不变。")
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell(f"暂停/继续记录失败：{exc}")
        self._refresh_buttons()

    def refresh(self):
        self._tick += 1
        if self._tick % 300 == 0:
            self._refresh_disk_space()
        if self.engine is None:
            return
        buttons_key = (
            self.engine, self.engine.running, self.engine.recording,
            self.engine.recording_paused, self.engine.error, self.engine.termination_reason,
            self.scenario.currentData(), self._previous_zero is not None,
            self._connecting, self._device_error,
            (self.engine.device_connected, self.engine.connection_error)
            if isinstance(self.engine, RemoteEngine) else None,
            json.dumps(self.engine.output, sort_keys=True) if isinstance(self.engine, RemoteEngine) else None,
        )
        if buttons_key != self._buttons_refresh_key:
            self._refresh_buttons()
            self._buttons_refresh_key = buttons_key
        if isinstance(self.engine, RemoteEngine) and self.engine.connection_error:
            if self.engine.connection_error != self._error_shown:
                self._error_shown = self.engine.connection_error
                self.tell(self.engine.connection_error)
            return
        if self.engine.error and self.engine.error != self._error_shown:
            self._error_shown = self.engine.error
            if isinstance(self.engine, RemoteEngine) and self.engine.source == "usb" and not self.engine.device_connected:
                self.tell(f"数采连接已断开。插好 USB 后点击左下角“重新连接”恢复待机。详情：{self.engine.error}",
                          "danger", error_title="数采连接已断开 · 插好 USB 后点击“重新连接”")
            else:
                self.tell(f"采集已中止：{self.engine.error}。已有记录已尝试关闭；可重新开始预览。")
        if (not self.engine.recording and self.engine.termination_reason == "duration_complete"
                and self.engine.last_recording != self._completion_shown):
            self._completion_shown = self.engine.last_recording
            self.tell(f"本组定长记录已完成并保存 · {self.engine.last_recording}")
        page = self.pages.currentIndex()
        if self._preview_invalidated and page == 0:
            return
        if page != 0:
            if self._tick % 30 == 0:
                if page == 2:
                    self.refresh_analysis()
                elif page == 4:
                    self.refresh_protocol()
            return
        # A stopped input retains its last buffer. Only redraw when samples or
        # display/conversion settings change; hidden live widgets need no work.
        display_key = (
            self.engine, self.engine.total_scans, self.engine.recording,
            self.engine.recorded_scans, self.engine.recording_remaining_scans,
            self.live_value_mode.currentData(), self.value_decimals.value(),
            self.live_time_axis.currentData(), self.live_seconds.value(), self.pause_plot.isChecked(),
            self.engine.display_revision if isinstance(self.engine, RemoteEngine) else None,
            tuple(check.isChecked() for check in self.live_channel_checks),
            tuple((c.index, c.name, c.unit, c.sensitivity_mv_per_unit,
                   c.zero_volts, c.inverted) for c in self._selected_settings()),
        )
        if display_key == self._live_display_key:
            return
        times, raw = self.engine.snapshot(seconds=self.live_seconds.value())
        if len(raw) == 0:
            return
        self._live_display_key = display_key
        converted = convert_volts(raw, self._selected_settings())
        for j, i in enumerate(self.active_channels):
            c = self.channels[i]
            recent = converted[-min(len(raw), max(1, round(self.engine.sample_rate))):, j]
            mode = self.live_value_mode.currentData()
            value = {"last": lambda: recent[-1], "first": lambda: recent[0],
                     "mean": lambda: np.mean(recent), "min": lambda: np.min(recent),
                     "max": lambda: np.max(recent), "rms": lambda: np.sqrt(np.mean(recent ** 2)),
                     "std": lambda: np.std(recent), "delta": lambda: np.ptp(recent)}[mode]()
            self.metric_cards[i].set_value_text(f"{value:.{self.value_decimals.value()}f}")
            clipped = bool(np.any(np.abs(raw[-min(len(raw), 1000):, j]) >= self.ranges[i] * 0.9999))
            self.metric_subs[i].setText("已达量程边界" if clipped else
                                       f"{self.live_value_mode.currentText()} · 近 {len(recent) / self.engine.sample_rate:.3f} s")
            self.metric_cards[i].sparkline.set_values(recent)
        if not self.pause_plot.isChecked():
            self._live_cache = times, raw
            self._draw_live(times, raw)
        total = self.engine.total_scans
        if self.engine.recording:
            count = self.engine.recorded_scans
            remaining = self.engine.recording_remaining_scans
            suffix = f" · 剩余 {remaining / self.engine.sample_rate:.2f} s" if remaining is not None else ""
            self.count_label.setText(f"本组 {count:,} 扫描 · {count / self.engine.sample_rate:.2f} s{suffix}")
        else:
            self.count_label.setText(f"{total:,} 扫描 · {total / self.engine.sample_rate:.2f} s")
        origin = "DT9837C 真实输入" if self.engine.source == "usb" else "HPF 循环回放（接缝为拼接）" if self.engine.source == "file_replay" and self.hpf_loop.isChecked() else "HPF 回放" if self.engine.source == "file_replay" else "电压模拟"
        timing = ""
        if self.live_time_axis.currentData() == "absolute":
            epoch = self._timestamp(self.engine.acquisition_started_at_utc)
            if epoch is not None:
                timing = " · 当前 " + datetime.fromtimestamp(epoch + times[-1]).astimezone().isoformat(timespec="milliseconds")
        self.live_info.setText(f"{origin} · {len(self.active_channels)} 路 · {self.engine.sample_rate:,.3f} S/s/通道 · 已记录 {self.engine.recorded_scans:,} 扫描{timing}")

    @staticmethod
    def _timestamp(value):
        if not value:
            return None
        try:
            date = datetime.fromisoformat(value)
            return (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
        except (TypeError, ValueError):
            return None

    def _replay_epoch(self):
        if self.replay is None:
            return None
        metadata = self.replay.metadata
        epoch = self._timestamp(metadata.get("acquisition_started_at_utc"))
        if epoch is not None:
            start = metadata.get("acquisition_config", {}).get("record_start_scan", 0)
            return epoch + start / self.replay.sample_rate
        return self._timestamp(metadata.get("started_at_utc"))

    @staticmethod
    def _time_axis_values(plot, times, mode, epoch, sample_rate):
        absolute = mode == "absolute" and epoch is not None
        if absolute != isinstance(plot.getAxis("bottom"), pg.DateAxisItem):
            plot.setAxisItems({"bottom": pg.DateAxisItem() if absolute else pg.AxisItem("bottom")})
            theme.style_plot(plot, theme.current())
        if absolute:
            plot.setLabel("bottom", "电脑时钟（本地时间）")
            return epoch + times
        if mode == "samples":
            plot.setLabel("bottom", "样本号")
            return np.rint(times * sample_rate)
        plot.setLabel("bottom", "相对时间", units="s")
        return times

    def _time_axis_changed(self, source):
        combo = self.live_time_axis if source == "live" else self.replay_time_axis
        measurements = self.replay_measurements if source == "replay" else None
        cursor_times = measurements.measurements.get("times") if measurements is not None else None
        epoch = (self._timestamp(self.engine.acquisition_started_at_utc) if self.engine else None) if source == "live" else self._replay_epoch()
        available = self.engine is not None if source == "live" else self.replay is not None
        if available and combo.currentData() == "absolute" and epoch is None:
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self.tell("此输入没有可用的绝对起点，使用相对时间；不会以文件修改时间代替。")
        if source == "live":
            self._live_follow = True
            if self._live_cache is not None:
                self._draw_live(*self._live_cache)
        elif self.replay is not None:
            self.draw_replay()
        if cursor_times and available:
            rate = self.engine.sample_rate if source == "live" else self.replay.sample_rate
            for line, timestamp in zip(measurements.cursor_lines, cursor_times):
                line.setValue(epoch + timestamp if combo.currentData() == "absolute" and epoch is not None else
                              round(timestamp * rate) if combo.currentData() == "samples" else timestamp)

    def _draw_live(self, times, raw):
        channels = self._selected_settings()
        units = {c.unit for c in channels if self.live_channel_checks[c.index].isChecked()}
        unit = next(iter(units)) if len(units) == 1 else "V"
        self.live_plot.setLabel("left", "工程值" if len(units) == 1 else "原始电压", units=unit)
        values = convert_volts(raw, channels) if len(units) == 1 else raw
        epoch = self._timestamp(self.engine.acquisition_started_at_utc)
        x = self._time_axis_values(self.live_plot, times, self.live_time_axis.currentData(), epoch, self.engine.sample_rate)
        for i, curve in enumerate(self.live_curves):
            visible = i in self.active_channels and self.live_channel_checks[i].isChecked()
            curve.setVisible(visible)
            if visible:
                curve.setData(x, values[:, self.active_channels.index(i)])
            else:
                curve.setData([], [])
        if self._live_follow and len(times):
            span = self.live_seconds.value()
            end = max(span, times[-1] + 1 / self.engine.sample_rate)
            bounds = np.array([end - span, end])
            mode = self.live_time_axis.currentData()
            if mode == "absolute" and epoch is not None:
                bounds += epoch
            elif mode == "samples":
                bounds *= self.engine.sample_rate
            self.live_plot.setXRange(*bounds, padding=0)

    def _live_visibility_changed(self, *_):
        self._live_display_key = None
        if self._live_cache is not None:
            self._draw_live(*self._live_cache)
        self.live_plot.enableAutoRange(axis="y")

    def _live_range_changed(self, axes):
        if axes[0]:
            self._live_follow = False

    def _fit_live_plot(self):
        self._live_follow = True
        self.live_plot.enableAutoRange(axis="y")
        if self._live_cache is not None:
            self._draw_live(*self._live_cache)
        else:
            self.live_plot.setXRange(0, self.live_seconds.value(), padding=0)

    def _live_window_changed(self, *_):
        if isinstance(self.engine, RemoteEngine):
            self.engine.display_seconds = self.live_seconds.value()
        self._live_display_key = None
        self._fit_live_plot()

    def _update_live_window_limit(self, *_):
        rate = self.engine.sample_rate if self.engine is not None else plan_clock(self.rate.currentData()).actual_rate
        limit = math.floor(min(60, 200000 / rate) * 10) / 10
        self.live_seconds.setMaximum(limit)
        self.live_seconds.setToolTip(f"横轴显示时长，满窗后滚动。当前采样率最多显示 {limit:g} 秒（每通道最多 200,000 点）。不影响记录时长。")
        if isinstance(self.engine, RemoteEngine):
            self.engine.display_seconds = self.live_seconds.value()

    def _set_sample_rate(self, rate):
        index = self.rate.findData(rate)
        if index < 0:
            self.rate.addItem(f"{rate:g} S/s · 已载入", rate)
            index = self.rate.count() - 1
        self.rate.setCurrentIndex(index)
        self._update_live_window_limit()

    def zero_channels(self):
        if self.engine is None or not self.engine.running or self.engine.recording:
            return
        _, raw = self.engine.snapshot(seconds=1)
        if not len(raw):
            return
        previous = [c.zero_volts for c in self.channels]
        for j, i in enumerate(self.active_channels):
            self.channels[i] = replace(self.channels[i], zero_volts=float(np.mean(raw[:, j])))
        if isinstance(self.engine, RemoteEngine):
            try:
                self.engine.set_channels(self.channels)
            except (OSError, ValueError, RuntimeError) as exc:
                self.channels = [ChannelSettings(**c) for c in self.engine.profile["channels"]]
                self.tell(f"调零未确认：{exc}")
                return
        self._previous_zero = previous
        self._refresh_buttons()
        self.refresh()
        if self.pause_plot.isChecked() and self._live_cache is not None:
            self._draw_live(*self._live_cache)
        self.live_plot.enableAutoRange(axis="y")
        self.tell("已将最近最多 1 秒的平均电压设为零点，可点击“撤销调零”恢复。请确认此时为空载；原始电压未修改。")

    def undo_zero(self):
        if self.engine is None or self.engine.recording or self._previous_zero is None:
            return
        channels = [replace(c, zero_volts=zero) for c, zero in zip(self.channels, self._previous_zero)]
        if isinstance(self.engine, RemoteEngine):
            try:
                self.engine.set_channels(channels)
            except (OSError, ValueError, RuntimeError) as exc:
                self.tell(f"撤销调零未确认：{exc}")
                return
        self.channels = channels
        self._previous_zero = None
        self._refresh_buttons()
        self.refresh()
        if self.pause_plot.isChecked() and self._live_cache is not None:
            self._draw_live(*self._live_cache)
        self.live_plot.enableAutoRange(axis="y")
        self.tell("已恢复上次调零前的零点；原始电压未修改。")

    def edit_channels(self):
        if ChannelDialog(self).exec() == W.QDialog.DialogCode.Accepted:
            self._discard_preview(keep_device_connected=True)
            self._sync_channel_labels()
            self.count_label.setText("等待采样")
            self.live_info.setText("通道设置已更新，开始预览后生效。")
            connected = isinstance(self.engine, RemoteEngine) and self.engine.device_connected
            self.tell("通道设置已更新，设备保持连接。直接开始预览后生效。" if connected else
                      "通道设置已更新。开始预览后生效。")

    def _discard_preview(self, *, keep_device_connected=False):
        self._previous_zero = None
        self._live_cache = None
        self._live_display_key = None
        # A retained USB session still holds samples from the previous channel map.
        # Keep polling connection health, but display no old samples until restart.
        self._live_follow = True
        keep_connection = (keep_device_connected and isinstance(self.engine, RemoteEngine)
                           and self.engine.source == "usb")
        self._preview_invalidated = keep_connection
        if self.engine and not keep_connection:
            self.engine.stop()
            if isinstance(self.engine, RemoteEngine):
                self.engine.shutdown()
            self.engine = None
        for curve in self.live_curves:
            curve.setData([], [])
        for metric in self.metric_values:
            metric.setText("—")
        for sub in self.metric_subs:
            sub.setText("等待采样")
        for card in getattr(self, "metric_cards", []):
            card.sparkline.set_values([])
        self.live_plot.setXRange(0, self.live_seconds.value(), padding=0)
        self._clear_analysis()
        self._refresh_buttons()

    def choose_directory(self):
        result = W.QFileDialog.getExistingDirectory(self, "选择记录目录", str(self.recording_root.parent))
        if result:
            self.recording_root = Path(result)
            self.directory_label.setText(result)
            self._refresh_disk_space()

    def _refresh_disk_space(self):
        path = self.recording_root if self.recording_root.exists() else self.recording_root.parent
        try:
            free = shutil.disk_usage(path).free
            self.disk_space_label.setText(f"可用空间：{free / 1024 ** 3:,.2f} GiB")
        except OSError as exc:
            self.disk_space_label.setText(f"无法读取可用空间：{exc}")

    def _recording_options(self, group_number=0):
        return {"duration_seconds": self.recording_duration_seconds,
                "group_number": group_number}

    def _set_recording_options(self, options):
        duration = options.get("duration_seconds")
        with QtCore.QSignalBlocker(self.duration_mode), QtCore.QSignalBlocker(self.duration_input):
            index = self.duration_mode.findData(duration)
            if index < 0:
                index = self.duration_mode.findData("custom")
                self.duration_input.setValue(duration)
            self.duration_mode.setCurrentIndex(index)
        self._duration_changed()

    def _duration_changed(self, *_):
        duration = self.duration_mode.currentData()
        custom = duration == "custom"
        self.duration_input.setVisible(custom)
        self.recording_duration_seconds = self.duration_input.value() if custom else duration

    def save_profile(self):
        path, _ = W.QFileDialog.getSaveFileName(self, "保存实验配置", str(ROOT / "实验配置.json"), "JSON 配置 (*.json)")
        if path:
            try:
                self.save_profile_to(Path(path))
            except (ValueError, OSError) as exc:
                self.tell(f"配置无法保存：{exc}")

    def save_profile_to(self, path: Path):
        data = {"schema": "labdaq-profile-v1", "channels": [asdict(c) for c in self.channels],
                "active_channels": self.active_channels, "ranges": self.ranges, "coupling": self.coupling,
                "iepe": self.iepe, "sample_rate": self.rate.currentData(), "scenario": self.scenario.currentData(),
                "experiment": self.experiment.text(), "signal_settings": self.signal_settings.to_dict(),
                "output_settings": self.output_panel.settings(),
                "recording_options": self._recording_options(),
                "display": {"live_time_axis": self.live_time_axis.currentData(),
                            "replay_time_axis": self.replay_time_axis.currentData(),
                            "value_mode": self.live_value_mode.currentData(),
                            "value_decimals": self.value_decimals.value(), "live_seconds": self.live_seconds.value()},
                "input_hpf": str(self.input_hpf.path.resolve()) if self.input_hpf else None,
                "hpf_loop": self.hpf_loop.isChecked()}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.tell(f"配置已保存 · {path}")

    def load_profile(self):
        path, _ = W.QFileDialog.getOpenFileName(self, "载入实验配置", str(ROOT), "JSON 配置 (*.json)")
        if path:
            try:
                self.load_profile_from(Path(path))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.tell(f"配置无法载入：{exc}")

    def load_profile_from(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != "labdaq-profile-v1":
            raise ValueError("配置文件版本不匹配")
        channels = [ChannelSettings(**c) for c in data["channels"]]
        if len(channels) != 4 or [c.index for c in channels] != list(range(4)):
            raise ValueError("配置必须包含 AI 0–3")
        config = AcquisitionConfig(sample_rate=data["sample_rate"], channels=tuple(data["active_channels"]),
                                   ranges=tuple(data["ranges"]), coupling=tuple(data["coupling"]), iepe=tuple(data["iepe"]))
        mode = data["scenario"]
        if self.scenario.findData(mode) < 0:
            raise ValueError("未知输入来源")
        settings = SimulationSettings.from_dict(data["signal_settings"]) if "signal_settings" in data else SimulationSettings()
        output_settings = data.get("output_settings", DEFAULT_OUTPUT)
        validate_output(output_settings)
        options = data.get("recording_options", {})
        duration = options.get("duration_seconds")
        if duration is not None and (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                                     or not math.isfinite(duration) or not 0 < duration <= 999999999):
            raise ValueError("记录时长必须为正的有限秒数，手动结束使用 null")
        display = data.get("display", {})
        for key, combo, default in (("live_time_axis", self.live_time_axis, "relative"),
                                    ("replay_time_axis", self.replay_time_axis, "relative"),
                                    ("value_mode", self.live_value_mode, "last")):
            if combo.findData(display.get(key, default)) < 0:
                raise ValueError("未知的显示模式")
        decimals = display.get("value_decimals", 3)
        if type(decimals) is not int or not 0 <= decimals <= 9:
            raise ValueError("显示小数位必须是 0 到 9 的整数")
        live_seconds = display.get("live_seconds", 10)
        if (isinstance(live_seconds, bool) or not isinstance(live_seconds, (int, float))
                or not math.isfinite(live_seconds) or not .1 <= live_seconds <= 60):
            raise ValueError("横轴显示时长必须在 0.1 到 60 秒之间")
        reader = None
        if mode == "hpf":
            from .hpf import open_hpf
            hpf_path = Path(data["input_hpf"])
            reader = open_hpf(hpf_path if hpf_path.is_absolute() else path.parent / hpf_path, waveform_only=True)
            if not reader.voltage_compatible:
                raise ValueError(reader.voltage_replay_error)
            config = self._hpf_config(reader)
            for channel in reader.channels:
                channels[channel.index] = replace(channel)
        # Configure a simulator to validate external configuration before replacing UI state.
        validation = AcquisitionEngine(config, [channels[i] for i in config.channels], "custom" if mode in ("rust", "usb") else mode,
                                       signal_settings=settings if mode in ("custom", "rust", "usb") else None,
                                       input_session=HpfReplaySession(reader, loop=data.get("hpf_loop", False)) if reader else None)
        validation.stop()
        self._discard_preview()
        self.channels, self.active_channels = channels, config.channels
        self.signal_settings = settings
        self.output_panel.set_settings(output_settings)
        if reader is not None:
            self.input_hpf, self.input_hpf_config = reader, config
        self.ranges, self.coupling, self.iepe = list(config.ranges), list(config.coupling), list(config.iepe)
        self._set_sample_rate(config.sample_rate)
        self.hpf_loop.setChecked(data.get("hpf_loop", False))
        self.scenario.setCurrentIndex(self.scenario.findData(mode))
        self._input_changed()
        self.experiment.setText(data["experiment"])
        self._set_recording_options(options)
        self.live_time_axis.setCurrentIndex(self.live_time_axis.findData(display.get("live_time_axis", "relative")))
        self.replay_time_axis.setCurrentIndex(self.replay_time_axis.findData(display.get("replay_time_axis", "relative")))
        self.live_value_mode.setCurrentIndex(self.live_value_mode.findData(display.get("value_mode", "last")))
        self.value_decimals.setValue(decimals)
        self.live_seconds.setValue(live_seconds)
        self._sync_channel_labels()
        self.tell(f"配置已载入 · {path.name}")

    def refresh_analysis(self, *_):
        if not hasattr(self, "analysis_info"):
            return
        mode = self.analysis_mode.currentIndex()
        time_domain = mode == 3
        if mode != getattr(self, "_analysis_plot_mode", None):
            self.analysis_plot.enableAutoRange()
            self.analysis_plot.setLabel("bottom", "相对时间" if time_domain else "频率",
                                        units="s" if time_domain else "Hz")
            self._analysis_plot_mode = mode
        self.analysis_measurements.setVisible(time_domain)
        for line in self.analysis_measurements.cursor_lines:
            line.setVisible(time_domain and self.analysis_measurements.enabled_checkbox.isChecked())
        self.reference_channel.setEnabled(mode == 2)
        self.window_type.setEnabled(not time_domain)
        if (time_domain and self.analysis_freeze.isChecked() and not _
                and self.analysis_curve.xData is not None and len(self.analysis_curve.xData)):
            return
        from_replay = self.analysis_source.currentIndex() == 1
        if from_replay:
            if self.replay is None:
                self._clear_analysis()
                self.analysis_info.setText("请先在“数据回看”中打开记录或 HPF 文件。")
                return
            raw, start, reader = self._replay_window()
            fs, settings = self.replay.sample_rate, self.replay.channels
        else:
            if self.engine is None or self._preview_invalidated:
                self._clear_analysis()
                self.analysis_info.setText("等待开始预览。")
                return
            times, raw = self.engine.snapshot(seconds=self.live_seconds.value() if time_domain else 8)
            fs, settings = self.engine.sample_rate, self._selected_settings()
        if len(raw) < (1 if time_domain else 32):
            self._clear_analysis()
            self.analysis_info.setText("等待至少 32 个扫描点。")
            return
        # Fixed upper bound keeps plotting and analysis responsive at high rates.
        original_length = len(raw)
        raw = raw[-(200000 if time_domain else 65536):]
        if from_replay:
            analysis_start = start + max(0, original_length - len(raw))
            times = self._replay_times(reader, analysis_start, len(raw))
        else:
            times = times[-len(raw):]
        has_gaps = bool(np.any(np.diff(times) > 1.5 / fs))
        if has_gaps and (not time_domain or self.lowpass.isChecked()):
            self._clear_analysis()
            self.analysis_info.setText("当前窗口包含暂停缺口，请选择一个连续段再分析或关闭低通查看时域波形。")
            return
        values = convert_volts(raw, settings)
        indices = [c.index for c in settings]
        selected = self.analysis_channel.currentData()
        reference = self.reference_channel.currentData()
        if selected not in indices:
            return
        idx = indices.index(selected)
        try:
            if self.lowpass.isChecked():
                values = filter_window(values, fs, self.cutoff.value())
            if time_domain:
                f = times
                y = values[:, idx]
                self.analysis_plot.setLabel("bottom", "相对时间", units="s")
                self.analysis_plot.setLabel("left", "工程值", units=settings[idx].unit)
                self.coherence_plot.hide()
                gaps = np.flatnonzero(np.diff(times) > 1.5 / fs) + 1
                x = np.insert(times, gaps, times[gaps])
                plotted = np.insert(y, gaps, np.nan)
                self.analysis_measurements.set_data(
                    x, plotted[:, None], [(f"AI {selected} · {settings[idx].name}", settings[idx].unit)])
            elif mode == 2:
                if selected == reference:
                    self._clear_analysis()
                    self.analysis_info.setText("请选择不同的参考与响应通道。")
                    return
                refidx = indices.index(reference)
                f, h1, coh = frequency_response(values[:, refidx], values[:, idx], fs,
                                                 nperseg=min(2048, len(values) // 2), window=self.window_type.currentText())
                y = np.abs(h1)
                self.analysis_plot.setLabel("left", "H1 幅值", units=f"{settings[idx].unit}/{settings[refidx].unit}")
                self.coherence_curve.setData(f, coh)
                self.coherence_plot.show()
            else:
                f, y = spectrum(values[:, idx], fs, mode="amplitude" if mode == 0 else "psd", window=self.window_type.currentText())
                self.analysis_plot.setLabel("left", "峰值幅度" if mode == 0 else "功率谱密度", units=settings[idx].unit if mode == 0 else f"{settings[idx].unit}²/Hz")
                self.coherence_plot.hide()
            self.analysis_curve.setPen(pg.mkPen(self.p.channels[selected], width=1))
            if time_domain:
                self.analysis_curve.setData(x, plotted, connect="finite", autoDownsample=True,
                                            downsampleMethod="peak")
            else:
                self.analysis_plot.setLabel("bottom", "频率", units="Hz")
                self.analysis_curve.setData(f, y, connect="all")
            signal = values[:, idx]
            origin = "回看窗口" if from_replay else "实时窗口"
            gaps_note = " · 含暂停缺口，曲线按真实时间断开" if has_gaps else ""
            self.analysis_info.setText(f"{origin} · {len(signal):,} 点 · 均值 {np.mean(signal):.4f} · RMS {np.sqrt(np.mean(signal**2)):.4f} · 峰峰值 {np.ptp(signal):.4f} {settings[idx].unit}{gaps_note}  |  原始文件保持不变")
        except (ValueError, TypeError) as exc:
            self._clear_analysis()
            self.analysis_info.setText(f"分析设置：{exc}")

    def _clear_analysis(self):
        self.analysis_curve.setData([], [])
        self.coherence_curve.setData([], [])
        self.coherence_plot.hide()
        self.analysis_measurements.set_data([], np.empty((0, 0)), [])
        for line in self.analysis_measurements.cursor_lines:
            line.hide()

    def choose_replay(self):
        result = W.QFileDialog.getExistingDirectory(self, "打开包含 metadata.json 的记录文件夹", str(self.recording_root))
        if result:
            self.open_replay(Path(result))

    def open_recording_library(self):
        dialog = RecordingLibraryDialog(self.recording_root, self)
        if dialog.exec() == W.QDialog.DialogCode.Accepted:
            self.open_replay(dialog.selected_path)

    def open_last_recording(self):
        if self.latest_recording:
            self.open_replay(self.latest_recording)
        else:
            self.tell("还没有记录。先开始预览并保存一段模拟数据。")

    def open_replay(self, path: Path):
        if self.engine and self.engine.recording and path == self.latest_recording:
            self.tell("请先结束这段记录，再打开回看。")
            return
        try:
            is_hpf = path.suffix.lower() == ".hpf"
            if is_hpf:
                from .hpf import open_hpf
                reader = open_hpf(path, waveform_only=True)
                if not reader.voltage_compatible:
                    raise ValueError(reader.voltage_replay_error)
            else:
                reader = load_session(path)
            if len(reader.channels) > 4:
                raise ValueError("此原型界面最多显示四路；文件未修改，请使用读取 API 处理全部通道。")
            self.replay = reader
            self.replay_is_hpf = is_hpf
            self.replay_path = path
            self._replay_legend_key = None
            for i, check in enumerate(self.replay_channel_checks):
                channel = next((c for c in reader.channels if c.index == i), None)
                check.blockSignals(True)
                check.setEnabled(channel is not None)
                check.setChecked(channel is not None)
                text = f"AI {i} · {channel.name}" if channel else f"AI {i}"
                check.setText(text if len(text) <= 18 else text[:17] + "…")
                check.setToolTip(channel.name if channel else "")
                check.blockSignals(False)
            duration = getattr(reader, "elapsed_duration", reader.duration)
            for spin, value in ((self.selection_start, 0), (self.selection_stop, duration)):
                spin.blockSignals(True)
                spin.setRange(0, duration)
                spin.setSingleStep(1 / reader.sample_rate)
                spin.setValue(value)
                spin.blockSignals(False)
            self.replay_select.setChecked(False)
            self._replay_selection_changed()
            self.replay_start.blockSignals(True)
            self.replay_start.setValue(0)
            self.replay_start.setSingleStep(1 / reader.sample_rate)
            self.replay_start.blockSignals(False)
            self._time_axis_changed("replay")
            self._analysis_source_changed()
            self.nav.setCurrentRow(3)
        except (OSError, ValueError, KeyError) as exc:
            self.tell(f"记录无法打开：{exc}")

    def _replay_window(self):
        r = self.replay
        span = min(self.replay_seconds.value(), 200000 / r.sample_rate)
        maximum = max(0, getattr(r, "elapsed_duration", r.duration) - span)
        time_start = min(self.replay_start.value(), maximum)
        start = self._replay_boundary(time_start)
        stop = self._replay_boundary(time_start + span)
        raw = r.read(start=start, count=min(200000, stop - start))
        return raw, start, r

    def _replay_boundary(self, seconds):
        reader = self.replay
        scan = max(0, math.floor(seconds * reader.sample_rate + .5))
        if hasattr(reader, "sample_index_at_time"):
            return reader.sample_index_at_time(scan / reader.sample_rate)
        return min(scan, reader.sample_count)

    @staticmethod
    def _replay_times(reader, start, count):
        return (reader.sample_times(start, count) if hasattr(reader, "sample_times") else
                (start + np.arange(count)) / reader.sample_rate)

    def _replay_x(self, times):
        return self._time_axis_values(self.replay_plot, np.asarray(times), self.replay_time_axis.currentData(),
                                      self._replay_epoch(), self.replay.sample_rate)

    def _replay_slider_changed(self, position):
        if self.replay is None:
            return
        self.replay_start.setValue(self.replay_start.maximum() * position / 1000)

    def step_replay(self, direction):
        if self.replay is not None:
            span = min(self.replay_seconds.value(), 200000 / self.replay.sample_rate)
            self.replay_start.setValue(self.replay_start.value() + direction * span)

    def select_replay_window(self):
        if self.replay is None:
            return
        raw, start, reader = self._replay_window()
        times = self._replay_times(reader, start, len(raw))
        self.selection_start.setValue(times[0] if len(times) else self.replay_start.value())
        self.selection_stop.setValue(times[-1] + 1 / reader.sample_rate if len(times) else
                                     self.replay_start.value() + self.replay_seconds.value())
        self.replay_select.setChecked(True)

    def _replay_selection_changed(self, *_):
        selected = self.replay_select.isChecked()
        for spin in (self.selection_start, self.selection_stop):
            spin.setEnabled(selected and self.replay is not None)
        self.replay_region.setVisible(selected and self.replay is not None)
        if self.replay is None:
            return
        self.replay_region.blockSignals(True)
        duration = getattr(self.replay, "elapsed_duration", self.replay.duration)
        self.replay_region.setBounds(tuple(self._replay_x([0, duration])))
        self.replay_region.setRegion(tuple(self._replay_x([self.selection_start.value(), self.selection_stop.value()])))
        self.replay_region.blockSignals(False)

    def _replay_region_changed(self):
        start, stop = self.replay_region.getRegion()
        mode = self.replay_time_axis.currentData()
        if mode == "absolute" and self._replay_epoch() is not None:
            start, stop = start - self._replay_epoch(), stop - self._replay_epoch()
        elif mode == "samples":
            start, stop = start / self.replay.sample_rate, stop / self.replay.sample_rate
        for spin, value in ((self.selection_start, start), (self.selection_stop, stop)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _replay_selection(self):
        if self.replay is None:
            raise ValueError("请先打开一段记录")
        channels = [ch.index for ch in self.replay.channels if self.replay_channel_checks[ch.index].isChecked()]
        if not channels:
            raise ValueError("请至少勾选一个通道")
        start, stop = 0, self.replay.sample_count
        if self.replay_select.isChecked():
            start, stop = [self._replay_boundary(spin.value()) for spin in (self.selection_start, self.selection_stop)]
        if stop <= start:
            raise ValueError("所选区间至少需要一个样本，起点必须小于终点")
        return start, stop, channels

    def _sync_replay_legend(self, visible):
        key = tuple(channel.index for channel in visible)
        if key == getattr(self, "_replay_legend_key", None):
            return
        self._replay_legend_key = key
        self._clear_layout(self.replay_legend_row)
        for channel in visible:
            self.replay_legend_row.addWidget(
                legend_chip(f"AI {channel.index} · {channel.name}", self.p.channels[channel.index]))
        self.replay_legend_row.addStretch()

    def draw_replay(self, *_):
        if self.replay is None:
            return
        raw, start, r = self._replay_window()
        span = min(self.replay_seconds.value(), 200000 / r.sample_rate)
        max_start = max(0, getattr(r, "elapsed_duration", r.duration) - span)
        self.replay_start.blockSignals(True)
        self.replay_start.setMaximum(max_start)
        self.replay_start.blockSignals(False)
        self.replay_slider.blockSignals(True)
        self.replay_slider.setValue(round(self.replay_start.value() / max_start * 1000) if max_start else 0)
        self.replay_slider.blockSignals(False)
        converted = convert_volts(raw, r.channels)
        visible = [c for c in r.channels if self.replay_channel_checks[c.index].isChecked()]
        units = {c.unit for c in visible}
        values = converted if len(units) == 1 else raw
        self.replay_plot.setLabel("left", "工程值" if len(units) == 1 else "原始电压", units=next(iter(units)) if len(units) == 1 else "V")
        times = self._replay_times(r, start, len(raw))
        x = self._replay_x(times)
        gaps = np.flatnonzero(np.diff(times) > 1.5 / r.sample_rate) + 1
        plot_x = np.insert(x, gaps, x[gaps])
        plot_values = np.insert(values, gaps, np.nan, axis=0)
        self.replay_plot.plotItem.legend.clear()
        for j, curve in enumerate(self.replay_curves):
            if j < len(r.channels) and self.replay_channel_checks[r.channels[j].index].isChecked():
                curve.setPen(pg.mkPen(self.p.channels[r.channels[j].index], width=1))
                curve.setData(plot_x, plot_values[:, j], connect="finite")
                self.replay_plot.plotItem.legend.addItem(curve, f"AI {r.channels[j].index} · {r.channels[j].name}")
            else:
                curve.setData([], [])
        self._sync_replay_legend(visible)
        self.replay_plot.enableAutoRange()
        columns = [j for j, c in enumerate(r.channels) if c in visible]
        unit = next(iter(units)) if len(units) == 1 else "V"
        self.replay_measurements.set_data(times, values[:, columns],
                                          [(f"AI {c.index} · {c.name}", unit) for c in visible], x_values=x)
        self._replay_selection_changed()
        self.replay_position.setText(f"{self.replay_start.value():.6f} s")
        source = "原厂 HPF 文件" if self.replay_is_hpf else "协议模拟" if r.metadata.get("source") == "simulation" else str(r.metadata.get("source", "未知"))
        ending = (r.metadata.get("waveform_only_note") or "只读 HPF 波形") if self.replay_is_hpf else "异常中止：" + str(r.metadata.get("error")) if r.metadata.get("error") else "正常结束" if r.metadata.get("finalized") else "未正常关闭的记录"
        if r.metadata.get("termination_reason") == "duration_complete":
            ending = "定长记录完成"
        elif r.metadata.get("termination_reason") == "end_of_file":
            ending = "输入文件已到末尾"
        elif r.metadata.get("termination_reason") == "user_stop" and not r.metadata.get("error"):
            ending = "手动结束"
        if ((r.metadata.get("acquisition_config") or {}).get("input") or {}).get("loop"):
            ending += " · 循环输入含首尾拼接，不代表连续物理过程"
        options = r.metadata.get("recording_options", {})
        notes = options.get("notes", "")
        group = options.get("group_number", 0)
        if group:
            ending += f" · 第 {group:03d} 组"
        elapsed = getattr(r, "elapsed_duration", r.duration)
        if elapsed - r.duration > .5 / r.sample_rate:
            ending += f" · 实录 {r.duration:.3f} s / 含暂停跨度 {elapsed:.3f} s"
        if self.replay_time_axis.currentData() == "absolute" and self._replay_epoch() is not None:
            ending += " · 电脑时钟起点 " + datetime.fromtimestamp(self._replay_epoch()).astimezone().isoformat(timespec="milliseconds")
        self.replay_info.setText(f"{self.replay_path.name}  ·  {source}  ·  {len(r.channels)} 路  ·  {r.sample_rate:,.3f} S/s  ·  {r.sample_count:,} 扫描  /  {r.sample_count/r.sample_rate:.2f} s\n{ending}"
                                 + (f"\n备注：{notes}" if notes else ""))

    def choose_hpf(self):
        path, _ = W.QFileDialog.getOpenFileName(self, "只读打开 QuickDAQ HPF", str(ROOT), "QuickDAQ 数据 (*.hpf)")
        if path:
            self.open_replay(Path(path))

    def open_vendor_sample(self):
        self.open_replay(ROOT / "resources/vendor/extracted/quickdaq/LocalAppData/Data Translation/QuickDAQ/Data/QuickDAQ FFT Analyzer Sample Data.hpf")
        if self.replay_is_hpf:
            self.replay_seconds.setValue(.02)

    def analyze_replay(self):
        if self.replay is None:
            self.tell("请先打开一段记录或原厂样例。")
            return
        self.analysis_source.setCurrentIndex(1)
        self._analysis_source_changed()
        self.nav.setCurrentRow(2)

    def export_replay(self):
        try:
            start, stop, channels = self._replay_selection()
            dialog = ExportDialog(self.replay, start, stop, channels, self)
            if dialog.exec() == W.QDialog.DialogCode.Accepted:
                self.tell(f"数据已导出 · {dialog.output_path}")
        except (OSError, ValueError) as exc:
            self.tell(f"导出失败：{exc}")

    def show_replay_statistics(self):
        try:
            start, stop, channels = self._replay_selection()
            StatisticsDialog(self.replay, start, stop, channels, self).exec()
        except (OSError, ValueError) as exc:
            self.tell(f"统计失败：{exc}")

    def save_plot_image(self, plot):
        path, _ = W.QFileDialog.getSaveFileName(self, "保存当前波形图", str(self.recording_root / "waveform.png"), "PNG 图像 (*.png)")
        if path:
            try:
                self.save_plot_image_to(plot, Path(path))
                self.tell(f"波形图已保存 · {path}")
            except (OSError, ValueError) as exc:
                self.tell(f"图像保存失败：{exc}")

    @staticmethod
    def save_plot_image_to(plot, path):
        from pyqtgraph.exporters import ImageExporter
        if Path(path).suffix.lower() != ".png":
            raise ValueError("波形图请保存为 .png 文件")
        exporter = ImageExporter(plot.plotItem)
        exporter.export(str(path))

    def scan_devices(self):
        try:
            devices = enumerate_usb_devices()
            self.device_result.setText("\n".join(f"VID {d['vendor_id']:04X} / PID {d['product_id']:04X} · {d['state']} · {d['model']}" for d in devices)
                                      or "未发现 DT9837 系列 USB 设备。当前仍为协议模拟模式。")
        except Exception as exc:
            self.device_result.setText(f"USB 枚举未完成：{exc}")

    def simulate_boot(self):
        from .firmware import run_boot_simulation
        fw = ROOT / "resources/opensource/libuldaq-1.2.2/src/usb/fw"
        try:
            result = run_boot_simulation(fw / "DTFX2LDR_FW.c", fw / "DT9837A_FW.c")
            self.boot_result.setText(f"冷启动离线验证完成 · {result['loader_records']} 条 loader / {result['device_records']} 条设备固件记录\n"
                                    f"{result['control_transfers']:,} 次控制传输 · PID {result['from_pid']:04X} → {result['to_pid']:04X} · 未访问硬件")
        except (ValueError, OSError, KeyError) as exc:
            self.boot_result.setText(f"冷启动离线验证未完成：{exc}")

    def refresh_protocol(self):
        if self.engine is None:
            return
        if isinstance(self.engine, RemoteEngine):
            if self.engine.source == "usb":
                self.protocol_stats.setText("Rust 独立核心 · DT9837C USB")
                self.protocol_log.setPlainText(json.dumps(self.engine.profile.get("device"), ensure_ascii=False, indent=2)
                                              + "\n样本时间轴使用硬件采样率和扫描序号；不使用 GUI 刷新时间。")
                return
            self.protocol_stats.setText("Rust 独立核心 · 不产生 USB 命令")
            self.protocol_log.setPlainText("核心负责电压生成、AC 演示模型、24 位量化和原始写盘。\n"
                                          "控制与显示通过本机连接；GUI 退出不停止采集。")
            return
        if self.engine.transport is None:
            self.protocol_stats.setText("HPF 文件输入 · 不产生 USB 命令")
            self.protocol_log.setPlainText("直接回放原始电压；不再次进行 ADC 量化、耦合滤波或启动延迟补偿。")
            return
        stats = self.engine.transport.stats
        self.protocol_stats.setText("  ·  ".join(f"{name}: {stats.get(key, 0):,}" for key, name in
                                               [("commands", "命令"), ("command_bytes", "命令字节"), ("stream_bytes", "采样字节")]))
        trace = self.engine.transport.command_log[-12:]
        self.protocol_log.setPlainText("最近的 64 字节命令（十六进制）\n\n" + "\n\n".join(packet.hex(" ") for packet in trace))

    def inject_fault(self):
        if self.engine and self.engine.running and self.engine.transport is not None:
            self.engine.transport.inject_fault(self.fault.currentData())
            self.tell("故障已注入模拟传输，等待采集线程处理。")
        else:
            self.tell("故障注入只适用于正在运行的协议模拟输入。")

    def closeEvent(self, event):
        if self._connecting:
            self._close_after_connect = True
            event.ignore()
            return
        self.timer.stop()
        if isinstance(self.engine, RemoteEngine):
            if not self.engine.running and not self.engine.connection_error:
                try:
                    self.engine.shutdown()
                except (OSError, ValueError, RuntimeError):
                    pass  # Window closure must never stop an unknown active record.
            self.engine.detach()
        elif self.engine:
            self.engine.stop()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="LabDAQ 中文桌面原型")
    parser.add_argument("--demo", action="store_true", help="打开后开始模拟预览")
    args = parser.parse_args()
    app = W.QApplication.instance() or W.QApplication(sys.argv[:1])
    configure_application(app)
    window = MainWindow()
    if window.engine is None:
        window.scenario.setCurrentIndex(window.scenario.findData("rust" if args.demo else "usb"))
    window.show()
    if args.demo:
        QtCore.QTimer.singleShot(100, window.start_preview)
    else:
        QtCore.QTimer.singleShot(100, window.auto_connect_device)
    return app.exec()
