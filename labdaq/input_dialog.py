"""Parameter editor for offline voltage synthesis."""
from PySide6 import QtWidgets as W

from .signals import SignalSpec, SimulationSettings


WAVEFORMS = [
    ("直流", "dc"), ("正弦", "sine"), ("方波", "square"),
    ("三角波", "triangle"), ("锯齿波", "sawtooth"), ("阶跃", "step"),
    ("单次脉冲", "pulse"), ("线性扫频", "chirp"), ("白噪声", "noise"),
]


class SignalDialog(W.QDialog):
    def __init__(self, settings: SimulationSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("电信号模拟参数")
        self.resize(660, 720)
        self.settings = settings
        layout = W.QVBoxLayout(self)
        note = W.QLabel("所有参数均为数采端的电压。正弦等周期波使用峰值幅度；白噪声使用 RMS，忽略幅度栏。\n"
                        "同频通道可用相位设置相互关系；超过采样奈奎斯特频率会产生混叠。")
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = W.QTabWidget()
        layout.addWidget(tabs)
        self.controls = []
        fields = [
            ("amplitude_v", "幅度 / V（可为负）", -10000, 10000),
            ("offset_v", "直流偏置 / V", -10000, 10000),
            ("frequency_hz", "频率 / Hz", 0, 10000000),
            ("phase_deg", "相位 / °", -36000, 36000),
            ("noise_rms_v", "叠加噪声 RMS / V", 0, 10000),
            ("drift_v_per_s", "线性漂移 / V/s", -10000, 10000),
            ("duty_cycle", "方波占空比（0–1）", 0, 1),
            ("start_s", "阶跃/脉冲/扫频开始 / s", 0, 1000000),
            ("duration_s", "脉冲/扫频时长 / s", 0.000001, 1000000),
            ("end_frequency_hz", "扫频结束频率 / Hz", 0, 10000000),
        ]
        for index, spec in enumerate(settings.channels):
            page = W.QWidget()
            form = W.QFormLayout(page)
            controls = {}
            waveform = W.QComboBox()
            for title, key in WAVEFORMS:
                waveform.addItem(title, key)
            waveform.setCurrentIndex(waveform.findData(spec.waveform))
            controls["waveform"] = waveform
            form.addRow("波形", waveform)
            for key, title, low, high in fields:
                control = W.QDoubleSpinBox()
                control.setDecimals(6)
                control.setRange(low, high)
                control.setValue(getattr(spec, key))
                controls[key] = control
                form.addRow(title, control)
            self.controls.append(controls)
            tabs.addTab(page, f"AI {index}")
        seed_row = W.QFormLayout()
        self.seed = W.QSpinBox()
        self.seed.setRange(0, 2147483647)
        self.seed.setValue(settings.seed)
        seed_row.addRow("随机种子（重复实验使用相同值）", self.seed)
        layout.addLayout(seed_row)
        self.as_voltage = W.QCheckBox("按电压显示：将启用通道的单位设置为 V")
        self.as_voltage.setChecked(True)
        layout.addWidget(self.as_voltage)
        extra = W.QLabel("阶跃在开始时间后持续；脉冲和扫频在指定时窗内有效。偏置、噪声和漂移始终叠加。\n"
                         "超出 ±1/±10 V 输入量程时按 ADC 边界削顶；此处不模拟真实传感器供电或完整模拟滤波。")
        extra.setWordWrap(True)
        layout.addWidget(extra)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok | W.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self):
        try:
            channels = tuple(SignalSpec(**{key: widget.currentData() if key == "waveform" else widget.value()
                                          for key, widget in controls.items()}) for controls in self.controls)
            self.settings = SimulationSettings(channels=channels, seed=self.seed.value())
        except ValueError as exc:
            W.QMessageBox.information(self, "模拟参数", str(exc))
            return
        self.accept()
