"""Chinese desktop workbench for simulated voltage and original HPF inputs."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W

from .engine import AcquisitionEngine
from .core_client import DEFAULT_ENDPOINT, RemoteEngine
from .inputs import HpfReplaySession
from .signals import SimulationSettings
from .input_dialog import SignalDialog
from .processing import ChannelSettings, convert_volts, spectrum, frequency_response, filter_window
from .protocol import AcquisitionConfig
from .recording import load_session, next_recording_path
from .data_dialog import ExportDialog, StatisticsDialog
from .recording_library import RecordingLibraryDialog
from .plot_measurements import PlotMeasurements
from .transport import enumerate_usb_devices

ROOT = Path(__file__).resolve().parents[1]
COLORS = ["#2878F0", "#0C9A83", "#ED9645", "#9460DA"]

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 13px; color: #26354D; }
QMainWindow, QWidget#workspace { background: #F3F6FA; }
QFrame#sidebar { background: #15263D; border: none; }
QFrame#sidebar QLabel { color: #CAD5E5; background: transparent; }
QFrame#sidebar QLabel#brand { color: white; font-size: 24px; font-weight: 700; }
QFrame#sidebar QLabel#sideCaption { color: #8C9EB7; font-size: 11px; }
QListWidget#nav { background: transparent; border: none; color: #C3CFE0; outline: none; }
QListWidget#nav::item { padding: 15px 16px; margin: 4px 0; border-radius: 8px; }
QListWidget#nav::item:selected { background: #29445F; color: #FFFFFF; border-left: 3px solid #5BD9C3; }
QListWidget#nav::item:hover { background: #20364E; }
QFrame#card { background: white; border: 1px solid #E0E7EF; border-radius: 10px; }
QLabel#pageTitle { font-size: 24px; font-weight: 700; color: #172A43; }
QLabel#sectionTitle { font-size: 15px; font-weight: 700; }
QLabel#muted { color: #75849A; font-size: 12px; }
QLabel#metric { font-size: 31px; font-weight: 650; }
QLabel#badge { background: #FFF1D5; border: 1px solid #F3D6A0; border-radius: 6px; color: #966216; padding: 7px 12px; }
QLabel#notice { background: #EAF0F7; color: #45607E; border-radius: 6px; padding: 9px 12px; }
QPushButton { background: white; border: 1px solid #D5DEE9; border-radius: 6px; padding: 8px 13px; font-weight: 500; }
QPushButton:hover { background: #EDF4FF; border-color: #96B9EE; }
QPushButton:pressed { background: #DCEBFF; }
QPushButton:disabled { background: #F3F5F8; color: #A1ACBB; border-color: #E2E7ED; }
QPushButton#primary { background: #276FE3; color: white; border-color: #276FE3; }
QPushButton#primary:hover { background: #185DCF; }
QPushButton#primary:disabled { background: #BCCEEB; border-color: #BCCEEB; }
QPushButton#record { color: #C94C56; border-color: #E8BBC0; }
QPushButton#record:checked { background: #C94C56; color: white; }
QPushButton:checked { background: #E3EDFB; }
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit { background: white; border: 1px solid #D5DEE9; border-radius: 5px; padding: 7px; min-height: 19px; }
QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled { background: #F1F4F8; color: #97A3B3; }
QComboBox::drop-down { border: none; width: 24px; }
QTableWidget { background: white; border: 1px solid #E1E7EF; gridline-color: #EEF1F6; selection-background-color: #E8F0FD; }
QHeaderView::section { background: #F4F7FB; color: #61738C; border: none; border-bottom: 1px solid #DEE6EF; padding: 9px; }
QPlainTextEdit { background: #F6F8FB; border: 1px solid #DFE6EF; border-radius: 6px; padding: 8px; font-family: Consolas, 'Microsoft YaHei UI'; font-size: 12px; }
QTabWidget::pane { border: 1px solid #DFE6EF; background: white; }
QTabBar::tab { padding: 10px 18px; background: #EDF2F8; }
QTabBar::tab:selected { background: white; color: #276FE3; }
QSlider::groove:horizontal { height: 5px; background: #D9E3F0; border-radius: 2px; }
QSlider::handle:horizontal { width: 16px; margin: -6px 0; border-radius: 8px; background: #276FE3; }
QCheckBox { spacing: 7px; }
QToolTip { background: #172A43; color: white; border: none; padding: 5px; }
"""


def label(text: str, name: str = "", wrap: bool = False) -> W.QLabel:
    result = W.QLabel(text)
    result.setObjectName(name)
    result.setWordWrap(wrap)
    return result


def button(text: str, callback=None, name="") -> W.QPushButton:
    result = W.QPushButton(text)
    result.setObjectName(name)
    result.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    if callback is not None:
        result.clicked.connect(callback)
    return result


def card() -> tuple[W.QFrame, W.QVBoxLayout]:
    frame = W.QFrame()
    frame.setObjectName("card")
    layout = W.QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    return frame, layout


def plot_widget(ylabel="力", unit="N") -> pg.PlotWidget:
    plot = pg.PlotWidget(background="w")
    plot.showGrid(x=True, y=True, alpha=0.12)
    plot.setLabel("bottom", "时间", units="s")
    plot.setLabel("left", ylabel, units=unit)
    # Prefixing a compound unit such as V²/Hz would visually turn milli(V²)
    # into mV², which denotes a different scale. Keep y units explicit.
    plot.getAxis("left").enableAutoSIPrefix(False)
    plot.setMenuEnabled(False)
    plot.hideButtons()
    for axis in ("left", "bottom"):
        plot.getAxis(axis).setPen("#C9D3E1")
        plot.getAxis(axis).setTextPen("#6C7E96")
    return plot


def configure_application(app):
    # Qt's offscreen Windows plugin does not always discover system fonts.
    # Register the same installed CJK faces used by the native desktop window.
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msyh.ttc", "msyhbd.ttc"):
        path = font_dir / name
        if path.exists():
            QtGui.QFontDatabase.addApplicationFont(str(path))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
    pg.setConfigOptions(antialias=True, foreground="#5D708B")


class ChannelDialog(W.QDialog):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.setWindowTitle("通道与传感器配置")
        self.resize(1150, 440)
        self.window = window
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.addWidget(label("通道与传感器", "pageTitle"))
        layout.addWidget(label("当前 10 mV/N 为演示灵敏度。实际测量请填写传感器与调理器的换算参数。", "notice", True))
        self.table = W.QTableWidget(4, 9)
        self.table.setHorizontalHeaderLabels(["启用", "通道名称", "单位", "mV / 单位", "零点 / V", "反向", "量程", "耦合", "IEPE"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, W.QHeaderView.ResizeMode.Stretch)
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
        layout.addWidget(self.table)
        layout.addWidget(label("换算：工程值 = 1000 × (原始电压[V] − 零点[V]) ÷ 灵敏度[mV/单位]。单位为 V 时不使用灵敏度；原始电压始终保留。", "muted", True))
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok | W.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(W.QDialogButtonBox.StandardButton.Ok).setText("应用设置")
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
            w.channels[i] = ChannelSettings(i, name.text().strip() or f"通道 {i}", unit.currentText().strip() or "N",
                                           sensitivity.value(), zero.value(), inverted.isChecked())
            w.ranges[i] = 10.0 if ranges.currentIndex() == 0 else 1.0
            w.coupling[i] = coupling.currentText()
            w.iepe[i] = iepe.isChecked()
        w.active_channels = active
        self.accept()


class MainWindow(W.QMainWindow):
    def __init__(self, *, core_endpoint=DEFAULT_ENDPOINT):
        super().__init__()
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
        self.recording_notes = ""
        self._completion_shown: Path | None = None
        self._previous_zero: list[float] | None = None
        self._live_cache = None
        self.replay = None
        self.replay_path: Path | None = None
        self.replay_is_hpf = False
        self._error_shown = ""
        self._tick = 0
        self._build()
        self.scenario.currentIndexChanged.connect(self._input_changed)
        self._input_changed()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)
        try:
            self.engine = RemoteEngine.reconnect(self.core_endpoint)
            if self.engine:
                self._restore_core_session()
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell(f"无法连接采集核心：{exc}")

    def _restore_core_session(self):
        p = self.engine.profile
        self.channels = [ChannelSettings(**c) for c in p["channels"]]
        config = self.engine.config
        self.active_channels = tuple(config.channels)
        self.ranges, self.coupling, self.iepe = list(config.ranges), list(config.coupling), list(config.iepe)
        self.signal_settings = SimulationSettings.from_dict(p["signal_settings"])
        self.rate.setValue(config.sample_rate)
        self.experiment.setText(p["experiment"])
        self._set_recording_options(p["recording_options"])
        self.recording_root = Path(p["recording_root"])
        self.directory_label.setText(str(self.recording_root))
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
        side = W.QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(196)
        sl = W.QVBoxLayout(side)
        sl.setContentsMargins(18, 30, 18, 24)
        sl.addWidget(label("LabDAQ", "brand"))
        sl.addWidget(label("实验室信号工作台", "sideCaption"))
        sl.addSpacing(25)
        self.nav = W.QListWidget()
        self.nav.setObjectName("nav")
        self.nav.addItems(["实时采集", "信号分析", "数据回看", "设备与协议"])
        self.nav.setSpacing(2)
        sl.addWidget(self.nav)
        sl.addWidget(label("DT9837C", "sectionTitle"))
        sl.addWidget(label("4 通道 · 24 bit · USB 2.0", "sideCaption"))
        sl.addSpacing(10)
        sl.addWidget(label("协议模拟原型  0.1\n实际硬件采集尚未启用", "sideCaption", True))
        outer.addWidget(side)
        work = W.QWidget()
        work.setObjectName("workspace")
        body = W.QVBoxLayout(work)
        body.setContentsMargins(25, 24, 25, 16)
        body.setSpacing(18)
        top = W.QHBoxLayout()
        titles = W.QVBoxLayout()
        self.title = label("实时采集", "pageTitle")
        titles.addWidget(self.title)
        self.subtitle = label("配置传感器，观察信号，记录每一次实验。", "muted")
        titles.addWidget(self.subtitle)
        top.addLayout(titles)
        top.addStretch()
        self.mode_badge = label("●  协议模拟 · 非真实测量", "badge")
        top.addWidget(self.mode_badge)
        body.addLayout(top)
        self.pages = W.QStackedWidget()
        self.pages.addWidget(self._live_page())
        self.pages.addWidget(self._analysis_page())
        self.pages.addWidget(self._replay_page())
        self.pages.addWidget(self._device_page())
        body.addWidget(self.pages, 1)
        self.notice = label("准备就绪 · 选择演示信号后开始预览。原始数据只在“开始记录”后保存。", "notice", True)
        body.addWidget(self.notice)
        outer.addWidget(work, 1)
        self.nav.currentRowChanged.connect(self._page_changed)
        self.nav.setCurrentRow(0)
        self._sync_channel_labels()

    def _page_changed(self, index):
        if index < 0:
            return
        self.pages.setCurrentIndex(index)
        self.title.setText(["实时采集", "信号分析", "数据回看", "设备与协议"][index])
        self.subtitle.setText(["配置传感器，观察信号，记录每一次实验。", "从时间波形到频域，明确每一次分析的来源与单位。",
                               "保留原始信号，让数据可以重新查看、分析和导出。", "查看设备描述符、协议命令和离线冷启动流程。"][index])
        self._update_mode_badge()
        if index == 1:
            self.refresh_analysis()
        if index == 3:
            self.refresh_protocol()

    def _live_page(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        metrics = W.QHBoxLayout()
        metrics.setSpacing(12)
        self.metric_titles, self.metric_values, self.metric_subs = [], [], []
        for i in range(4):
            frame, fl = card()
            title = label(f"AI {i}", "muted")
            value = label("—", "metric")
            value.setStyleSheet(f"color: {COLORS[i]};")
            sub = label("等待采样", "muted")
            fl.addWidget(title)
            fl.addWidget(value)
            fl.addWidget(sub)
            metrics.addWidget(frame)
            self.metric_titles.append(title)
            self.metric_values.append(value)
            self.metric_subs.append(sub)
        layout.addLayout(metrics)
        split = W.QHBoxLayout()
        split.setSpacing(16)
        plot_card, pl = card()
        plot_top = W.QHBoxLayout()
        plot_top.addWidget(label("实时波形", "sectionTitle"))
        plot_top.addStretch()
        self.pause_plot = W.QCheckBox("暂停滚动")
        self.pause_plot.setToolTip("只暂停曲线显示；采集和文件记录会继续。")
        plot_top.addWidget(self.pause_plot)
        plot_top.addWidget(button("保存图像", lambda: self.save_plot_image(self.live_plot)))
        plot_top.addWidget(button("适应窗口", lambda: self.live_plot.enableAutoRange()))
        pl.addLayout(plot_top)
        self.live_plot = plot_widget()
        self.live_plot.addLegend(offset=(10, 8), brush=(255, 255, 255, 235), pen="#E4EAF1")
        self.live_curves = [self.live_plot.plot(pen=pg.mkPen(c, width=1.6), name=f"AI {i}", autoDownsample=True,
                                               downsampleMethod="peak") for i, c in enumerate(COLORS)]
        pl.addWidget(self.live_plot, 1)
        self.live_measurements = PlotMeasurements(self.live_plot)
        pl.addWidget(self.live_measurements)
        self.live_info = label("时间轴来自样本序号 · 鼠标拖动平移，滚轮缩放", "muted", True)
        pl.addWidget(self.live_info)
        split.addWidget(plot_card, 1)
        options, ol = card()
        options_scroll = W.QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setFixedWidth(280)
        options_scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        ol.setSizeConstraint(W.QLayout.SizeConstraint.SetMinimumSize)
        ol.addWidget(label("本次实验", "sectionTitle"))
        self.experiment = W.QLineEdit("力传感器实验")
        ol.addWidget(self.experiment)
        ol.addWidget(label("输入来源", "muted"))
        self.scenario = W.QComboBox()
        self.scenario.addItem("拖拽载荷 · 周期接触", "drag")
        self.scenario.addItem("正弦 · 频谱验证", "sine")
        self.scenario.addItem("阶跃 · 零点与响应", "step")
        self.scenario.addItem("可配置电信号 · 电压模拟", "custom")
        self.scenario.addItem("原始 HPF · 按时间回放", "hpf")
        self.scenario.addItem("独立核心 · 电信号模拟", "rust")
        self.scenario.addItem("DT9837C · 真实 USB 输入", "usb")
        ol.addWidget(self.scenario)
        self.input_button = button("配置电信号", self.edit_input)
        ol.addWidget(self.input_button)
        self.hpf_loop = W.QCheckBox("循环回放（首尾会产生接缝）")
        ol.addWidget(self.hpf_loop)
        self.input_info = label("", "muted", True)
        ol.addWidget(self.input_info)
        ol.addWidget(label("采样率 / 每通道", "muted"))
        self.rate = W.QDoubleSpinBox()
        self.rate.setDecimals(3)
        self.rate.setRange(195.313, 105469)
        self.rate.setValue(2000)
        self.rate.setSuffix("  S/s")
        ol.addWidget(self.rate)
        self.actual_rate = label("实际时钟：开始后显示", "muted")
        self.actual_rate.setToolTip("此处显示 PLL 计算出的名义采样率；晶振误差和真实时序尚未测量。")
        ol.addWidget(self.actual_rate)
        ol.addWidget(label("时间轴", "muted"))
        self.live_time_axis = W.QComboBox()
        for title, mode in (("相对时间", "relative"), ("绝对时间（电脑时钟）", "absolute"), ("样本号", "samples")):
            self.live_time_axis.addItem(title, mode)
        self.live_time_axis.currentIndexChanged.connect(lambda: self._time_axis_changed("live"))
        ol.addWidget(self.live_time_axis)
        ol.addWidget(label("数值读数（近 1 秒）", "muted"))
        self.live_value_mode = W.QComboBox()
        for title, mode in (("最新值", "last"), ("首值", "first"), ("均值", "mean"), ("最小值", "min"),
                            ("最大值", "max"), ("RMS", "rms"), ("标准差", "std"), ("峰峰值", "delta")):
            self.live_value_mode.addItem(title, mode)
        ol.addWidget(self.live_value_mode)
        self.value_decimals = W.QSpinBox()
        self.value_decimals.setRange(0, 9)
        self.value_decimals.setValue(3)
        self.value_decimals.setPrefix("显示小数位：")
        ol.addWidget(self.value_decimals)
        self.channel_button = button("通道与传感器设置", self.edit_channels)
        ol.addWidget(self.channel_button)
        self.zero_button = button("当前载荷设为零点", self.zero_channels)
        self.zero_button.setToolTip("以最近最多1秒原始电压的平均值设置零点，原始记录不变。")
        ol.addWidget(self.zero_button)
        self.undo_zero_button = button("撤销上次调零", self.undo_zero)
        self.undo_zero_button.setEnabled(False)
        ol.addWidget(self.undo_zero_button)
        row = W.QHBoxLayout()
        self.save_config_button = button("保存配置", self.save_profile)
        self.load_config_button = button("载入配置", self.load_profile)
        row.addWidget(self.save_config_button)
        row.addWidget(self.load_config_button)
        ol.addLayout(row)
        self.record_settings_button = button("记录设置 · 时长与备注", self.edit_recording_options)
        ol.addWidget(self.record_settings_button)
        self.record_settings_info = label("手动结束 · 自动编号", "muted", True)
        ol.addWidget(self.record_settings_info)
        ol.addStretch()
        ol.addWidget(label("记录目录", "muted"))
        self.directory_label = label(str(self.recording_root), "muted", True)
        ol.addWidget(self.directory_label)
        self.disk_space_label = label("", "muted")
        ol.addWidget(self.disk_space_label)
        self._refresh_disk_space()
        self.directory_button = button("更改目录", self.choose_directory)
        ol.addWidget(self.directory_button)
        options_scroll.setWidget(options)
        split.addWidget(options_scroll)
        layout.addLayout(split, 1)
        toolbar, tl = card()
        tr = W.QHBoxLayout()
        self.device_button = button("连接设备", self.toggle_device_connection)
        self.preview_button = button("开始预览", self.start_preview, "primary")
        self.record_button = button("●  开始记录", self.toggle_record, "record")
        self.record_button.setCheckable(True)
        self.pause_record_button = button("暂停记录", self.toggle_record_pause)
        self.stop_button = button("■  停止采集", self.stop_acquisition)
        self.status_label = label("未开始", "sectionTitle")
        self.count_label = label("0 扫描 · 0.00 s", "muted")
        for b in [self.device_button, self.preview_button, self.record_button, self.pause_record_button, self.stop_button]:
            tr.addWidget(b)
        tr.addStretch()
        tr.addWidget(self.status_label)
        tr.addSpacing(12)
        tr.addWidget(self.count_label)
        tl.addLayout(tr)
        layout.addWidget(toolbar)
        return page

    def _analysis_page(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        frame, fl = card()
        row = W.QHBoxLayout()
        self.analysis_source = W.QComboBox()
        self.analysis_source.addItems(["实时采样窗口", "回看中的数据窗口"])
        row.addWidget(self.analysis_source)
        self.analysis_channel = W.QComboBox()
        self.reference_channel = W.QComboBox()
        for channel_combo in (self.analysis_channel, self.reference_channel):
            channel_combo.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.analysis_mode = W.QComboBox()
        self.analysis_mode.addItems(["幅值谱 · 峰值", "功率谱密度 PSD", "频率响应 H1 / 相干性"])
        self.window_type = W.QComboBox()
        self.window_type.addItems(["hann", "boxcar", "hamming", "blackman", "flattop"])
        row.addStretch()
        row.addWidget(self.analysis_channel)
        row.addWidget(self.analysis_mode)
        row.addWidget(self.window_type)
        fl.addLayout(row)
        second = W.QHBoxLayout()
        second.addWidget(label("H1 参考通道", "muted"))
        second.addWidget(self.reference_channel)
        second.addSpacing(20)
        self.lowpass = W.QCheckBox("窗口零相位低通")
        self.lowpass.setToolTip("4 阶 Butterworth 前后向零相位滤波：有效阶数 8，填写频率处总响应约 −6 dB。仅分析窗口使用，不修改记录。")
        second.addWidget(self.lowpass)
        self.cutoff = W.QDoubleSpinBox()
        self.cutoff.setRange(0.1, 50000)
        self.cutoff.setValue(100)
        self.cutoff.setSuffix(" Hz")
        second.addWidget(self.cutoff)
        second.addStretch()
        fl.addLayout(second)
        self.analysis_plot = plot_widget("幅值", "N")
        self.analysis_plot.setLabel("bottom", "频率", units="Hz")
        self.analysis_curve = self.analysis_plot.plot(pen=pg.mkPen(COLORS[0], width=1.5))
        fl.addWidget(self.analysis_plot, 1)
        self.coherence_plot = plot_widget("相干性", "")
        self.coherence_plot.setLabel("bottom", "频率", units="Hz")
        self.coherence_plot.setMaximumHeight(180)
        self.coherence_plot.setYRange(0, 1)
        self.coherence_curve = self.coherence_plot.plot(pen=COLORS[1])
        fl.addWidget(self.coherence_plot)
        self.coherence_plot.hide()
        self.analysis_info = label("开始预览后可分析。幅值谱保留 DC；低通仅用于此页，不改变记录的原始电压。", "muted", True)
        fl.addWidget(self.analysis_info)
        layout.addWidget(frame, 1)
        for widget in [self.analysis_channel, self.reference_channel, self.analysis_mode, self.window_type]:
            widget.currentIndexChanged.connect(self.refresh_analysis)
        self.lowpass.toggled.connect(self.refresh_analysis)
        self.cutoff.valueChanged.connect(self.refresh_analysis)
        self.analysis_source.currentIndexChanged.connect(self._analysis_source_changed)
        return page

    def _replay_page(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        frame, fl = card()
        row = W.QHBoxLayout()
        row.addWidget(label("原始记录回看", "sectionTitle"))
        row.addStretch()
        row.addWidget(button("记录库", self.open_recording_library))
        row.addWidget(button("打开记录文件夹", self.choose_replay))
        row.addWidget(button("打开 HPF", self.choose_hpf))
        row.addWidget(button("打开最近记录", self.open_last_recording))
        row.addWidget(button("导出数据", self.export_replay))
        row.addWidget(button("保存图像", lambda: self.save_plot_image(self.replay_plot)))
        fl.addLayout(row)
        row = W.QHBoxLayout()
        row.addWidget(button("读取原厂 FFT 样例", self.open_vendor_sample))
        row.addWidget(button("分析此窗口", self.analyze_replay))
        self.replay_time_axis = W.QComboBox()
        for title, mode in (("相对时间", "relative"), ("绝对时间（电脑时钟）", "absolute"), ("样本号", "samples")):
            self.replay_time_axis.addItem(title, mode)
        self.replay_time_axis.currentIndexChanged.connect(lambda: self._time_axis_changed("replay"))
        row.addWidget(self.replay_time_axis)
        row.addStretch()
        row.addWidget(label("显示时长", "muted"))
        self.replay_seconds = W.QDoubleSpinBox()
        self.replay_seconds.setRange(.002, 60)
        self.replay_seconds.setDecimals(3)
        self.replay_seconds.setValue(10)
        self.replay_seconds.setSuffix(" s")
        self.replay_seconds.valueChanged.connect(self.draw_replay)
        row.addWidget(self.replay_seconds)
        fl.addLayout(row)
        self.replay_info = label("支持原始记录、同步电压 HPF 和已验证的 DT9837C 力记录；按 HPF 原配置换算。", "notice", True)
        fl.addWidget(self.replay_info)
        row = W.QHBoxLayout()
        row.addWidget(label("显示与取数通道", "muted"))
        self.replay_channel_checks = []
        for i in range(4):
            check = W.QCheckBox(f"AI {i}")
            check.setEnabled(False)
            check.toggled.connect(self.draw_replay)
            self.replay_channel_checks.append(check)
            row.addWidget(check)
        row.addStretch()
        self.statistics_button = button("统计所选数据", self.show_replay_statistics)
        row.addWidget(self.statistics_button)
        fl.addLayout(row)
        self.replay_plot = plot_widget()
        self.replay_plot.addLegend(offset=(10, 8), brush=(255, 255, 255, 235), pen="#E4EAF1")
        self.replay_curves = [self.replay_plot.plot(pen=pg.mkPen(c, width=1.5), name=f"AI {i}",
                                                  autoDownsample=True, downsampleMethod="peak") for i, c in enumerate(COLORS)]
        self.replay_region = pg.LinearRegionItem(values=(0, 1), brush=(40, 120, 240, 30),
                                                 pen=pg.mkPen("#2878F0", width=1))
        self.replay_plot.addItem(self.replay_region, ignoreBounds=True)
        self.replay_region.hide()
        self.replay_region.sigRegionChangeFinished.connect(self._replay_region_changed)
        fl.addWidget(self.replay_plot, 1)
        self.replay_measurements = PlotMeasurements(self.replay_plot)
        fl.addWidget(self.replay_measurements)
        row = W.QHBoxLayout()
        row.addWidget(button("上一窗口", lambda: self.step_replay(-1)))
        row.addWidget(button("下一窗口", lambda: self.step_replay(1)))
        self.replay_start = W.QDoubleSpinBox()
        self.replay_start.setDecimals(9)
        self.replay_start.setRange(0, 0)
        self.replay_start.setSuffix(" s")
        self.replay_start.valueChanged.connect(self.draw_replay)
        row.addWidget(label("起点", "muted"))
        row.addWidget(self.replay_start)
        self.replay_slider = W.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.replay_slider.setRange(0, 1000)
        self.replay_slider.valueChanged.connect(self._replay_slider_changed)
        row.addWidget(self.replay_slider, 1)
        self.replay_position = label("0.00 s", "muted")
        row.addWidget(self.replay_position)
        fl.addLayout(row)
        row = W.QHBoxLayout()
        self.replay_select = W.QCheckBox("使用选区")
        self.replay_select.toggled.connect(self._replay_selection_changed)
        row.addWidget(self.replay_select)
        self.selection_start, self.selection_stop = W.QDoubleSpinBox(), W.QDoubleSpinBox()
        for spin in (self.selection_start, self.selection_stop):
            spin.setDecimals(9)
            spin.setRange(0, 0)
            spin.setSuffix(" s")
            spin.setEnabled(False)
            spin.valueChanged.connect(self._replay_selection_changed)
        row.addWidget(self.selection_start)
        row.addWidget(label("至", "muted"))
        row.addWidget(self.selection_stop)
        row.addWidget(button("选当前窗口", self.select_replay_window))
        row.addStretch()
        fl.addLayout(row)
        fl.addWidget(label("统计与导出使用勾选通道；未启用选区时处理全记录。选区按最近样本边界取整，右端不含。", "muted", True))
        layout.addWidget(frame)
        return page

    def _device_page(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        frame, fl = card()
        fl.addWidget(label("独立 USB 接入研究", "sectionTitle"))
        fl.addWidget(label("真实输入由独立核心连接 DT9837C。选择真实输入后可先连接设备，再开始预览；停止采集保持连接，断开设备关闭工作电源。扫描不改变设备配置。", "notice", True))
        row = W.QHBoxLayout()
        row.addWidget(button("查找 DT USB 设备", self.scan_devices))
        row.addWidget(button("冷启动离线验证", self.simulate_boot))
        row.addWidget(button("打开技术说明", lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(ROOT / "docs" / "开发文档" / "technical-notes.md")))))
        row.addStretch()
        fl.addLayout(row)
        self.device_result = label("尚未扫描 · 扫描只读取设备描述符，不更换驱动或初始化设备。", "muted", True)
        fl.addWidget(self.device_result)
        self.boot_result = label("冷启动验证使用已归档固件记录，只在内存中模拟 RAM 写入与重新枚举。", "muted", True)
        fl.addWidget(self.boot_result)
        fl.addWidget(label("通信观察", "sectionTitle"))
        self.protocol_stats = label("开始预览后可查看二进制命令与传输计数。", "muted")
        fl.addWidget(self.protocol_stats)
        self.protocol_log = W.QPlainTextEdit()
        self.protocol_log.setReadOnly(True)
        fl.addWidget(self.protocol_log, 1)
        row = W.QHBoxLayout()
        row.addWidget(label("模拟故障", "muted"))
        self.fault = W.QComboBox()
        self.fault.addItem("传输超时", "timeout")
        self.fault.addItem("设备断连", "disconnect")
        self.fault.addItem("数据溢出", "overrun")
        row.addWidget(self.fault)
        self.fault_button = button("注入一次故障", self.inject_fault)
        row.addWidget(self.fault_button)
        row.addStretch()
        fl.addLayout(row)
        fl.addWidget(label("外部触发、模拟输出和多设备同步已有技术分析，本版界面尚未开放这些操作。", "muted", True))
        layout.addWidget(frame)
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
        self.rate.setValue(config.sample_rate)  # Display only; replay uses the reader's full precision.
        self._sync_channel_labels()

    def _sync_channel_labels(self):
        self.live_plot.plotItem.legend.clear()
        for i, c in enumerate(self.channels):
            self.metric_titles[i].setText(f"AI {i}  ·  {c.name}  /  {c.unit}")
            if i in self.active_channels:
                self.live_plot.plotItem.legend.addItem(self.live_curves[i], f"AI {i} · {c.name}")
            if i not in self.active_channels:
                self.metric_values[i].setText("—")
                self.metric_subs[i].setText("未启用")
        self._analysis_source_changed()

    def _analysis_source_changed(self, *_):
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
        file_view = index == 2 or (index == 1 and self.analysis_source.currentIndex() == 1)
        if file_view and self.replay is not None:
            self.mode_badge.setText("HPF 文件 · 离线回放" if self.replay_is_hpf else "记录回放 · " + ("模拟数据" if self.replay.metadata.get("source") == "simulation" else "文件数据"))
        elif self.scenario.currentData() == "hpf":
            self.mode_badge.setText("●  HPF 输入回放 · 非现场采集")
        elif self.scenario.currentData() == "rust":
            self.mode_badge.setText("●  独立核心模拟 · 非真实测量")
        elif self.scenario.currentData() == "usb":
            self.mode_badge.setText("●  DT9837C · 真实 USB 输入")
        else:
            self.mode_badge.setText("●  协议模拟 · 非真实测量")

    def tell(self, text):
        self.notice.setText(text)

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
                self._discard_preview()
                config = AcquisitionConfig(sample_rate=self.rate.value(), channels=self.active_channels,
                                           ranges=tuple(self.ranges), coupling=tuple(self.coupling), iepe=tuple(self.iepe))
                self.engine = RemoteEngine.start_new(config, self.channels, self.signal_settings,
                    self.experiment.text(), self.recording_root, endpoint=self.core_endpoint,
                    source="usb", connect_only=True)
                self._error_shown = ""
                self.actual_rate.setText(f"名义采样率：{self.engine.sample_rate:,.3f} S/s")
                self.tell("设备已连接，工作电源已开启；尚未采集或记录。")
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell(f"设备连接操作失败：{exc}")
        self._refresh_buttons()

    def _begin_input(self, recording_path=None, *, recording_options=None):
        if self.engine is not None and self.engine.running:
            return
        try:
            if self.engine is not None:
                self.engine.stop()
                if isinstance(self.engine, RemoteEngine):
                    self.engine.detach()
            mode = self.scenario.currentData()
            session = None
            if mode == "hpf":
                if self.input_hpf is None:
                    raise ValueError("请先选择原始 HPF")
                config = self.input_hpf_config
                session = HpfReplaySession(self.input_hpf, loop=self.hpf_loop.isChecked())
            else:
                config = AcquisitionConfig(sample_rate=self.rate.value(), channels=self.active_channels,
                                           ranges=tuple(self.ranges), coupling=tuple(self.coupling), iepe=tuple(self.iepe))
            if mode in ("rust", "usb"):
                self.engine = RemoteEngine.start_new(config, self.channels, self.signal_settings,
                                                      self.experiment.text(), self.recording_root, recording_path,
                                                      endpoint=self.core_endpoint,
                                                      source="usb" if mode == "usb" else "simulation",
                                                      recording_options=recording_options or self._recording_options())
            else:
                self.engine = AcquisitionEngine(config, self._selected_settings(), mode,
                                                signal_settings=self.signal_settings if mode == "custom" else None,
                                                input_session=session)
                self.engine.start(recording_path=recording_path, recording_options=recording_options,
                                  experiment=self.experiment.text())
            self._error_shown = ""
            if recording_path:
                self.latest_recording = recording_path
            self.actual_rate.setText(f"实际时钟：{self.engine.sample_rate:,.3f} S/s")
            self.actual_rate.setToolTip("HPF 文件保存的原始采样率，不重新取整。" if mode == "hpf" else "PLL 寄存器计算的名义采样率；未进行外部时钟校准。")
            self.pause_plot.setChecked(False)
            self.live_plot.enableAutoRange()
            origin = "HPF 输入回放" if mode == "hpf" else "DT9837C 真实输入" if mode == "usb" else "模拟信号"
            self.tell(f"正在记录{origin} · {recording_path}" if recording_path else
                      f"正在预览{origin} · 尚未保存。点击“开始记录”保存原始电压与本次配置。")
        except Exception as exc:
            self.tell(f"无法开始预览：{exc}")
        self._refresh_buttons()

    def stop_acquisition(self):
        try:
            if self.engine is not None:
                self.engine.stop()
            connected = isinstance(self.engine, RemoteEngine) and self.engine.device_connected
            self.tell("采集已停止，设备保持连接；点击“断开设备”可关闭工作电源。" if connected else
                      "采集已停止。已记录的数据可在“数据回看”中打开。")
        except Exception as exc:
            self.tell(f"停止采集：{exc}")
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
        self.device_button.setVisible(self.scenario.currentData() == "usb")
        self.device_button.setText("断开设备" if connected else "连接设备")
        self.device_button.setEnabled(not running)
        from_hpf = self.scenario.currentData() == "hpf"
        self.preview_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.record_button.setChecked(recording)
        self.record_button.setText("■  结束记录" if recording else "●  开始记录")
        paused = recording and self.engine.recording_paused
        self.pause_record_button.setEnabled(recording and running)
        self.pause_record_button.setText("继续记录" if paused else "暂停记录")
        for widget in [self.scenario, self.load_config_button, self.input_button, self.hpf_loop]:
            widget.setEnabled(not running)
        self.rate.setEnabled(not running and not from_hpf)
        self.channel_button.setEnabled(not running and not from_hpf)
        self.zero_button.setEnabled(running and not recording and not from_hpf)
        self.undo_zero_button.setEnabled(running and not recording and not from_hpf and self._previous_zero is not None)
        self.fault_button.setEnabled(running and self.engine.transport is not None)
        self.directory_button.setEnabled(not recording)
        self.experiment.setEnabled(not recording)
        self.record_settings_button.setEnabled(not recording)
        ended = self.engine is not None and not self.engine.error and self.engine.termination_reason == "end_of_file"
        self.status_label.setText("正在记录" if recording else "预览中 · 未记录" if running else
                                  "已连接 · 未采集" if connected else "HPF 已播完" if ended else
                                  "已断开" if self.engine and self.engine.source == "usb" else "已停止" if self.engine else "未开始")
        if not recording and running and self.engine.termination_reason == "duration_complete":
            self.status_label.setText("定长已完成 · 预览中")
        if paused:
            self.status_label.setText("记录已暂停 · 预览继续")
        if isinstance(self.engine, RemoteEngine) and self.engine.connection_error:
            self.status_label.setText("核心连接中断 · 状态未知")
            for widget in [self.preview_button, self.record_button, self.zero_button, self.scenario,
                           self.rate, self.channel_button, self.load_config_button, self.input_button, self.device_button,
                           self.record_settings_button, self.undo_zero_button, self.pause_record_button]:
                widget.setEnabled(False)

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
        if self._tick % 50 == 0:
            self._refresh_disk_space()
        if self.engine is None:
            return
        self._refresh_buttons()
        if isinstance(self.engine, RemoteEngine) and self.engine.connection_error:
            self.tell(self.engine.connection_error)
            return
        if self.engine.error and self.engine.error != self._error_shown:
            self._error_shown = self.engine.error
            self.tell(f"采集已中止：{self.engine.error}。已有记录已尝试关闭；可重新开始预览。")
        if (not self.engine.recording and self.engine.termination_reason == "duration_complete"
                and self.engine.last_recording != self._completion_shown):
            self._completion_shown = self.engine.last_recording
            self.tell(f"本组定长记录已完成并保存 · {self.engine.last_recording}")
        times, raw = self.engine.snapshot()
        if len(raw) == 0:
            return
        converted = convert_volts(raw, self._selected_settings())
        for j, i in enumerate(self.active_channels):
            c = self.channels[i]
            recent = converted[-min(len(raw), max(1, round(self.engine.sample_rate))):, j]
            mode = self.live_value_mode.currentData()
            value = {"last": lambda: recent[-1], "first": lambda: recent[0],
                     "mean": lambda: np.mean(recent), "min": lambda: np.min(recent),
                     "max": lambda: np.max(recent), "rms": lambda: np.sqrt(np.mean(recent ** 2)),
                     "std": lambda: np.std(recent), "delta": lambda: np.ptp(recent)}[mode]()
            self.metric_values[i].setText(f"{value:.{self.value_decimals.value()}f}")
            clipped = bool(np.any(np.abs(raw[-min(len(raw), 1000):, j]) >= self.ranges[i] * 0.9999))
            self.metric_subs[i].setText("输入已到量程边界" if clipped else
                                       f"{self.live_value_mode.currentText()} · 近 {len(recent) / self.engine.sample_rate:.3f} s · {c.unit}")
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
        if self._tick % 5 == 0 and self.pages.currentIndex() == 1:
            self.refresh_analysis()
        if self._tick % 5 == 0 and self.pages.currentIndex() == 3:
            self.refresh_protocol()

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
        measurements = self.live_measurements if source == "live" else self.replay_measurements
        cursor_times = measurements.measurements.get("times")
        epoch = (self._timestamp(self.engine.acquisition_started_at_utc) if self.engine else None) if source == "live" else self._replay_epoch()
        available = self.engine is not None if source == "live" else self.replay is not None
        if available and combo.currentData() == "absolute" and epoch is None:
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self.tell("此输入没有可用的绝对起点，使用相对时间；不会以文件修改时间代替。")
        if source == "live":
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
        units = {c.unit for c in channels}
        unit = next(iter(units)) if len(units) == 1 else "V"
        self.live_plot.setLabel("left", "工程值" if len(units) == 1 else "原始电压", units=unit)
        values = convert_volts(raw, channels) if len(units) == 1 else raw
        epoch = self._timestamp(self.engine.acquisition_started_at_utc)
        x = self._time_axis_values(self.live_plot, times, self.live_time_axis.currentData(), epoch, self.engine.sample_rate)
        for i, curve in enumerate(self.live_curves):
            if i in self.active_channels:
                curve.setData(x, values[:, self.active_channels.index(i)])
            else:
                curve.setData([], [])
        self.live_measurements.set_data(times, values, [(f"AI {c.index} · {c.name}", unit) for c in channels], x_values=x)

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
        self.tell("已将最近最多 1 秒的平均电压设为零点。请在实际实验中确认此时为空载；原始电压未修改。")

    def undo_zero(self):
        if self.engine is None or not self.engine.running or self.engine.recording or self._previous_zero is None:
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
        self.tell("已恢复上次调零前的零点；原始电压未修改。")

    def edit_channels(self):
        if ChannelDialog(self).exec() == W.QDialog.DialogCode.Accepted:
            self._discard_preview()
            self._sync_channel_labels()
            self.tell("通道设置已更新。开始预览后生效。")

    def _discard_preview(self):
        self._previous_zero = None
        self._live_cache = None
        self.live_measurements.set_data([], np.empty((0, 0)), [])
        if self.engine:
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
                "notes": self.recording_notes, "group_number": group_number}

    def _set_recording_options(self, options):
        self.recording_duration_seconds = options.get("duration_seconds")
        self.recording_notes = options.get("notes", "")
        duration = self.recording_duration_seconds
        self.record_settings_info.setText(
            (f"定长 {duration:g} s" if duration is not None else "手动结束") + " · 自动编号"
            + (" · 已填备注" if self.recording_notes else ""))

    def edit_recording_options(self):
        dialog = W.QDialog(self)
        dialog.setWindowTitle("本组记录设置")
        dialog.setMinimumWidth(440)
        layout = W.QVBoxLayout(dialog)
        form = W.QFormLayout()
        duration = W.QDoubleSpinBox()
        duration.setDecimals(3)
        duration.setRange(0, 999999999)
        duration.setSuffix(" s")
        duration.setSpecialValueText("手动结束")
        duration.setValue(self.recording_duration_seconds or 0)
        form.addRow("记录时长（0 为手动）", duration)
        notes = W.QPlainTextEdit(self.recording_notes)
        notes.setMaximumHeight(120)
        form.addRow("本组备注", notes)
        layout.addLayout(form)
        layout.addWidget(label("每组按日期与实验名自动编号。定长按实际采样率换算为整数帧；完成后保存并继续预览。", "muted", True))
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok | W.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == W.QDialog.DialogCode.Accepted:
            self._set_recording_options({"duration_seconds": duration.value() or None,
                                         "notes": notes.toPlainText()})

    def save_profile(self):
        path, _ = W.QFileDialog.getSaveFileName(self, "保存实验配置", str(ROOT / "实验配置.json"), "JSON 配置 (*.json)")
        if path:
            self.save_profile_to(Path(path))

    def save_profile_to(self, path: Path):
        data = {"schema": "labdaq-profile-v1", "channels": [asdict(c) for c in self.channels],
                "active_channels": self.active_channels, "ranges": self.ranges, "coupling": self.coupling,
                "iepe": self.iepe, "sample_rate": self.rate.value(), "scenario": self.scenario.currentData(),
                "experiment": self.experiment.text(), "signal_settings": self.signal_settings.to_dict(),
                "recording_options": self._recording_options(),
                "display": {"live_time_axis": self.live_time_axis.currentData(),
                            "replay_time_axis": self.replay_time_axis.currentData(),
                            "value_mode": self.live_value_mode.currentData(),
                            "value_decimals": self.value_decimals.value()},
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
        options = data.get("recording_options", {})
        duration = options.get("duration_seconds")
        if duration is not None and (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                                     or not math.isfinite(duration) or not 0 < duration <= 999999999):
            raise ValueError("记录时长必须为正的有限秒数，手动结束使用 null")
        if not isinstance(options.get("notes", ""), str):
            raise ValueError("记录备注必须为文字")
        display = data.get("display", {})
        for key, combo, default in (("live_time_axis", self.live_time_axis, "relative"),
                                    ("replay_time_axis", self.replay_time_axis, "relative"),
                                    ("value_mode", self.live_value_mode, "last")):
            if combo.findData(display.get(key, default)) < 0:
                raise ValueError("未知的显示模式")
        decimals = display.get("value_decimals", 3)
        if type(decimals) is not int or not 0 <= decimals <= 9:
            raise ValueError("显示小数位必须是 0 到 9 的整数")
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
        if reader is not None:
            self.input_hpf, self.input_hpf_config = reader, config
        self.ranges, self.coupling, self.iepe = list(config.ranges), list(config.coupling), list(config.iepe)
        self.rate.setValue(config.sample_rate)
        self.hpf_loop.setChecked(data.get("hpf_loop", False))
        self.scenario.setCurrentIndex(self.scenario.findData(mode))
        self._input_changed()
        self.experiment.setText(data["experiment"])
        self._set_recording_options(options)
        self.live_time_axis.setCurrentIndex(self.live_time_axis.findData(display.get("live_time_axis", "relative")))
        self.replay_time_axis.setCurrentIndex(self.replay_time_axis.findData(display.get("replay_time_axis", "relative")))
        self.live_value_mode.setCurrentIndex(self.live_value_mode.findData(display.get("value_mode", "last")))
        self.value_decimals.setValue(decimals)
        self._sync_channel_labels()
        self.tell(f"配置已载入 · {path.name}")

    def refresh_analysis(self, *_):
        if not hasattr(self, "analysis_info"):
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
            if self.engine is None:
                self._clear_analysis()
                return
            _, raw = self.engine.snapshot(seconds=8)
            fs, settings = self.engine.sample_rate, self._selected_settings()
        if len(raw) < 32:
            self._clear_analysis()
            self.analysis_info.setText("等待至少 32 个扫描点。")
            return
        # Fixed upper bound keeps plotting and analysis responsive at high rates.
        original_length = len(raw)
        raw = raw[-65536:]
        if from_replay:
            analysis_start = start + max(0, original_length - len(raw))
            times = self._replay_times(reader, analysis_start, len(raw))
            if np.any(np.diff(times) > 1.5 / fs):
                self._clear_analysis()
                self.analysis_info.setText("当前窗口包含暂停缺口，请选择一个连续段再分析。")
                return
        values = convert_volts(raw, settings)
        indices = [c.index for c in settings]
        selected = self.analysis_channel.currentData()
        reference = self.reference_channel.currentData()
        if selected not in indices:
            return
        idx = indices.index(selected)
        mode = self.analysis_mode.currentIndex()
        try:
            if self.lowpass.isChecked():
                values = filter_window(values, fs, self.cutoff.value())
            if mode == 2:
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
            self.analysis_curve.setPen(pg.mkPen(COLORS[selected], width=1.5))
            self.analysis_curve.setData(f, y)
            signal = values[:, idx]
            origin = "回看窗口" if from_replay else "实时窗口"
            self.analysis_info.setText(f"{origin} · {len(signal):,} 点 · 均值 {np.mean(signal):.4f} · RMS {np.sqrt(np.mean(signal**2)):.4f} · 峰峰值 {np.ptp(signal):.4f} {settings[idx].unit}  |  原始文件保持不变")
        except (ValueError, TypeError) as exc:
            self._clear_analysis()
            self.analysis_info.setText(f"分析设置：{exc}")

    def _clear_analysis(self):
        self.analysis_curve.setData([], [])
        self.coherence_curve.setData([], [])
        self.coherence_plot.hide()

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
            for i, check in enumerate(self.replay_channel_checks):
                channel = next((c for c in reader.channels if c.index == i), None)
                check.blockSignals(True)
                check.setEnabled(channel is not None)
                check.setChecked(channel is not None)
                check.setText(f"AI {i} · {channel.name}" if channel else f"AI {i}")
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
            self.nav.setCurrentRow(2)
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
                curve.setPen(pg.mkPen(COLORS[r.channels[j].index], width=1.5))
                curve.setData(plot_x, plot_values[:, j], connect="finite")
                self.replay_plot.plotItem.legend.addItem(curve, f"AI {r.channels[j].index} · {r.channels[j].name}")
            else:
                curve.setData([], [])
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
        self.nav.setCurrentRow(1)

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
        window.scenario.setCurrentIndex(window.scenario.findData("rust"))
    window.show()
    if args.demo:
        QtCore.QTimer.singleShot(100, window.start_preview)
    return app.exec()
