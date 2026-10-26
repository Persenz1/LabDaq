"""Cursor readings use samples and physical time, including recording gaps."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6 import QtCore, QtWidgets as W

from labdaq.plot_measurements import PlotMeasurements


@pytest.fixture
def cursors():
    app = W.QApplication.instance() or W.QApplication([])
    plot = pg.PlotWidget()
    widget = PlotMeasurements(plot)
    try:
        yield widget
    finally:
        widget.close()
        plot.close()
        widget.deleteLater()
        plot.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_sine_cursor_delta_peak_to_peak_and_channel_selection(cursors):
    times = np.arange(1001) / 1000
    values = np.column_stack((2 * np.sin(2 * np.pi * 5 * times), 3 + times))
    cursors.set_data(times, values, [("AI 0 · 力", "N"), ("AI 1 · 电压", "V")])
    assert not cursors.cursor_lines[0].isVisible()
    assert not cursors.channel_combo.isEnabled()
    cursors.enabled_checkbox.setChecked(True)
    assert all(line.isVisible() for line in cursors.cursor_lines)
    assert [line.value() for line in cursors.cursor_lines] == pytest.approx([.2, .8])
    cursors.cursor_lines[0].setValue(.05)
    cursors.cursor_lines[1].setValue(.15)
    measured = cursors.measurements
    assert measured["times"] == pytest.approx((.05, .15))
    assert measured["y"] == pytest.approx((2, -2))
    assert measured["delta_t"] == pytest.approx(.1)
    assert measured["delta_y"] == pytest.approx(-4)
    assert measured["peak_to_peak"] == pytest.approx(4)
    assert measured["reciprocal_hz"] == pytest.approx(10)
    assert cursors.value_labels["reciprocal_hz"].text() == "10"
    assert cursors.unit_labels["reciprocal_hz"].text() == "Hz"
    cursors.channel_combo.setCurrentIndex(1)
    assert cursors.measurements["y"] == pytest.approx((3.05, 3.15))
    assert cursors.measurements["delta_y"] == pytest.approx(.1)
    assert cursors.measurements["peak_to_peak"] == pytest.approx(.1)
    assert cursors.value_labels["A"].text() == "3.05"
    assert cursors.unit_labels["A"].text() == "V"
    cursors.enabled_checkbox.setChecked(False)
    assert all(not line.isVisible() for line in cursors.cursor_lines)
    assert cursors.measurements == {}


def test_gap_separator_and_sample_axis_use_actual_time_without_interpolation(cursors):
    times = np.array([0, .1, .2, np.nan, 2.3, 2.4])
    values = np.array([[1], [4], [-2], [np.nan], [3], [np.nan]])
    x_values = np.array([10, 11, 12, np.nan, 13, 14])
    cursors.set_data(times, values, [("AI 0", "N")], x_values=x_values)
    cursors.enabled_checkbox.setChecked(True)
    cursors.cursor_lines[0].setValue(12)
    cursors.cursor_lines[1].setValue(13)
    measured = cursors.measurements
    assert measured["indices"] == (2, 4)
    assert measured["times"] == pytest.approx((.2, 2.3))
    assert measured["delta_t"] == pytest.approx(2.1)
    assert measured["reciprocal_hz"] == pytest.approx(1 / 2.1)
    assert measured["delta_y"] == 5
    assert measured["peak_to_peak"] == 5
    cursors.cursor_lines[1].setValue(14)
    assert np.isnan(cursors.measurements["y"][1])
    assert cursors.measurements["delta_y"] is None
    assert cursors.measurements["peak_to_peak"] == 5
    assert cursors.value_labels["B"].text() == "—"
    assert "无有效数据" in cursors.value_labels["B"].toolTip()


def test_duplicate_epoch_x_uses_real_first_sample_and_zero_delta_is_undefined(cursors):
    epoch = 1792370000.0
    times = np.array([0, .1, .2, .3])
    x_values = epoch + np.array([0, .1, .1, .3])
    values = np.array([[0], [2], [99], [4]])
    cursors.set_data(times, values, [("AI 0", "V")], x_values=x_values)
    cursors.enabled_checkbox.setChecked(True)
    for line in cursors.cursor_lines:
        line.setValue(epoch + .1)
    assert cursors.measurements["indices"] == (1, 1)
    assert cursors.measurements["times"] == (.1, .1)
    assert cursors.measurements["y"] == (2, 2)
    assert cursors.measurements["delta_t"] == 0
    assert cursors.measurements["reciprocal_hz"] is None
    assert cursors.measurements["peak_to_peak"] == 0
    assert "同一采样时刻" in cursors.readout.text()


def test_rolling_window_repositions_cursors_and_empty_or_nan_data_is_clear(cursors):
    cursors.set_data([0, 1], [[0], [1]], [("AI 0", "V")])
    cursors.enabled_checkbox.setChecked(True)
    cursors.set_data([10, 11], [[10], [11]], [("AI 0", "V")])
    assert [line.value() for line in cursors.cursor_lines] == pytest.approx([10.2, 10.8])
    cursors.set_data([10, 11], [[np.nan], [np.nan]], [("AI 0", "V")])
    assert cursors.measurements["peak_to_peak"] is None
    assert cursors.measurements["delta_y"] is None
    cursors.set_data([], np.empty((0, 1)), [("AI 0", "V")])
    assert cursors.measurements == {}
    assert cursors.readout.text() == "当前窗口无有效样本。"
    assert not any(spin.isEnabled() for spin in cursors.time_inputs)
    assert not cursors.reset_button.isEnabled()
    assert all(value.text() == "—" for value in cursors.value_labels.values())
    assert all(spin.text() == "—" for spin in cursors.time_inputs)
    assert all(not line.isVisible() for line in cursors.cursor_lines)
    cursors.set_data([10, 11], [[10], [11]], [("AI 0", "V")])
    assert all(line.isVisible() for line in cursors.cursor_lines)
    assert all(spin.specialValueText() == "" for spin in cursors.time_inputs)
    assert [spin.value() for spin in cursors.time_inputs] == [10, 11]


def test_time_inputs_follow_real_samples_on_sample_axis_and_across_gaps(cursors):
    times = np.array([0, .1, .2, 2.3, 2.4])
    values = np.array([[1], [4], [-2], [3], [6]])
    x_values = np.array([10, 11, 12, 13, 14])
    cursors.set_data(times, values, [("AI0", "N")], x_values=x_values)
    cursors.enabled_checkbox.setChecked(True)
    cursors.time_inputs[0].setValue(.19)
    cursors.time_inputs[1].setValue(2.31)
    assert [line.value() for line in cursors.cursor_lines] == [12, 13]
    assert [spin.value() for spin in cursors.time_inputs] == pytest.approx([.2, 2.3])
    assert cursors.measurements["delta_t"] == pytest.approx(2.1)
    assert cursors.value_labels["delta_y"].text() == "5"
    assert cursors.time_inputs[0].singleStep() == pytest.approx(.1)


def test_return_cursors_uses_current_view_and_keeps_readings_synchronized(cursors):
    times = np.arange(1001) / 1000
    cursors.set_data(times, times[:, None], [("AI0", "V")])
    cursors.enabled_checkbox.setChecked(True)
    cursors.plot.setXRange(.4, .6, padding=0)
    cursors.reset_button.click()
    assert [line.value() for line in cursors.cursor_lines] == pytest.approx([.44, .56])
    assert cursors.measurements["times"] == pytest.approx([.44, .56])
    assert [spin.value() for spin in cursors.time_inputs] == pytest.approx([.44, .56])


def test_cursor_bounds_follow_loaded_samples_and_clamp_each_cursor(cursors):
    times = np.arange(11, dtype=float)
    cursors.set_data(times, times[:, None], [("AI0", "V")])
    cursors.enabled_checkbox.setChecked(True)
    cursors.cursor_lines[0].setValue(-3)
    cursors.cursor_lines[1].setValue(18)
    assert [line.value() for line in cursors.cursor_lines] == [0, 10]
    assert cursors.measurements["times"] == (0, 10)
    cursors.set_data(times[4:], times[4:, None], [("AI0", "V")])
    assert [line.value() for line in cursors.cursor_lines] == [4, 10]
    assert cursors.measurements["times"] == (4, 10)
    assert [spin.value() for spin in cursors.time_inputs] == [4, 10]


def test_linked_cursor_limits_match_primary_when_window_moves(cursors):
    times = np.arange(11, dtype=float)
    cursors.set_data(times, times[:, None], [("AI0", "V")])
    cursors.enabled_checkbox.setChecked(True)
    linked_plot = pg.PlotWidget()
    try:
        linked = cursors.link_plot(linked_plot)
        linked[0].setValue(-3)
        assert linked[0].value() == cursors.cursor_lines[0].value() == 0
        cursors.set_data(times[4:], times[4:, None], [("AI0", "V")])
        assert linked[0].value() == cursors.cursor_lines[0].value() == 4
        linked[1].setValue(20)
        assert linked[1].value() == cursors.cursor_lines[1].value() == 10
    finally:
        linked_plot.close()
        linked_plot.deleteLater()
