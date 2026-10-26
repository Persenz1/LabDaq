"""Sample-based measurements for a displayed time waveform."""
from __future__ import annotations

import math
import weakref

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets as W

from . import theme
from .measurement_cursor import MeasurementCursor


class PlotMeasurements(W.QWidget):
    """Two movable cursors whose readings always come from real samples.

    ``times`` contains physical relative seconds; ``x_values`` may instead
    contain the displayed epoch seconds or sample numbers. NaN separator rows
    are discarded. Equal-distance and duplicate x positions use the first row.
    """

    def __init__(self, plot: pg.PlotWidget, parent: W.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("transparentHost")
        p = theme.current()
        self.plot = plot
        self.enabled_checkbox = W.QCheckBox("测量游标")
        self.channel_combo = W.QComboBox()
        self.channel_combo.setMinimumContentsLength(12)
        self.channel_combo.setEnabled(False)
        self.reset_button = W.QPushButton("游标回到窗口")
        self.reset_button.setEnabled(False)
        self.reset_button.clicked.connect(self._reset_to_view)
        self.readout = W.QLabel("左右移动游标，上下调整 A / B 标签。")
        self.readout.setStyleSheet(f"color: {p.text_muted}; font-size: 11px;")
        self.readout.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.readout.setWordWrap(True)
        self.readout.setToolTip("左右拖动标线或字母移动游标；上下拖动字母调整标签位置。"
                                "也可输入相对时间，读数取最近真实采样点。")
        self.readout.hide()
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        controls = W.QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self.enabled_checkbox)
        controls.addWidget(self.channel_combo)
        controls.addStretch()
        controls.addWidget(self.reset_button)
        layout.addLayout(controls)
        self.readings_panel = W.QWidget()
        self.readings_panel.setObjectName("transparentHost")
        grid = W.QGridLayout(self.readings_panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        self.time_inputs = []
        self.value_labels = {}
        self.unit_labels = {}
        for index, (name, color) in enumerate(zip(("A", "B"), p.channels[2:4])):
            frame = W.QFrame()
            frame.setObjectName("cursorReading")
            frame.setStyleSheet(
                f"QFrame#cursorReading {{ background: {theme.blend(p.surface, color, .06)}; "
                f"border: 1px solid {theme.blend(p.surface, color, .28)}; border-radius: 8px; }}")
            row = W.QHBoxLayout(frame)
            row.setContentsMargins(10, 8, 10, 8)
            row.setSpacing(10)
            badge = W.QLabel(name)
            badge.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: 700;")
            row.addWidget(badge)
            timing = W.QVBoxLayout()
            timing.setSpacing(3)
            title = W.QLabel("相对时间")
            title.setStyleSheet(f"color: {p.text_muted}; font-size: 11px;")
            timing.addWidget(title)
            spin = W.QDoubleSpinBox()
            spin.setDecimals(9)
            spin.setRange(0, 0)
            spin.setSpecialValueText("—")
            spin.setSuffix(" s")
            spin.setKeyboardTracking(False)
            spin.setStyleSheet("QDoubleSpinBox { padding: 3px 20px 3px 6px; min-height: 22px; font-size: 12px; }")
            spin.setToolTip("输入相对记录起点的时间；自动定位到最近真实采样点。")
            spin.valueChanged.connect(lambda value, cursor=index: self._time_edited(cursor, value))
            timing.addWidget(spin)
            self.time_inputs.append(spin)
            row.addLayout(timing, 1)
            amplitude = W.QVBoxLayout()
            amplitude.setSpacing(3)
            title = W.QLabel("幅值")
            title.setStyleSheet(f"color: {p.text_muted}; font-size: 11px;")
            amplitude.addWidget(title)
            values = W.QHBoxLayout()
            values.setSpacing(5)
            value = W.QLabel("—")
            value.setMinimumWidth(0)
            value.setStyleSheet(f"color: {p.text}; font-size: 17px; font-weight: 600;")
            value.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            unit = W.QLabel()
            unit.setStyleSheet(f"color: {p.text_muted}; font-size: 11px;")
            values.addWidget(value)
            values.addWidget(unit)
            values.addStretch()
            amplitude.addLayout(values)
            row.addLayout(amplitude, 1)
            self.value_labels[name] = value
            self.unit_labels[name] = unit
            grid.addWidget(frame, 0, index * 2, 1, 2)
        for column, (key, title, tip) in enumerate((
            ("delta_t", "时间差 Δt", "B − A，按真实采样时间计算。"),
            ("delta_y", "幅值差 Δy", "B − A，保留正负方向。"),
            ("peak_to_peak", "区间峰峰值", "A、B 之间真实样本的最大值 − 最小值。"),
            ("reciprocal_hz", "间隔倒数 1/|Δt|", "仅为游标时间间隔的倒数，不代表信号基频。"),
        )):
            frame = W.QFrame()
            frame.setObjectName("intervalReading")
            frame.setStyleSheet(
                f"QFrame#intervalReading {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 8px; }}")
            frame.setToolTip(tip)
            column_layout = W.QVBoxLayout(frame)
            column_layout.setContentsMargins(10, 7, 10, 7)
            column_layout.setSpacing(4)
            heading = W.QLabel(title)
            heading.setStyleSheet(f"color: {p.text_muted}; font-size: 11px;")
            column_layout.addWidget(heading)
            row = W.QHBoxLayout()
            row.setSpacing(5)
            value = W.QLabel("—")
            value.setMinimumWidth(0)
            value.setStyleSheet(f"color: {p.text}; font-size: 17px; font-weight: 600;")
            value.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            unit = W.QLabel()
            unit.setStyleSheet(f"color: {p.text_muted}; font-size: 11px;")
            row.addWidget(value)
            row.addWidget(unit)
            row.addStretch()
            column_layout.addLayout(row)
            self.value_labels[key] = value
            self.unit_labels[key] = unit
            grid.addWidget(frame, 1, column)
            grid.setColumnStretch(column, 1)
        self.readings_panel.hide()
        layout.addWidget(self.readings_panel)
        layout.addWidget(self.readout)
        self.cursor_lines = self._make_cursor_lines(p)
        for index, line in enumerate(self.cursor_lines):
            line.setZValue(30)
            plot.addItem(line, ignoreBounds=True)
            line.hide()
            self._connect_cursor(line, index)
        self._times = np.empty(0)
        self._x_values = np.empty(0)
        self._values = np.empty((0, 0))
        self._sample_indices = np.empty(0, dtype=np.int64)
        self._channels: list[tuple[str, str]] = []
        self._channel_keys: list = []
        self._series: list[dict] = []
        self._series_mode = False
        self._positioned = False
        self._syncing_cursors = False
        self.linked_lines: list[pg.InfiniteLine] = []
        self._linked_cursor_lines: list[tuple[pg.InfiniteLine, pg.InfiniteLine]] = []
        self.measurements: dict = {}
        self.enabled_checkbox.toggled.connect(self._toggle)
        self.channel_combo.currentIndexChanged.connect(self._channel_changed)

    @staticmethod
    def _make_cursor_lines(p):
        cursor_colors = (p.channels[2 % len(p.channels)], p.channels[3 % len(p.channels)])
        return tuple(
            MeasurementCursor(name, color)
            for name, color in zip(("A", "B"), cursor_colors)
        )

    def _connect_cursor(self, line, index):
        measurement_ref = weakref.ref(self)

        def changed(source):
            measurement = measurement_ref()
            if measurement is not None:
                measurement._cursor_changed(index, source)

        line.sigPositionChanged.connect(changed)

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
        self._series_mode = False
        self._series = []
        self._times = times[keep]
        self._x_values = x_values[keep]
        self._values = values[keep]
        self._sample_indices = np.flatnonzero(keep)
        channels = [(str(label), str(unit)) for label, unit in channels]
        self._set_channels(channels, channels)
        self._position_cursors()
        self._update_measurements()

    def set_series(self, series):
        """Set independent one-dimensional series with stable selection keys."""
        prepared = []
        for item in series:
            times = np.asarray(item["times"], dtype=float)
            x_values = np.asarray(item["x_values"], dtype=float)
            values = np.asarray(item["values"], dtype=float)
            if times.ndim != 1 or x_values.ndim != 1 or values.ndim != 1:
                raise ValueError("series times, x_values and values must be one-dimensional")
            if not len(times) == len(x_values) == len(values):
                raise ValueError("series times, x_values and values must have equal lengths")
            keep = np.isfinite(times) & np.isfinite(x_values) & np.isfinite(values)
            prepared.append({
                "key": item["key"], "label": str(item["label"]), "unit": str(item["unit"]),
                "times": times[keep], "x_values": x_values[keep], "values": values[keep],
                "sample_indices": np.flatnonzero(keep),
            })
        self._series_mode = True
        self._series = prepared
        self._set_channels(
            [(item["label"], item["unit"]) for item in prepared],
            [item["key"] for item in prepared],
        )
        self._activate_series()
        self._position_cursors()
        self._update_measurements()

    def _set_channels(self, channels, keys):
        if channels == self._channels and keys == self._channel_keys:
            return
        previous = self.channel_combo.currentIndex()
        selected = self._channel_keys[previous] if 0 <= previous < len(self._channel_keys) else None
        self._channels = channels
        self._channel_keys = keys
        with QtCore.QSignalBlocker(self.channel_combo):
            self.channel_combo.clear()
            self.channel_combo.addItems([label for label, _ in channels])
            if selected in keys:
                self.channel_combo.setCurrentIndex(keys.index(selected))
        self.channel_combo.setEnabled(self.enabled_checkbox.isChecked() and bool(channels))

    def _activate_series(self):
        if not self._series_mode:
            return
        channel = self.channel_combo.currentIndex()
        if not 0 <= channel < len(self._series):
            self._times = np.empty(0)
            self._x_values = np.empty(0)
            self._values = np.empty((0, 0))
            self._sample_indices = np.empty(0, dtype=np.int64)
            return
        item = self._series[channel]
        self._times = item["times"]
        self._x_values = item["x_values"]
        self._values = item["values"][:, None]
        self._sample_indices = item["sample_indices"]

    def _channel_changed(self, *_):
        self._activate_series()
        self._position_cursors()
        self._update_measurements()

    def _position_cursors(self):
        self.channel_combo.setEnabled(self.enabled_checkbox.isChecked() and bool(self._channels))
        if len(self._times):
            steps = np.diff(self._times)
            steps = steps[steps > 0]
            for spin in self.time_inputs:
                with QtCore.QSignalBlocker(spin):
                    spin.setSpecialValueText("")
                    spin.setRange(float(np.min(self._times)), float(np.max(self._times)))
                    spin.setSingleStep(float(np.min(steps)) if len(steps) else .001)
            low, high = float(np.min(self._x_values)), float(np.max(self._x_values))
            positions = [line.value() for line in self.cursor_lines]
            for lines in self._cursor_pairs():
                for line in lines:
                    with QtCore.QSignalBlocker(line):
                        line.setBounds((low, high))
            if not self._positioned or all(position < low for position in positions) or all(position > high for position in positions):
                self._reset_positions(low, high)

    def link_plot(self, plot: pg.PlotWidget):
        """Add a synchronized A/B cursor pair to another plot."""
        lines = self._make_cursor_lines(theme.current())
        for index, line in enumerate(lines):
            line.setZValue(30)
            plot.addItem(line, ignoreBounds=True)
            with QtCore.QSignalBlocker(line):
                line.setBounds(self.cursor_lines[index].bounds())
                line.setValue(self.cursor_lines[index].value())
            line.setVisible(self.enabled_checkbox.isChecked() and bool(len(self._times)))
            self._connect_cursor(line, index)
        self._linked_cursor_lines.append(lines)
        self.linked_lines.extend(lines)
        measurement_ref = weakref.ref(self)

        def discard(*_):
            measurement = measurement_ref()
            if measurement is not None:
                measurement._discard_linked_lines(lines)

        plot.destroyed.connect(discard)
        return lines

    def _discard_linked_lines(self, lines):
        self._linked_cursor_lines = [item for item in self._linked_cursor_lines if item is not lines]
        self.linked_lines = [line for line in self.linked_lines if all(line is not item for item in lines)]

    def _cursor_pairs(self):
        return (self.cursor_lines, *self._linked_cursor_lines)

    def _reset_positions(self, low, high):
        for lines in self._cursor_pairs():
            for line, fraction in zip(lines, (.2, .8)):
                with QtCore.QSignalBlocker(line):
                    line.setValue(low + (high - low) * fraction)
        self._positioned = True

    def _cursor_changed(self, index, source):
        if self._syncing_cursors:
            return
        position = source.value()
        self._syncing_cursors = True
        try:
            for lines in self._cursor_pairs():
                with QtCore.QSignalBlocker(lines[index]):
                    lines[index].setValue(position)
        finally:
            self._syncing_cursors = False
        self._update_measurements()

    def _toggle(self, enabled):
        for lines in self._cursor_pairs():
            for line in lines:
                line.setVisible(enabled)
        self.readout.setVisible(enabled)
        self.readings_panel.setVisible(enabled)
        self.channel_combo.setEnabled(enabled and bool(self._channels))
        self._update_measurements()

    def _time_edited(self, cursor, seconds):
        if not len(self._times):
            return
        sample = int(np.argmin(np.abs(self._times - seconds)))
        self.cursor_lines[cursor].setValue(float(self._x_values[sample]))
        self._update_measurements()

    def _reset_to_view(self):
        if not len(self._x_values):
            return
        left, right = self.plot.viewRange()[0]
        low = max(left, float(np.min(self._x_values)))
        high = min(right, float(np.max(self._x_values)))
        if low <= high:
            self._reset_positions(low, high)
            self._update_measurements()

    def _set_reading(self, key, value, unit, *, empty="无有效数据"):
        valid = value is not None and math.isfinite(value)
        self.value_labels[key].setText(f"{value:.9g}" if valid else "—")
        self.unit_labels[key].setText(unit)
        self.value_labels[key].setToolTip(f"{value:.12g} {unit}".rstrip() if valid else empty)

    def _update_measurements(self, *_):
        channel = self.channel_combo.currentIndex()
        ready = self.enabled_checkbox.isChecked() and bool(len(self._times)) and channel >= 0
        self.reset_button.setEnabled(ready)
        for lines in self._cursor_pairs():
            for line in lines:
                line.setVisible(ready)
        for spin in self.time_inputs:
            spin.setEnabled(ready)
        if not ready:
            self.measurements = {}
            self.readout.setText("当前窗口无有效样本。")
            if not len(self._times) or channel < 0:
                for spin in self.time_inputs:
                    with QtCore.QSignalBlocker(spin):
                        spin.setSpecialValueText("—")
                        spin.setRange(0, 0)
                        spin.setValue(0)
            for key in self.value_labels:
                self._set_reading(key, None, "")
            return
        indices = [int(np.argmin(np.abs(self._x_values - line.value()))) for line in self.cursor_lines]
        value_channel = 0 if self._series_mode else channel
        t1, t2 = (float(self._times[index]) for index in indices)
        y1, y2 = (float(self._values[index, value_channel]) for index in indices)
        delta_t = t2 - t1
        delta_y = y2 - y1 if math.isfinite(y1) and math.isfinite(y2) else None
        in_interval = (self._times >= min(t1, t2)) & (self._times <= max(t1, t2))
        interval = self._values[in_interval, value_channel]
        interval = interval[np.isfinite(interval)]
        peak_to_peak = float(np.ptp(interval)) if len(interval) else None
        reciprocal_hz = 1 / abs(delta_t) if delta_t else None
        self.measurements = {
            "indices": tuple(int(self._sample_indices[index]) for index in indices),
            "times": (t1, t2), "y": (y1, y2), "delta_t": delta_t,
            "delta_y": delta_y, "peak_to_peak": peak_to_peak,
            "reciprocal_hz": reciprocal_hz,
        }
        if self._series_mode:
            self.measurements["key"] = self._series[channel]["key"]
        unit = self._channels[channel][1]

        for spin, timestamp in zip(self.time_inputs, (t1, t2)):
            with QtCore.QSignalBlocker(spin):
                spin.setValue(timestamp)
        self._set_reading("A", y1, unit)
        self._set_reading("B", y2, unit)
        self._set_reading("delta_t", delta_t, "s")
        self._set_reading("delta_y", delta_y, unit)
        self._set_reading("peak_to_peak", peak_to_peak, unit)
        self._set_reading("reciprocal_hz", reciprocal_hz, "Hz", empty="同一采样时刻，间隔倒数未定义")
        self.readout.setText("左右移动游标，上下调整 A / B 标签。" if delta_t
                             else "A、B 位于同一采样时刻，间隔倒数未定义。")
