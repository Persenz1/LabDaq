"""Stopped and hidden displays must not keep processing unchanged samples."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6 import QtCore, QtWidgets as W

from labdaq.gui import MainWindow, configure_application
from labdaq.processing import ChannelSettings


@pytest.fixture
def stopped_display(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    window.timer.stop()
    window.channels = [ChannelSettings(i, f"AI {i}", unit="V") for i in range(4)]
    window._sync_channel_labels()
    times = np.arange(16000) / 2000
    raw = np.tile(np.linspace(-.5, 1.5, len(times))[:, None], (1, 4))
    engine = SimpleNamespace(
        running=False, recording=False, recording_paused=False, transport=None,
        source="simulation", termination_reason="user_stop", error="",
        last_recording=None, total_scans=len(times), recorded_scans=0,
        recording_remaining_scans=None, sample_rate=2000,
        acquisition_started_at_utc=None, snapshot=Mock(return_value=(times, raw)),
    )
    window.engine = engine
    try:
        yield window, engine, times, raw
    finally:
        window.engine = None
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_stopped_buffer_is_drawn_once_but_display_changes_still_apply(stopped_display, monkeypatch):
    window, engine, times, raw = stopped_display
    draw = Mock(wraps=window._draw_live)
    monkeypatch.setattr(window, "_draw_live", draw)
    window.refresh()
    for _ in range(10):
        window.refresh()
    assert engine.snapshot.call_count == draw.call_count == 1
    np.testing.assert_array_equal(window.live_curves[0].yData, raw[:, 0])
    window.show()
    W.QApplication.processEvents()
    assert len(window.live_curves[0].getData()[0]) < len(raw)

    window.metric_mode_combos[0].setCurrentIndex(window.metric_mode_combos[0].findData("mean"))
    window.value_decimals.setValue(6)
    window.refresh()
    assert float(window.metric_values[0].text()) == pytest.approx(np.mean(raw[-2000:, 0]), abs=5.1e-7)
    window.channels[0].zero_volts = .2
    window.refresh()
    assert float(window.metric_values[0].text()) == pytest.approx(np.mean(raw[-2000:, 0]) - .2, abs=5.1e-7)
    np.testing.assert_array_equal(window.live_curves[0].yData, raw[:, 0] - .2)
    assert draw.call_count == 3

    # Rendering reduction never replaces the samples used by measurement cursors.
    window.analysis_mode.setCurrentIndex(3)
    window.refresh_analysis()
    window.analysis_measurements.enabled_checkbox.setChecked(True)
    for line, timestamp in zip(window.analysis_measurements.cursor_lines, (times[321], times[12345])):
        line.setValue(timestamp)
    assert window.analysis_measurements.measurements["times"] == (times[321], times[12345])
    assert window.analysis_measurements.measurements["y"] == pytest.approx((raw[321, 0] - .2, raw[12345, 0] - .2))


def test_live_checkboxes_only_hide_curves_and_analysis_can_freeze(stopped_display):
    window, engine, times, raw = stopped_display
    original = raw.copy()
    window.refresh()
    window.pause_plot.setChecked(True)
    window.live_channel_checks[1].setChecked(False)
    assert not window.live_curves[1].isVisible()
    assert window.live_curves[1].yData is None
    assert window.active_channels == (0, 1, 2, 3)
    assert len(window.channels) == 4
    assert float(window.metric_values[1].text()) == pytest.approx(raw[-1, 1])
    window.live_channel_checks[1].setChecked(True)
    np.testing.assert_array_equal(window.live_curves[1].yData, raw[:, 1])
    np.testing.assert_array_equal(raw, original)

    window.nav.setCurrentRow(2)
    window.analysis_mode.setCurrentIndex(3)
    np.testing.assert_array_equal(window.analysis_curve.xData, times)
    np.testing.assert_array_equal(window.analysis_curve.yData, raw[:, 0])
    window.analysis_freeze.setChecked(True)
    engine.snapshot.return_value = times + 1, raw + 2
    window.refresh_analysis()
    np.testing.assert_array_equal(window.analysis_curve.yData, raw[:, 0])
    window.analysis_freeze.setChecked(False)
    np.testing.assert_array_equal(window.analysis_curve.yData, raw[:, 0] + 2)
    window.analysis_mode.setCurrentIndex(0)
    assert not window.analysis_measurements.isVisible()
    assert window.analysis_plot.getAxis("bottom").labelUnits == "Hz"


def test_hidden_live_page_skips_work_and_unfreezing_catches_up(stopped_display, monkeypatch):
    window, engine, times, raw = stopped_display
    draw = Mock(wraps=window._draw_live)
    monkeypatch.setattr(window, "_draw_live", draw)
    window.refresh()
    window.nav.setCurrentRow(3)
    engine.total_scans += 100
    engine.snapshot.return_value = times + .05, raw + .1
    window.refresh()
    assert engine.snapshot.call_count == draw.call_count == 1
    window.nav.setCurrentRow(0)
    window.refresh()
    np.testing.assert_array_equal(window.live_curves[0].yData, raw[:, 0] + .1)

    window.pause_plot.setChecked(True)
    engine.total_scans += 100
    engine.snapshot.return_value = times + .1, raw + .2
    window.refresh()
    assert draw.call_count == 2
    assert float(window.metric_values[0].text()) == pytest.approx(raw[-1, 0] + .2)
    np.testing.assert_array_equal(window.live_curves[0].yData, raw[:, 0] + .1)
    window.pause_plot.setChecked(False)
    window.refresh()
    assert draw.call_count == 3
    np.testing.assert_array_equal(window.live_curves[0].yData, raw[:, 0] + .2)


def test_live_window_grows_before_scrolling_and_keeps_manual_zoom(stopped_display):
    window, engine, times, raw = stopped_display
    window.live_seconds.setValue(4)
    engine.snapshot.return_value = times[:1000], raw[:1000]
    engine.total_scans = 1000
    window.refresh()
    assert window.live_plot.viewRange()[0] == pytest.approx((0, 4))
    assert window.live_curves[0].xData[-1] < .5

    engine.snapshot.return_value = times, raw
    engine.total_scans = len(times)
    window.refresh()
    assert window.live_plot.viewRange()[0] == pytest.approx((4, 8))
    window.live_seconds.setValue(6)
    window.refresh()
    assert window.live_plot.viewRange()[0] == pytest.approx((2, 8))
    engine.snapshot.assert_called_with(seconds=6)

    window.live_time_axis.setCurrentIndex(window.live_time_axis.findData("samples"))
    assert window.live_plot.viewRange()[0] == pytest.approx((4000, 16000))
    engine.acquisition_started_at_utc = "2026-10-19T04:00:00+00:00"
    window.live_time_axis.setCurrentIndex(window.live_time_axis.findData("absolute"))
    epoch = window._timestamp(engine.acquisition_started_at_utc)
    assert window.live_plot.viewRange()[0] == pytest.approx((epoch + 2, epoch + 8))

    window.live_time_axis.setCurrentIndex(window.live_time_axis.findData("relative"))
    window.live_plot.setXRange(5, 6, padding=0)
    window.live_plot.getViewBox().sigRangeChangedManually.emit([True, False])
    engine.snapshot.return_value = times + .1, raw
    engine.total_scans += 200
    window.refresh()
    assert window.live_plot.viewRange()[0] == pytest.approx((5, 6))
    window.live_seconds.setValue(2)
    assert window.live_plot.viewRange()[0] == pytest.approx((6.1, 8.1))


@pytest.mark.parametrize("paused", [False, True])
def test_zero_and_stopped_undo_keep_zoomed_waveforms_visible(stopped_display, paused):
    window, engine, times, _ = stopped_display
    raw = np.column_stack([
        offset + .4 * np.sin(2 * np.pi * frequency * times)
        for offset, frequency in zip((3, 1, -1, -3.5), (2, 1, .5, 3))
    ])
    original = raw.copy()
    previous = (.1, -.2, .3, -.4)
    for channel, zero in zip(window.channels, previous):
        channel.zero_volts = zero
    engine.running = True
    engine.snapshot.side_effect = lambda seconds=8: (
        times[-round(seconds * engine.sample_rate):], raw[-round(seconds * engine.sample_rate):],
    )
    window.show()
    window.refresh()
    window.pause_plot.setChecked(paused)
    window.live_plot.setRange(xRange=(6, 7), yRange=(2, 4), padding=0)
    window.live_plot.getViewBox().sigRangeChangedManually.emit([True, True])
    W.QApplication.processEvents()
    x_range = window.live_plot.viewRange()[0]

    window.zero_button.click()
    W.QApplication.processEvents()
    zeros = np.mean(raw[-2000:], axis=0)
    np.testing.assert_allclose([c.zero_volts for c in window.channels], zeros)
    for i, curve in enumerate(window.live_curves):
        np.testing.assert_allclose(curve.yData, raw[:, i] - zeros[i], atol=1e-12)
        low, high = window.live_plot.viewRange()[1]
        assert low <= np.min(curve.yData) <= np.max(curve.yData) <= high
    assert window.live_plot.viewRange()[0] == pytest.approx(x_range)
    assert engine.running and engine.total_scans == len(times)
    assert window.pause_plot.isChecked() == paused

    engine.running = False
    window._refresh_buttons()
    assert window.undo_zero_button.isEnabled()
    # Recording still locks conversion settings even when undo is pending.
    engine.recording = True
    window._refresh_buttons()
    assert not window.undo_zero_button.isEnabled()
    window.undo_zero()
    np.testing.assert_allclose([c.zero_volts for c in window.channels], zeros)
    engine.recording = False
    window._refresh_buttons()
    window.undo_zero_button.click()
    W.QApplication.processEvents()
    np.testing.assert_allclose([c.zero_volts for c in window.channels], previous)
    for i, curve in enumerate(window.live_curves):
        np.testing.assert_allclose(curve.yData, raw[:, i] - previous[i])
        low, high = window.live_plot.viewRange()[1]
        assert low <= np.min(curve.yData) <= np.max(curve.yData) <= high
    np.testing.assert_array_equal(raw, original)
    assert not window.undo_zero_button.isEnabled()
    assert window.live_plot.viewRange()[0] == pytest.approx(x_range)
