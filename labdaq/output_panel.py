"""Controls for the physical AO channel; commands are owned by the Rust core."""
import math

from PySide6 import QtWidgets as W
from .ui_kit import CollapsibleSection, field, label


DEFAULT_SETTINGS = dict(waveform="sine", amplitude_v=0.2, offset_v=0.0,
                        frequency_hz=10.0, sample_rate=48000.0)


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
    if not 10000 <= settings["sample_rate"] <= 96000:
        raise ValueError("AO 更新率需在 10000–96000 S/s 之间")
    if not 0 < settings["frequency_hz"] <= 47999:
        raise ValueError("AO 频率需在 0–47999 Hz 之间（不含 0）")
    if settings["waveform"] != "dc" and settings["frequency_hz"] >= settings["sample_rate"] / 2:
        raise ValueError("AO 频率必须小于更新率的一半")


class OutputPanel(CollapsibleSection):
    def __init__(self, start, stop):
        super().__init__("信号输出 · AO", "wave", expanded=False)
        self.waveform = W.QComboBox()
        for title, key in (("正弦波", "sine"), ("直流", "dc"), ("方波 · 50%", "square"), ("三角波", "triangle")):
            self.waveform.addItem(title, key)
        self.add(field("输出波形", self.waveform))
        self.controls = {}
        for key, title, low, high, decimals in (
                ("amplitude_v", "峰值幅度 / V", 0, 3, 4),
                ("offset_v", "偏置（直流电平）/ V", -3, 3, 4),
                ("frequency_hz", "频率 / Hz", .001, 47999, 3),
                ("sample_rate", "AO 更新率 / S/s", 10000, 96000, 3)):
            control = W.QDoubleSpinBox()
            control.setDecimals(decimals)
            control.setRange(low, high)
            control.setValue(DEFAULT_SETTINGS[key])
            self.controls[key] = control
            self.add(field(title, control))
        self.status = label("未输出 · 请先连接设备", "muted", True)
        self.add(self.status)
        row = W.QHBoxLayout()
        self.start_button = W.QPushButton("开始输出")
        self.stop_button = W.QPushButton("停止并归零")
        self.start_button.clicked.connect(start)
        self.stop_button.clicked.connect(stop)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        self.add_layout(row)
        self.add(label("输出范围 ±3 V；峰峰值为峰值幅度的 2 倍。回接 AI0 时关闭 IEPE。"
                       "AI 与 AO 独立启停；关闭窗口后核心继续输出。", "faint", True))
        self.waveform.currentIndexChanged.connect(self._waveform_changed)
        self.update_state(False, {})

    def settings(self):
        settings = dict(waveform=self.waveform.currentData(),
                        **{key: control.value() for key, control in self.controls.items()})
        validate_settings(settings)
        return settings

    def set_settings(self, settings):
        validate_settings(settings)
        self.waveform.setCurrentIndex(self.waveform.findData(settings["waveform"]))
        for key, control in self.controls.items():
            control.setValue(settings[key])

    def _waveform_changed(self):
        periodic = self.waveform.currentData() != "dc" and self.waveform.isEnabled()
        for key in ("amplitude_v", "frequency_hz"):
            self.controls[key].setEnabled(periodic)

    def update_state(self, ready, state, unknown=False):
        running = state.get("running", False)
        self.start_button.setEnabled(ready and not running and not unknown)
        self.stop_button.setEnabled(ready and not unknown)
        self.waveform.setEnabled(not running and not unknown)
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
