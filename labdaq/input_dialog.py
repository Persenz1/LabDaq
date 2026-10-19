"""Parameter editor for offline voltage synthesis."""
from PySide6 import QtCore, QtWidgets as W

from . import theme, ui_kit
from .signals import SignalSpec, SimulationSettings


WAVEFORMS = [
    ("直流", "dc"), ("正弦", "sine"), ("方波", "square"),
    ("三角波", "triangle"), ("锯齿波", "sawtooth"), ("阶跃", "step"),
    ("单次脉冲", "pulse"), ("线性扫频", "chirp"), ("白噪声", "noise"),
]

DIALOG_MARGINS = (22, 20, 22, 20)


def _dialog_layout(dialog: W.QDialog, spacing: int = 13) -> W.QVBoxLayout:
    layout = W.QVBoxLayout(dialog)
    layout.setContentsMargins(*DIALOG_MARGINS)
    layout.setSpacing(spacing)
    return layout


def _header(title: str, subtitle: str, icon_name: str | None = None) -> W.QWidget:
    p = theme.current()
    holder = W.QWidget()
    holder.setObjectName("transparentHost")
    row = W.QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)
    if icon_name:
        mark = W.QLabel()
        mark.setFixedSize(40, 40)
        mark.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(theme.icon(icon_name, p.accent, 20).pixmap(20, 20))
        mark.setStyleSheet(
            f"background: {p.accent_soft}; border: 1px solid {p.border};"
            f" border-radius: {p.radius_small}px;")
        row.addWidget(mark)
    texts = W.QVBoxLayout()
    texts.setContentsMargins(0, 0, 0, 0)
    texts.setSpacing(2)
    texts.addWidget(ui_kit.label(title, "pageTitle"))
    texts.addWidget(ui_kit.label(subtitle, "pageSubtitle", True))
    row.addLayout(texts, 1)
    return holder


def _notice(text: str = "", tone: str = "neutral") -> W.QLabel:
    bar = ui_kit.label(text, "notice", True)
    bar.setProperty("tone", tone)
    bar.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    return bar


def _fit_to_screen(dialog: W.QDialog, width: int, height: int) -> None:
    """Keep the tall parameter editor inside a 1366×768 laptop workspace."""
    screen = dialog.screen() or W.QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, max(640, available.width() - 80))
        height = min(height, max(560, available.height() - 60))
    dialog.setMinimumSize(640, 560)
    dialog.resize(width, height)


class SignalDialog(W.QDialog):
    def __init__(self, settings: SimulationSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("电信号模拟参数")
        _fit_to_screen(self, 760, 780)
        self.settings = settings
        layout = _dialog_layout(self)

        layout.addWidget(_header(
            "电信号模拟参数",
            "按 AI 通道设置波形与电压参数；修改后点击“应用”生效。",
            "wave"))
        layout.addWidget(_notice(
            "所有参数均为数采端的电压。正弦等周期波使用峰值幅度；白噪声使用 RMS，忽略幅度栏。"
            "同频通道可用相位设置相互关系；超过采样奈奎斯特频率会产生混叠。", "info"))

        self.controls = []
        self.tabs = W.QTabWidget()
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
            page.setObjectName("transparentHost")
            page_layout = W.QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            scroll = W.QScrollArea()
            scroll.setObjectName("panelScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(W.QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            body = W.QWidget()
            body.setObjectName("transparentHost")
            form = W.QFormLayout(body)
            form.setContentsMargins(18, 14, 18, 16)
            form.setHorizontalSpacing(18)
            form.setVerticalSpacing(8)
            form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            form.setFieldGrowthPolicy(W.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

            controls = {}
            waveform = W.QComboBox()
            for title, key in WAVEFORMS:
                waveform.addItem(title, key)
            waveform.setCurrentIndex(waveform.findData(spec.waveform))
            waveform.setMinimumWidth(190)
            controls["waveform"] = waveform
            form.addRow(ui_kit.field_label("波形"), waveform)
            for key, title, low, high in fields:
                control = W.QDoubleSpinBox()
                control.setDecimals(6)
                control.setRange(low, high)
                control.setValue(getattr(spec, key))
                control.setMinimumWidth(220)
                control.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
                controls[key] = control
                form.addRow(ui_kit.field_label(title), control)
            self.controls.append(controls)
            scroll.setWidget(body)
            page_layout.addWidget(scroll)
            self.tabs.addTab(page, f"AI {index}")
        layout.addWidget(self.tabs, 1)

        options_card, options_layout = ui_kit.card(margins=(16, 12, 16, 13), spacing=9)
        options_layout.addWidget(ui_kit.section_title("生成选项", "sliders"))
        seed_row = W.QHBoxLayout()
        seed_row.setSpacing(18)
        self.seed = W.QSpinBox()
        self.seed.setRange(0, 2147483647)
        self.seed.setValue(settings.seed)
        self.seed.setMinimumWidth(180)
        self.seed.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        seed_row.addWidget(ui_kit.field("随机种子（重复实验使用相同值）", self.seed), 0,
                          QtCore.Qt.AlignmentFlag.AlignBottom)
        self.as_voltage = W.QCheckBox("按电压显示：将启用通道的单位设置为 V")
        self.as_voltage.setChecked(True)
        seed_row.addWidget(self.as_voltage, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        seed_row.addStretch(1)
        options_layout.addLayout(seed_row)
        options_layout.addWidget(ui_kit.label(
            "阶跃在开始时间后持续；脉冲和扫频在指定时窗内有效。偏置、噪声和漂移始终叠加。"
            "超出 ±1/±10 V 输入量程时按 ADC 边界削顶；此处不模拟真实传感器供电或完整模拟滤波。",
            "faint", True))
        layout.addWidget(options_card)

        layout.addWidget(theme.divider())
        p = theme.current()
        footer = W.QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        cancel = W.QPushButton("取消")
        cancel.setObjectName("ghost")
        footer.addWidget(cancel)
        apply_button = W.QPushButton("应用")
        apply_button.setObjectName("primary")
        apply_button.setIcon(theme.icon("check", p.accent_text, 15))
        apply_button.setIconSize(QtCore.QSize(15, 15))
        footer.addWidget(apply_button)
        layout.addLayout(footer)
        cancel.clicked.connect(self.reject)
        apply_button.clicked.connect(self.apply)

    def apply(self):
        try:
            channels = tuple(SignalSpec(**{key: widget.currentData() if key == "waveform" else widget.value()
                                          for key, widget in controls.items()}) for controls in self.controls)
            self.settings = SimulationSettings(channels=channels, seed=self.seed.value())
        except ValueError as exc:
            W.QMessageBox.information(self, "模拟参数", str(exc))
            return
        self.accept()
