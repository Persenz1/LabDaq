"""Independent-series and linked-plot measurement cursor coverage."""
import os
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets as W

from labdaq.plot_measurements import PlotMeasurements


@pytest.fixture
def qt():
    return W.QApplication.instance() or W.QApplication([])


@pytest.fixture
def measurement(qt):
    plot = pg.PlotWidget()
    measurements = PlotMeasurements(plot)
    yield measurements
    measurements.deleteLater()
    plot.deleteLater()
    qt.processEvents()


def make_series(key, label, times, values, x_values=None, unit="V"):
    times = np.asarray(times, dtype=float)
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "times": times,
        "x_values": times if x_values is None else np.asarray(x_values, dtype=float),
        "values": np.asarray(values, dtype=float),
    }


def test_independent_lengths_use_nearest_real_samples_and_keep_cursor_positions(measurement):
    first = make_series("a", "A", [0, .1, .2, .3], [0, 1, 4, 9], [100, 101, 102, 103])
    second = make_series(2, "B", np.arange(6) * .02, [10, 11, 14, 19, 25, 30], np.arange(6) + 100)
    measurement.set_series([first, second])
    measurement.enabled_checkbox.setChecked(True)
    for line, position in zip(measurement.cursor_lines, (100.1, 102.1)):
        line.setValue(position)

    assert measurement.measurements["key"] == "a"
    assert measurement.measurements["indices"] == (0, 2)
    assert measurement.measurements["times"] == pytest.approx((0, .2))
    assert measurement.measurements["y"] == pytest.approx((0, 4))
    assert measurement.measurements["delta_t"] == pytest.approx(.2)
    assert measurement.measurements["delta_y"] == pytest.approx(4)
    assert measurement.measurements["peak_to_peak"] == pytest.approx(4)
    assert measurement.measurements["reciprocal_hz"] == pytest.approx(5)

    measurement.channel_combo.setCurrentIndex(1)
    assert [line.value() for line in measurement.cursor_lines] == pytest.approx((100.1, 102.1))
    assert measurement.measurements["key"] == 2
    assert measurement.measurements["indices"] == (0, 2)
    assert measurement.measurements["times"] == pytest.approx((0, .04))
    assert measurement.measurements["y"] == pytest.approx((10, 14))
    assert measurement.measurements["delta_t"] == pytest.approx(.04)
    assert measurement.measurements["peak_to_peak"] == pytest.approx(4)

    measurement.set_series([dict(second, label="B updated"), make_series("empty", "Empty", [], []), first])
    assert measurement.channel_combo.currentIndex() == 0
    assert measurement.channel_combo.currentText() == "B updated"
    assert measurement.measurements["key"] == 2
    positions = [line.value() for line in measurement.cursor_lines]
    measurement.channel_combo.setCurrentIndex(1)
    assert measurement.measurements == {}
    assert [line.value() for line in measurement.cursor_lines] == pytest.approx(positions)
    measurement.channel_combo.setCurrentIndex(2)
    assert measurement.measurements["key"] == "a"
    assert [line.value() for line in measurement.cursor_lines] == pytest.approx(positions)


def test_nan_separators_are_not_samples_and_empty_series_are_valid(measurement):
    gapped = make_series("gapped", "Gapped", [0, .1, .2, .3, .4], [5, np.nan, 7, np.nan, 9], [0, 1, 2, 3, 4])
    measurement.set_series([gapped])
    measurement.enabled_checkbox.setChecked(True)
    for line, position in zip(measurement.cursor_lines, (1.4, 3.1)):
        line.setValue(position)

    assert [line.value() for line in measurement.cursor_lines] == pytest.approx((1.4, 3.1))
    assert measurement.measurements["indices"] == (2, 4)
    assert measurement.measurements["times"] == pytest.approx((.2, .4))
    assert measurement.measurements["y"] == pytest.approx((7, 9))
    assert measurement.measurements["delta_y"] == pytest.approx(2)
    assert measurement.measurements["peak_to_peak"] == pytest.approx(2)

    measurement.set_series([make_series("empty", "No samples", [], [])])
    assert measurement.channel_combo.count() == 1
    assert measurement.channel_combo.currentText() == "No samples"
    assert measurement.measurements == {}
    assert measurement.readout.text() == "当前窗口无有效样本。"


def test_series_window_resets_only_when_both_cursors_are_outside(measurement):
    measurement.set_series([make_series("near", "Near", [0, 1, 2], [0, 1, 2])])
    measurement.enabled_checkbox.setChecked(True)
    for line, position in zip(measurement.cursor_lines, (.4, 1.6)):
        line.setValue(position)
    measurement.set_series([make_series("overlap", "Overlap", [0, .5, 1, 1.5, 2, 2.5, 3], [0, 1, 2, 3, 4, 5, 6])])
    assert [line.value() for line in measurement.cursor_lines] == pytest.approx((.4, 1.6))

    measurement.set_series([make_series("far", "Far", [0, 1], [0, 1], [20, 21])])
    assert [line.value() for line in measurement.cursor_lines] == pytest.approx((20.2, 20.8))


def test_linked_plots_synchronize_both_directions_and_share_visibility(measurement, qt):
    measurement.set_series([make_series("a", "A", np.arange(5), [0, 2, 4, 8, 16])])
    measurement.enabled_checkbox.setChecked(True)
    first_plot = pg.PlotWidget()
    second_plot = pg.PlotWidget()
    first = measurement.link_plot(first_plot)
    second = measurement.link_plot(second_plot)
    assert len(measurement.cursor_lines) == 2
    assert measurement.linked_lines == [*first, *second]
    assert [line.value() for line in first] == pytest.approx([line.value() for line in measurement.cursor_lines])
    assert [line.value() for line in second] == pytest.approx([line.value() for line in measurement.cursor_lines])

    update = Mock(wraps=measurement._update_measurements)
    measurement._update_measurements = update
    first[0].setValue(1.26)
    assert update.call_count == 1
    for line in (measurement.cursor_lines[0], second[0]):
        assert line.value() == pytest.approx(1.26)
    assert measurement.measurements["indices"][0] == 1

    measurement.cursor_lines[1].setValue(2.7)
    assert update.call_count == 2
    for line in (first[1], second[1]):
        assert line.value() == pytest.approx(2.7)
    assert measurement.measurements["peak_to_peak"] == pytest.approx(6)

    measurement.enabled_checkbox.setChecked(False)
    assert all(not line.isVisible() for line in measurement.linked_lines)
    measurement.enabled_checkbox.setChecked(True)
    assert all(line.isVisible() for line in measurement.linked_lines)

    first_plot.deleteLater()
    qt.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qt.processEvents()
    assert measurement.linked_lines == list(second)
    measurement.enabled_checkbox.setChecked(False)
    measurement.enabled_checkbox.setChecked(True)
    second[0].setValue(2.1)
    assert measurement.cursor_lines[0].value() == pytest.approx(2.1)

    second_plot.deleteLater()
    qt.processEvents()


def test_original_set_data_keeps_channel_selection_and_source_indices(measurement):
    times = np.array([0, .1, np.nan, .3])
    values = np.array([[0, 10], [1, 11], [99, 99], [3, 13]], dtype=float)
    x_values = np.array([10, 11, 12, 13], dtype=float)
    channels = [("A", "V"), ("B", "mV")]
    measurement.set_data(times, values, channels, x_values=x_values)
    measurement.enabled_checkbox.setChecked(True)
    measurement.channel_combo.setCurrentIndex(1)
    for line, position in zip(measurement.cursor_lines, (10.1, 12.6)):
        line.setValue(position)

    assert measurement.measurements["indices"] == (0, 3)
    assert measurement.measurements["times"] == pytest.approx((0, .3))
    assert measurement.measurements["y"] == pytest.approx((10, 13))
    assert measurement.measurements["delta_t"] == pytest.approx(.3)
    assert measurement.measurements["peak_to_peak"] == pytest.approx(3)

    measurement.set_data(times, values[:, ::-1], list(reversed(channels)), x_values=x_values)
    assert measurement.channel_combo.currentIndex() == 0
    assert measurement.channel_combo.currentText() == "B"
    assert measurement.measurements["y"] == pytest.approx((10, 13))
