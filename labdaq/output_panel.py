"""Controls for the physical AO channel; commands are owned by the Rust core."""
import math

import numpy as np
from PySide6 import QtCore, QtWidgets as W
from .signals import SignalSpec, SimulationSettings, generate_signals
from .ui_kit import CollapsibleSection, NoWheelComboBox, field, label


DEFAULT_SETTINGS = dict(waveform="sine", amplitude_v=0.2, offset_v=0.0,
                        frequency_hz=10.0, sample_rate=48000.0)
OUTPUT_RATES = (10000.0, 20000.0, 48000.0, 96000.0)


def preview_waveform(settings):
    """Three ideal periods for display, independent of the physical DAC clock."""
    validate_settings(settings)
    dc = settings["waveform"] == "dc"
    span = 1.0 if dc else 3.0 / settings["frequency_hz"]
    spec = SignalSpec(waveform=settings["waveform"], offset_v=settings["offset_v"],
                      amplitude_v=0.0 if dc else settings["amplitude_v"],
                      frequency_hz=settings["frequency_hz"])
    times = np.linspace(0, span, 1201)
    volts = generate_signals(SimulationSettings(channels=(spec,) * 4), 0, len(times),
                             (len(times) - 1) / span)[:, 0]
    return times, volts


def validate_settings(settings):
    if settings["waveform"] not in ("dc", "sine", "square", "triangle"):
        raise ValueError("未知 AO 波形")
    if not all(isinstance(settings[key], (int, float)) and not isinstance(settings[key], bool)
               and math.isfinite(settings[key]) for key in DEFAULT_SETTINGS if key != "waveform"):
        raise ValueError("AO 参数必须为有限数")
    amplitude, offset = settings["amplitude_v"], settings["offset_v"]
    peak = 0 if settings["waveform"] == "dc" else amplitude
    if not 0 <= amplitude <= 3 or abs(offset) + peak > 3:
        raise ValueError("AO 偏置绝对值 + 峰值幅度不能超过 3 V")
    if settings["sample_rate"] not in OUTPUT_RATES:
        raise ValueError("AO 更新率请选择 10、20、48 或 96 kS/s")
    if settings["waveform"] != "dc" and not 0 < settings["frequency_hz"] < settings["sample_rate"] / 2:
        raise ValueError(f"AO 频率必须大于 0 且小于 {settings['sample_rate'] / 2:g} Hz（更新率的一半）")


class OutputPanel(CollapsibleSection):
    settings_changed = QtCore.Signal()

    def __init__(self, start, stop):
        super().__init__("信号输出 · AO", "wave", expanded=False)
        self.setProperty("outputControls", True)
        self.waveform = W.QComboBox()
        for title, key in (("正弦波", "sine"), ("直流", "dc"), ("方波 · 50%", "square"), ("三角波", "triangle")):
            self.waveform.addItem(title, key)
        self.add(field("输出波形", self.waveform))
        self.controls = {}
        for key, title, low, high, decimals in (
                ("amplitude_v", "峰值幅度 / V", 0, 3, 4),
                ("offset_v", "偏置（直流电平）/ V", -3, 3, 4),
                ("frequency_hz", "频率 / Hz", .001, 47999.999, 3)):
            control = W.QDoubleSpinBox()
            control.setDecimals(decimals)
            control.setRange(low, high)
            control.setKeyboardTracking(False)
            control.setValue(DEFAULT_SETTINGS[key])
            self.controls[key] = control
            self.add(field(title, control))
        self.rate = NoWheelComboBox()
        for rate in OUTPUT_RATES:
            self.rate.addItem(f"{rate / 1000:g} kS/s", rate)
        self.rate.setCurrentIndex(self.rate.findData(DEFAULT_SETTINGS["sample_rate"]))
        self.rate.setToolTip("AO 独立更新率，与 AI 采样率分别设置。")
        self.add(field("AO 更新率", self.rate))
        self.frequency_hint = label("", "muted", True)
        self.add(self.frequency_hint)
        self.voltage_hint = label("", "muted", True)
        self.add(self.voltage_hint)
        self.status = label("未输出 · 请先连接设备", "muted", True)
        self.add(self.status)
        row = W.QHBoxLayout()
        self.start_button = W.QPushButton("开始输出")
        self.start_button.setObjectName("aoStart")
        self.stop_button = W.QPushButton("停止并归零")
        self.start_button.clicked.connect(start)
        self.stop_button.clicked.connect(stop)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        self.add_layout(row)
        for control in (self.waveform, self.rate, *self.controls.values()):
            control.setMinimumHeight(36)
            control.setSizePolicy(W.QSizePolicy.Policy.Expanding, W.QSizePolicy.Policy.Fixed)
        self.waveform.currentIndexChanged.connect(self._waveform_changed)
        self.rate.currentIndexChanged.connect(self._rate_changed)
        for control in self.controls.values():
            control.valueChanged.connect(self._check_limits)
        self._can_start = False
        self._rate_changed()
        self.update_state(False, {})

    def settings(self):
        settings = dict(waveform=self.waveform.currentData(), sample_rate=self.rate.currentData(),
                        **{key: control.value() for key, control in self.controls.items()})
        validate_settings(settings)
        return settings

    def set_settings(self, settings):
        validate_settings(settings)
        self.rate.setCurrentIndex(self.rate.findData(settings["sample_rate"]))
        self.waveform.setCurrentIndex(self.waveform.findData(settings["waveform"]))
        for key, control in self.controls.items():
            control.setValue(settings[key])

    def _waveform_changed(self):
        periodic = self.waveform.currentData() != "dc" and self.waveform.isEnabled()
        for key in ("amplitude_v", "frequency_hz"):
            self.controls[key].setEnabled(periodic)
        self._check_limits()

    def _rate_changed(self):
        limit = self.rate.currentData() / 2
        self.controls["frequency_hz"].setMaximum(limit - .001)
        self.frequency_hint.setText(f"周期波形频率上限：< {limit:,.0f} Hz（AO 更新率的一半）；直流不适用。")
        self._check_limits()

    def _check_limits(self):
        amplitude = self.controls["amplitude_v"].value() if self.waveform.currentData() != "dc" else 0
        offset = self.controls["offset_v"].value()
        try:
            self.settings()
            valid = True
            text = f"输出电压：{offset - amplitude:g} ～ {offset + amplitude:g} V；|偏置| + 峰值幅度 ≤ 3 V。"
        except ValueError as exc:
            valid, text = False, str(exc)
        self.voltage_hint.setText(text)
        self.start_button.setEnabled(self._can_start and valid)
        self.settings_changed.emit()

    def update_state(self, ready, state, unknown=False):
        running = state.get("running", False)
        if running and state.get("settings"):
            self.set_settings(state["settings"])
        self._can_start = ready and not running and not unknown
        active = running and not unknown
        if self.start_button.property("active") != active:
            self.start_button.setProperty("active", active)
            self.start_button.style().unpolish(self.start_button)
            self.start_button.style().polish(self.start_button)
        self.start_button.setText("状态未知" if unknown else "正在输出" if active else "开始输出")
        self.stop_button.setEnabled(ready and not unknown)
        self.waveform.setEnabled(not running and not unknown)
        self.rate.setEnabled(not running and not unknown)
        for control in self.controls.values():
            control.setEnabled(not running and not unknown)
        self._waveform_changed()
        if unknown:
            text = "核心连接中断 · AO 状态未知"
        elif running:
            settings = state["settings"]
            text = f"输出中 · {state['actual_rate']:,.3f} S/s"
            text += f" · {settings['offset_v']:g} V" if settings["waveform"] == "dc" else f" · {settings['frequency_hz']:g} Hz"
        elif state.get("zeroed"):
            text = "已停止 · 已下发 0 V"
        elif state.get("settings"):
            text = "输出已停止 · 归零未确认"
        else:
            text = "设备已连接 · AO 独立输出" if ready else "未输出 · 请先连接设备"
        self.status.setText(text)
