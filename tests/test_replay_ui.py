"""Folder-based replay, viewport navigation and recovery from a failed open."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6 import QtCore, QtTest, QtWidgets as W

from labdaq.gui import MainWindow, configure_application
from labdaq.processing import ChannelSettings
from labdaq.recording import SessionRecorder
from test_cursor_drag import drag
from test_daily_workflow import dispose


@pytest.fixture
def replay_window(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    window.resize(1180, 780)
    window.show()
    window.nav.setCurrentRow(3)
    app.processEvents()
    try:
        yield window, app
    finally:
        dispose(app, window)


def test_empty_folder_error_clears_after_valid_record_and_channels_are_short(replay_window, tmp_path):
    window, app = replay_window
    empty = tmp_path / "empty"
    empty.mkdir()
    window.open_replay(empty)
    assert window.error_banner.isVisible()
    assert window.replay is None

    path = tmp_path / "valid"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "1", unit="V"),
                                      ChannelSettings(3, "力传感器", unit="V")]) as recorder:
        recorder.write(np.column_stack((np.arange(100) / 100, np.ones(100))))
    window.open_replay(path)
    app.processEvents()

    assert window.replay_path == path
    assert not window.error_banner.isVisible()
    assert str(empty) not in window.notice.text()
    assert "无法打开" not in window.notice.text()
    assert [check.text() for check in window.replay_channel_checks] == ["AI0", "AI1", "AI2", "AI3"]
    combo = window.replay_measurements.channel_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["AI0", "AI3"]
    legend_text = [widget.text() for widget in window.pages.widget(3).findChildren(W.QLabel)
                   if widget.objectName() == "legendLabel"]
    assert legend_text == ["AI0", "AI3"]
    assert "AI0" in window.replay_info.text()
    assert "AI0 · 1" not in window.replay_info.text()

    # A subsequent failed open leaves the already opened data intact.
    old_reader = window.replay
    window.open_replay(empty)
    assert window.error_banner.isVisible()
    assert window.replay is old_reader
    window.open_replay(path)
    assert not window.error_banner.isVisible()


def test_replay_page_has_no_library_analysis_or_transport_controls(replay_window):
    window, _ = replay_window
    page = window.pages.widget(3)
    texts = {button.text() for button in page.findChildren(W.QPushButton)}
    assert {"打开记录文件夹", "打开 HPF", "导出数据", "保存图像", "适应窗口"} <= texts
    assert not {"记录库", "最近记录", "统计所选数据", "分析此窗口", "上一窗口", "下一窗口", "选当前窗口"} & texts
    assert not page.findChildren(W.QSlider)
    assert isinstance(window.replay_timeline, pg.PlotWidget)
    assert isinstance(window.replay_time_region, pg.LinearRegionItem)
    for name in ("replay_start", "replay_slider", "replay_select", "selection_start",
                 "selection_stop", "replay_region"):
        assert not hasattr(window, name)


def test_plot_pan_loads_later_samples_and_fit_restores_span_and_y(replay_window, tmp_path):
    window, app = replay_window
    fs = 1000
    raw = (2 * np.sin(2 * np.pi * 7 * np.arange(40000) / fs))[:, None]
    path = tmp_path / "long_record"
    with SessionRecorder(path, fs, [ChannelSettings(0, "1", unit="V")]) as recorder:
        recorder.write(raw)
    window.open_replay(path)
    window.replay_seconds.setValue(2)
    window.replay_plot.setXRange(25, 27, padding=0)
    app.processEvents()
    block, start, _ = window._replay_window()
    assert start == 25000
    np.testing.assert_array_equal(block, raw[25000:27000])
    np.testing.assert_array_equal(window.replay_curves[0].yData, block[:, 0])
    assert window.replay_curves[0].xData[0] == pytest.approx(25)

    window.replay_plot.setXRange(25.5, 25.6, padding=0)
    window.replay_plot.setYRange(20, 30, padding=0)
    window._fit_replay_plot()
    app.processEvents()
    x_range, y_range = window.replay_plot.viewRange()
    assert x_range[1] - x_range[0] == pytest.approx(2)
    assert x_range[0] >= 0 and x_range[1] <= 40
    assert y_range[0] <= -2 and y_range[1] >= 2
    assert window._replay_selection() == (0, len(raw), [0])


def test_large_record_keeps_window_reads_bounded(replay_window, tmp_path, monkeypatch):
    window, app = replay_window
    fs = 100000
    path = tmp_path / "high_rate"
    with SessionRecorder(path, fs, [ChannelSettings(0, "1", unit="V")]) as recorder:
        recorder.write(np.arange(600000, dtype=float)[:, None] / fs)
    window.open_replay(path)
    reader = window.replay
    reads = []
    read = reader.read

    def bounded_read(start=0, count=None):
        assert count is not None and 0 <= count <= 200000
        reads.append((start, count))
        return read(start=start, count=count)

    monkeypatch.setattr(reader, "read", bounded_read)
    window.replay_plot.setXRange(4, 6, padding=0)
    app.processEvents()
    block, start, _ = window._replay_window()
    assert start == 400000
    assert len(block) == 200000
    assert reads
    assert window.replay_curves[0].xData[-1] == pytest.approx(5.99999)

    # Expanding the time strip cannot bypass the same visible-window cap.
    window.replay_time_region.setRegion((0, 6))
    app.processEvents()
    block, start, _ = window._replay_window()
    low, high = window.replay_time_region.getRegion()
    assert len(block) <= 200000
    assert high - low <= 2 + 1e-9
    assert window.replay_plot.viewRange()[0] == pytest.approx((low, high))


def test_short_record_timeline_resizes_moves_and_resets_on_open(replay_window, tmp_path):
    window, app = replay_window
    raw = np.arange(7000, dtype=float)[:, None] / 1000
    path = tmp_path / "seven_seconds"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "1", unit="V")]) as recorder:
        recorder.write(raw)
    window.open_replay(path)
    app.processEvents()
    assert window.replay_time_region.getRegion() == pytest.approx((0, 7))
    window.replay_plot.setYRange(-1, 10, padding=0)
    y_range = window.replay_plot.viewRange()[1]

    for limits in ((1, 3), (4, 6)):
        window.replay_time_region.setRegion(limits)
        app.processEvents()
        block, start, _ = window._replay_window()
        assert start == limits[0] * 1000
        np.testing.assert_array_equal(block, raw[start:limits[1] * 1000])
        assert window.replay_plot.viewRange()[0] == pytest.approx(limits)
        assert window.replay_plot.viewRange()[1] == pytest.approx(y_range)

    window.open_replay(path)
    app.processEvents()
    assert window.replay_time_region.getRegion() == pytest.approx((0, 7))
    assert window._replay_window()[1] == 0

    empty = tmp_path / "empty_record"
    with SessionRecorder(empty, 1000, [ChannelSettings(0, "1", unit="V")]):
        pass
    window.open_replay(empty)
    app.processEvents()
    assert len(window._replay_window()[0]) == 0
    assert window.replay_measurements.measurements == {}
    window.open_replay(path)
    app.processEvents()
    assert window.replay_time_region.getRegion() == pytest.approx((0, 7))
    assert len(window._replay_window()[0]) == len(raw)


@pytest.mark.parametrize("mode", ["relative", "samples", "absolute"])
def test_timeline_and_waveform_ranges_stay_in_sync_across_axes(replay_window, tmp_path, mode):
    window, app = replay_window
    raw = np.arange(7000, dtype=float)[:, None] / 1000
    path = tmp_path / "axis_record"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "1", unit="V")]) as recorder:
        recorder.write(raw)
    window.open_replay(path)
    window.replay_time_axis.setCurrentIndex(window.replay_time_axis.findData(mode))
    app.processEvents()
    scale = 1000 if mode == "samples" else 1
    offset = window._replay_epoch() if mode == "absolute" else 0

    # The strip always denotes physical relative seconds, regardless of axis labels.
    window.replay_time_region.setRegion((2.25, 3.25))
    app.processEvents()
    block, start, _ = window._replay_window()
    assert start == 2250
    np.testing.assert_array_equal(block, raw[2250:3250])
    assert window.replay_plot.viewRange()[0] == pytest.approx(
        (offset + 2.25 * scale, offset + 3.25 * scale), abs=1e-6, rel=0)

    # Main-plot pan/zoom updates the strip without changing its seconds convention.
    window.replay_plot.setXRange(offset + 4 * scale, offset + 5.25 * scale, padding=0)
    app.processEvents()
    assert window.replay_time_region.getRegion() == pytest.approx((4, 5.25), abs=1e-6)
    block, start, _ = window._replay_window()
    assert start == 4000
    np.testing.assert_array_equal(block, raw[4000:5250])


def test_timeline_mouse_drags_resize_edge_and_move_middle(replay_window, tmp_path):
    window, app = replay_window
    path = tmp_path / "mouse_record"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "1", unit="V")]) as recorder:
        recorder.write(np.arange(7000, dtype=float)[:, None] / 1000)
    window.open_replay(path)
    QtTest.QTest.qWait(30)
    timeline = window.replay_timeline
    view = timeline.getViewBox()
    region = window.replay_time_region
    mouse_rate = pg.getConfigOption("mouseRateLimit")
    pg.setConfigOption("mouseRateLimit", 0)
    try:
        right = timeline.mapFromScene(view.mapViewToScene(QtCore.QPointF(7, .5)))
        drag(app, timeline, right, -round(view.width() * 4 / 7))
        low, high = region.getRegion()
        assert low == pytest.approx(0)
        assert 2.8 < high < 3.2
        assert window.replay_plot.viewRange()[0] == pytest.approx((low, high))

        width = high - low
        middle = timeline.mapFromScene(view.mapViewToScene(QtCore.QPointF((low + high) / 2, .5)))
        drag(app, timeline, middle, round(view.width() * 2 / 7))
        low, high = region.getRegion()
        assert 1.8 < low < 2.2
        assert high - low == pytest.approx(width)
        assert window.replay_plot.viewRange()[0] == pytest.approx((low, high))
        assert window._replay_window()[1] == round(low * 1000)
    finally:
        pg.setConfigOption("mouseRateLimit", mouse_rate)
