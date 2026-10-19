"""Sample-based measurements for a displayed time waveform."""
from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets as W


class PlotMeasurements(W.QWidget):
    """Two movable cursors whose readings always come from real samples.

    ``times`` contains physical relative seconds; ``x_values`` may instead
    contain the displayed epoch seconds or sample numbers. NaN separator rows
    are discarded. Equal-distance and duplicate x positions use the first row.
    """

    def __init__(self, plot: pg.PlotWidget, parent: W.QWidget | None = None):
        super().__init__(parent)
        self.plot = plot
        self.enabled_checkbox = W.QCheckBox("测量游标")
        self.channel_combo = W.QComboBox()
        self.channel_combo.setMinimumContentsLength(12)
        self.channel_combo.setEnabled(False)
        self.readout = W.QLabel()
        self.readout.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.readout.setWordWrap(True)
        self.readout.setToolTip("A、B 的时间与幅值取最近真实样本；间隔倒数仅为 1/|Δt|，不代表信号基频。")
        self.readout.hide()
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = W.QHBoxLayout()
        controls.addWidget(self.enabled_checkbox)
        controls.addWidget(self.channel_combo)
        controls.addStretch()
        layout.addLayout(controls)
        layout.addWidget(self.readout)
        self.cursor_lines = tuple(
            pg.InfiniteLine(angle=90, movable=True, label=name,
                            pen=pg.mkPen(color, width=1.5),
                            hoverPen=pg.mkPen(color, width=2.5),
                            labelOpts={"position": .96, "color": color})
            for name, color in (("A", "#CA7B1A"), ("B", "#7954BE"))
        )
        for line in self.cursor_lines:
            line.setZValue(30)
            plot.addItem(line, ignoreBounds=True)
            line.hide()
            line.sigPositionChanged.connect(self._update_measurements)
        self._times = np.empty(0)
        self._x_values = np.empty(0)
        self._values = np.empty((0, 0))
        self._sample_indices = np.empty(0, dtype=np.int64)
        self._channels: list[tuple[str, str]] = []
        self._positioned = False
        self.measurements: dict = {}
        self.enabled_checkbox.toggled.connect(self._toggle)
        self.channel_combo.currentIndexChanged.connect(self._update_measurements)

    def set_data(self, times, values, channels, *, x_values=None):
        """Set displayed rows and ``[(channel label, unit), ...]`` metadata.

        Arrays have shapes (n,), (n, c), and (n,). Only rows with finite
        physical time and plotting x are samples; channel NaNs stay missing.
        Positions reset after the entire data window moves beyond both cursors.
        """
        times = np.asarray(times, dtype=float)
        values = np.asarray(values, dtype=float)
        x_values = times if x_values is None else np.asarray(x_values, dtype=float)
        keep = np.isfinite(times) & np.isfinite(x_values)
        self._times = times[keep]
        self._x_values = x_values[keep]
        self._values = values[keep]
        self._sample_indices = np.flatnonzero(keep)
        channels = [(str(label), str(unit)) for label, unit in channels]
        if channels != self._channels:
            previous = self.channel_combo.currentIndex()
            selected = self._channels[previous] if previous >= 0 else None
            self._channels = channels
            with QtCore.QSignalBlocker(self.channel_combo):
                self.channel_combo.clear()
                self.channel_combo.addItems([label for label, _ in channels])
                if selected in channels:
                    self.channel_combo.setCurrentIndex(channels.index(selected))
        self.channel_combo.setEnabled(self.enabled_checkbox.isChecked() and bool(channels))
        if len(self._times):
            low, high = float(np.min(self._x_values)), float(np.max(self._x_values))
            positions = [line.value() for line in self.cursor_lines]
            if not self._positioned or all(position < low for position in positions) or all(position > high for position in positions):
                self._reset_positions(low, high)
        self._update_measurements()

    def _reset_positions(self, low, high):
        for line, fraction in zip(self.cursor_lines, (.2, .8)):
            with QtCore.QSignalBlocker(line):
                line.setValue(low + (high - low) * fraction)
        self._positioned = True

    def _toggle(self, enabled):
        for line in self.cursor_lines:
            line.setVisible(enabled)
        self.readout.setVisible(enabled)
        self.channel_combo.setEnabled(enabled and bool(self._channels))
        self._update_measurements()

    def _update_measurements(self, *_):
        channel = self.channel_combo.currentIndex()
        if not self.enabled_checkbox.isChecked() or not len(self._times) or channel < 0:
            self.measurements = {}
            self.readout.setText("当前窗口无有效样本。")
            return
        indices = [int(np.argmin(np.abs(self._x_values - line.value()))) for line in self.cursor_lines]
        t1, t2 = (float(self._times[index]) for index in indices)
        y1, y2 = (float(self._values[index, channel]) for index in indices)
        delta_t = t2 - t1
        delta_y = y2 - y1 if math.isfinite(y1) and math.isfinite(y2) else None
        in_interval = (self._times >= min(t1, t2)) & (self._times <= max(t1, t2))
        interval = self._values[in_interval, channel]
        interval = interval[np.isfinite(interval)]
        peak_to_peak = float(np.ptp(interval)) if len(interval) else None
        reciprocal_hz = 1 / abs(delta_t) if delta_t else None
        self.measurements = {
            "indices": tuple(int(self._sample_indices[index]) for index in indices),
            "times": (t1, t2), "y": (y1, y2), "delta_t": delta_t,
            "delta_y": delta_y, "peak_to_peak": peak_to_peak,
            "reciprocal_hz": reciprocal_hz,
        }
        unit = self._channels[channel][1]

        def value_text(value, suffix):
            return f"{value:.9g} {suffix}".rstrip() if value is not None and math.isfinite(value) else "无有效数据"

        reciprocal = value_text(reciprocal_hz, "Hz") if delta_t else "—（同一采样时刻）"
        self.readout.setText(
            f"A：t = {t1:.9g} s，y = {value_text(y1, unit)}；"
            f"B：t = {t2:.9g} s，y = {value_text(y2, unit)}\n"
            f"Δt = {delta_t:.9g} s；Δy = {value_text(delta_y, unit)}；"
            f"区间峰峰值 = {value_text(peak_to_peak, unit)}；"
            f"间隔倒数 1/|Δt| = {reciprocal}"
        )
