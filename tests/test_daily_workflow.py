"""Daily replay, statistics, export and zero-undo workflows on simulated data."""
import csv
import json
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6 import QtCore, QtGui, QtWidgets as W
from PySide6.QtTest import QTest

from labdaq import data_dialog
from labdaq.core_client import CORE_EXE, CoreClient
from labdaq.data_dialog import ExportDialog, StatisticsDialog
from labdaq.gui import MainWindow, configure_application
from labdaq.processing import ChannelSettings, convert_volts
from labdaq.recording import OperationCancelled, SessionRecorder, load_session
from labdaq.recording_library import RecordingLibraryDialog
from labdaq.signals import SignalSpec, SimulationSettings


def test_export_dialog_close_waits_for_controlled_cancellation(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    path = tmp_path / "session"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "X", unit="V")]) as recorder:
        recorder.write(np.arange(9000, dtype=float)[:, None])
    reader = load_session(path)
    entered, release = threading.Event(), threading.Event()
    original_export = data_dialog.export_csv

    def controlled_export(*args, progress, **kwargs):
        def pause_after_first_chunk(done, total):
            progress(done, total)
            if done == 4096:
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("test did not release export")
        return original_export(*args, progress=pause_after_first_chunk, **kwargs)

    monkeypatch.setattr(data_dialog, "export_csv", controlled_export)
    dialog = ExportDialog(reader, 0, 9000, [0])
    try:
        dialog.show()
        app.processEvents()
        output = tmp_path / "cancelled.csv"
        dialog.begin_export(output)
        spin(app, entered.is_set)
        assert dialog.progress.value() == 455
        dialog.close()
        app.processEvents()
        assert dialog.isVisible()
        assert dialog._close_when_finished
        assert not dialog.cancel_button.isEnabled()
        assert "正在取消" in dialog.status.text()
        release.set()
        spin(app, lambda: dialog.job is None)
        assert dialog.isVisible() and dialog.status.isVisible()
        assert not dialog._close_when_finished
        assert dialog.output_path is None
        assert dialog.target_path == output
        assert "已取消" in dialog.status.text() and "可能不完整" in dialog.status.text()
        assert str(output) in dialog.status.text()
        with output.open(encoding="utf-8-sig", newline="") as handle:
            handle.readline()
            rows = list(csv.reader(handle))
        np.testing.assert_array_equal(np.asarray(rows[1:], dtype=float)[:, 1], np.arange(4096))
        assert dialog.save_button.isEnabled()
        assert dialog.file_format.isEnabled() and dialog.values.isEnabled()
        assert dialog.time_format.isEnabled() and dialog.cancel_button.isEnabled()
        assert dialog.cancel_button.text() == "关闭"
        dialog.cancel_button.click()
        assert not dialog.isVisible()
        assert dialog.result() == W.QDialog.DialogCode.Rejected
    finally:
        release.set()
        if dialog.job is not None:
            spin(app, lambda: dialog.job is None)
        dispose(app, dialog)


def test_statistics_dialog_cancellation_discards_partial_results(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    path = tmp_path / "session"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "X", unit="V")]) as recorder:
        recorder.write(np.arange(8, dtype=float)[:, None])
    reader = load_session(path)
    entered, release = threading.Event(), threading.Event()

    def controlled_statistics(*_args, progress, cancelled, **_kwargs):
        progress(2, 5)
        entered.set()
        if not release.wait(8):
            raise TimeoutError("test did not release statistics")
        if cancelled():
            raise OperationCancelled("操作已取消")
        return [{"index": 0, "name": "partial", "unit": "V", "count": 2,
                 "min": 0, "max": 1, "delta": 1, "mean": .5, "std": .5, "rms": .7}]

    monkeypatch.setattr(data_dialog, "session_statistics", controlled_statistics)
    dialog = StatisticsDialog(reader, 0, 8, [0])
    try:
        dialog.show()
        spin(app, entered.is_set)
        assert dialog.progress.value() == 400
        assert dialog.table.rowCount() == 0
        dialog.close_button.click()
        app.processEvents()
        assert dialog.isVisible()
        assert not dialog.close_button.isEnabled()
        assert "正在取消" in dialog.status.text()
        release.set()
        spin(app, lambda: dialog.job is None)
        assert dialog.table.rowCount() == 0
        assert "已取消" in dialog.status.text() and "未显示不完整结果" in dialog.status.text()
        assert dialog.values.isEnabled() and dialog.close_button.isEnabled()
        assert not dialog.copy_button.isEnabled()
        assert dialog.close_button.text() == "关闭"
    finally:
        release.set()
        if dialog.job is not None:
            spin(app, lambda: dialog.job is None)
        dispose(app, dialog)


def spin(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        app.processEvents()
        QTest.qWait(20)
    app.processEvents()
    assert predicate()


def dispose(app, *widgets):
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_replay_exact_location_selection_statistics_export_and_png(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    fs = 1000.125
    channels = [ChannelSettings(0, "X", sensitivity_mv_per_unit=500, zero_volts=.1),
                ChannelSettings(3, "Y", sensitivity_mv_per_unit=250, inverted=True)]
    times = np.arange(20003) / fs
    raw = np.column_stack((.1 + np.sin(2 * np.pi * 10 * times), .4 * np.cos(2 * np.pi * 7 * times)))
    path = tmp_path / "20261019_模拟取数_001"
    with SessionRecorder(path, fs, channels, experiment="模拟取数",
                         recording_options={"notes": "记录库筛选", "group_number": 1}) as recorder:
        recorder.write(raw)
    before = (path / "raw.f64").read_bytes()
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    stats = export = library = None
    try:
        window.recording_root = tmp_path
        library = RecordingLibraryDialog(tmp_path)
        library.search.setText("记录库筛选")
        library.table.selectRow(0)
        library.open_button.click()
        assert library.selected_path == path
        window.open_replay(library.selected_path)
        window.replay_seconds.setValue(.1)
        window.replay_start.setValue(12.345)
        block, start, reader = window._replay_window()
        assert start == int(12.345 * fs + .5)
        np.testing.assert_array_equal(block, raw[start:start + len(block)])
        window.step_replay(1)
        assert window._replay_window()[1] == start + len(block)
        window.step_replay(-1)
        window.select_replay_window()
        window.replay_channel_checks[0].setChecked(False)
        first, stop, selected = window._replay_selection()
        assert (first, stop, selected) == (start, start + len(block), [3])
        assert window.replay_curves[0].xData is None or len(window.replay_curves[0].xData) == 0
        # Dragging region boundaries updates the same interval used by both tools.
        window.replay_region.setRegion(((start + 3) / fs, (start + 17) / fs))
        window._replay_region_changed()
        first, stop, selected = window._replay_selection()
        assert (first, stop) == (start + 3, start + 17)
        stats = StatisticsDialog(reader, first, stop, selected)
        spin(app, lambda: stats.job is None)
        assert stats.table.rowCount() == 1 and stats.copy_button.isEnabled()
        truth = convert_volts(raw[first:stop], channels)[:, 1]
        assert float(stats.table.item(0, 6).text()) == pytest.approx(np.mean(truth), rel=1e-9)
        assert float(stats.table.item(0, 7).text()) == pytest.approx(np.std(truth), rel=1e-9)
        stats.copy_table()
        assert "AI 3 · Y" in app.clipboard().text()
        export = ExportDialog(reader, first, stop, selected)
        export.file_format.setCurrentIndex(1)
        export.time_format.setCurrentIndex(export.time_format.findData("samples"))
        output = tmp_path / "selected.txt"
        export.begin_export(output)
        spin(app, lambda: export.job is None)
        assert export.output_path == output
        with output.open(encoding="utf-8-sig", newline="") as handle:
            metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
            rows = list(csv.reader(handle, delimiter="\t"))
        assert metadata["export"]["scan_start"] == first
        assert len(rows) == stop - first + 1
        values = np.asarray(rows[1:], dtype=float)
        np.testing.assert_array_equal(values[:, 0], np.arange(first, stop))
        np.testing.assert_array_equal(values[:, 1], raw[first:stop, 1])
        np.testing.assert_allclose(values[:, 2], truth)
        window.show()
        app.processEvents()
        image = tmp_path / "waveform.png"
        window.save_plot_image_to(window.replay_plot, image)
        assert not QtGui.QImage(str(image)).isNull()
        with pytest.raises(ValueError, match=".png"):
            window.save_plot_image_to(window.replay_plot, path / "raw.f64")
        assert (path / "raw.f64").read_bytes() == before
        window.selection_start.setValue(window.selection_stop.value())
        with pytest.raises(ValueError, match="至少需要一个样本"):
            window._replay_selection()
    finally:
        dispose(app, *[w for w in (stats, export, library, window) if w is not None])


def test_rust_simulated_preview_zero_can_be_undone_and_recorded(tmp_path):
    if not CORE_EXE.exists():
        pytest.skip("Build the Rust core first")
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    client, _ = CoreClient.connect_or_start(tmp_path / "endpoint.json", port=0)
    window = MainWindow(core_endpoint=client.endpoint)
    try:
        window.recording_root = tmp_path
        window.scenario.setCurrentIndex(window.scenario.findData("rust"))
        window.channels[0].zero_volts = .123
        window.signal_settings = SimulationSettings(channels=(SignalSpec(waveform="dc", amplitude_v=.4),) * 4)
        window.start_preview()
        spin(app, lambda: len(window.engine.snapshot()[0]) >= 200)
        window.zero_channels()
        assert window.undo_zero_button.isEnabled()
        assert window.channels[0].zero_volts == pytest.approx(.4, abs=20 / 2**24)
        window.stop_acquisition()
        assert not window.engine.running and window.undo_zero_button.isEnabled()
        window.undo_zero_button.click()
        assert window.channels[0].zero_volts == .123 and not window.undo_zero_button.isEnabled()
        assert client.request("poll")["profile"]["channels"][0]["zero_volts"] == .123
        window._set_recording_options({"duration_seconds": .137, "notes": "撤销调零后记录"})
        window.toggle_record()
        spin(app, lambda: not window.engine.recording)
        session = load_session(window.latest_recording)
        assert session.channels[0].zero_volts == .123
        np.testing.assert_allclose(session.read(), .4, rtol=0, atol=20 / 2**24)
        window.stop_acquisition()
    finally:
        window.close()
        try:
            client.request("stop")
            client.request("shutdown")
        except OSError:
            pass
        dispose(app, window)
