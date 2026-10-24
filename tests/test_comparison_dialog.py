import csv
import os
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6 import QtCore, QtGui, QtWidgets as W
from PySide6.QtTest import QTest

from labdaq import comparison_dialog as comparison_ui
from labdaq.comparison_dialog import ComparisonDialog
from labdaq.processing import ChannelSettings
from labdaq.recording import OperationCancelled, SessionRecorder, load_session
from labdaq.recording_library import RecordingLibraryDialog


def wait(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        app.processEvents()
        QTest.qWait(10)
    app.processEvents()
    assert predicate()


def make_group(root, name, *, second=False):
    path = root / name
    if second:
        channels = [ChannelSettings(1, "第二路电压", unit="V", point_number=2, direction="Y+")]
        raw = np.arange(8, dtype=float)[:, None] / 10
        fs = 4
    else:
        channels = [ChannelSettings(0, "电压", unit="V"),
                    ChannelSettings(2, "力", sensitivity_mv_per_unit=10, point_number=7, direction="X+")]
        raw = np.column_stack((np.arange(6) / 10, np.arange(6) / 20))
        fs = 10
    with SessionRecorder(path, fs, channels, experiment="重复实验",
                         acquisition_config={"record_start_scan": 40,
                                             "acquisition_started_at_utc": "2026-10-24T01:00:00+00:00"},
                         recording_options={"specimen": "A-01", "condition": "室温", "notes": "比较验证"}) as writer:
        if second:
            writer.write(raw, source_start_scan=40)
        else:
            writer.write(raw[:3], source_start_scan=40)
            writer.write(raw[3:], source_start_scan=47)
    return path


def close(window, app):
    window.close()
    if window.job is not None:
        wait(app, lambda: window.job is None)
    window.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_comparison_full_qt_workflow(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    first = make_group(tmp_path, "first")
    second = make_group(tmp_path, "second", second=True)
    original_raw = (first / "raw.f64").read_bytes()
    dialog = ComparisonDialog([first, second])
    dialog.show()
    try:
        wait(app, lambda: dialog.comparison_result is not None and dialog.job is None)
        result = dialog.comparison_result
        assert len(result.traces) == 3 and len(dialog.plots) == 2
        assert result.traces[1].unit == "N"
        assert not result.traces[1].raw_values.flags.writeable
        first_curve = dialog.curves["0:0"]
        assert np.isnan(first_curve.yData[3])
        assert first_curve.xData[3] == pytest.approx(result.traces[0].x[3])
        dialog.measurements.enabled_checkbox.setChecked(True)
        dialog.measurements.cursor_lines[0].setValue(.1)
        assert dialog.measurements.measurements["key"] == "0:0"
        assert dialog.measurements.measurements["times"][0] == pytest.approx(.1)
        assert dialog.measurements.measurements["y"][0] == pytest.approx(.1)
        assert len(dialog.measurements.linked_lines) == 2
        assert dialog.measurements.linked_lines[0].value() == pytest.approx(.1)
        assert dialog.measurements.linked_lines[1].value() == pytest.approx(
            dialog.measurements.cursor_lines[1].value())

        dialog.group_combo.setCurrentIndex(0)
        dialog.zero_input.setValue(.12)
        dialog.align_button.click()
        wait(app, lambda: dialog.job is None and dialog.comparison_result.sources[0].zero_seconds == .1)
        assert dialog.comparison_result.start == pytest.approx(-.1)
        assert dialog.comparison_result.traces[0].x[0] == pytest.approx(-.1)
        dialog.axis.setCurrentIndex(dialog.axis.findData("samples"))
        wait(app, lambda: dialog.job is None and dialog.comparison_result.axis == "samples")
        dialog.group_combo.setCurrentIndex(0)
        dialog.zero_input.setValue(7)
        dialog.align_button.click()
        wait(app, lambda: dialog.job is None and dialog.comparison_result.sources[0].sample_origin == 7)
        assert dialog.comparison_result.traces[0].x[3] == 0
        dialog.axis.setCurrentIndex(dialog.axis.findData("absolute"))
        wait(app, lambda: dialog.job is None and dialog.comparison_result.axis == "absolute")
        assert isinstance(dialog.plots[0].getAxis("bottom"), pg.DateAxisItem)

        dialog.trace_checks["0:2"].setChecked(False)
        assert not dialog.curves["0:2"].isVisible()
        assert dialog.measurements.channel_combo.count() == 2
        monkeypatch.setattr(W.QColorDialog, "getColor", lambda *args, **kwargs: QtGui.QColor("#123456"))
        dialog._choose_color("0:0")
        assert dialog.appearance["0:0"]["color"] == "#123456"
        dialog.splitter.setSizes([600, 900])
        saved_sizes = dialog.splitter.sizes()
        layout_path = tmp_path / "layout.json"
        dialog.save_profile_to(layout_path)
        dialog.trace_checks["0:2"].setChecked(True)
        dialog.axis.setCurrentIndex(0)
        wait(app, lambda: dialog.job is None and dialog.comparison_result.axis == "relative")
        dialog.splitter.setSizes([450, 450])
        dialog.load_profile_from(layout_path)
        wait(app, lambda: dialog.job is None and dialog.comparison_result.axis == "absolute")
        assert dialog.comparison_result.sources[0].zero_seconds == .1
        assert dialog.comparison_result.sources[0].sample_origin == 7
        assert not dialog.appearance["0:2"]["visible"]
        assert dialog.appearance["0:0"]["color"] == "#123456"
        assert dialog._view["splitter_sizes"] == saved_sizes
        assert dialog.splitter.sizes() == saved_sizes

        csv_path = tmp_path / "comparison.csv"
        dialog.begin_export(csv_path)
        wait(app, lambda: dialog.job is None and dialog.output_path == csv_path)
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            assert handle.readline().startswith("# labdaq_metadata:")
            rows = list(csv.reader(handle))
        assert len(rows) == 1 + sum(len(trace.x) for trace in dialog.comparison_result.traces)
        assert rows[0][:6] == ["recording", "channel_index", "channel_name",
                               "sample_index", "source_time_s", "comparison_x"]
        image_path = tmp_path / "comparison.png"
        dialog.save_image_to(image_path)
        assert image_path.stat().st_size > 0
        assert not QtGui.QImage(str(image_path)).isNull()
        assert (first / "raw.f64").read_bytes() == original_raw
        assert load_session(first).metadata["recording_options"]["specimen"] == "A-01"
    finally:
        close(dialog, app)


def test_comparison_library_opens_real_dialog_and_guards_incomplete(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    first = make_group(tmp_path, "first")
    second = make_group(tmp_path, "second", second=True)
    library = RecordingLibraryDialog(tmp_path)
    comparison = None
    try:
        library.table.setCurrentCell(0, 0)
        flags = (QtCore.QItemSelectionModel.SelectionFlag.Select |
                 QtCore.QItemSelectionModel.SelectionFlag.Rows)
        for row in range(2):
            library.table.selectionModel().select(library.table.model().index(row, 0), flags)
        comparison = library._compare_selected()
        assert isinstance(comparison, ComparisonDialog)
        comparison.show()
        wait(app, lambda: comparison.comparison_result is not None and comparison.job is None)
        assert {trace.record_path for trace in comparison.comparison_result.traces} == {first, second}
    finally:
        if comparison is not None:
            close(comparison, app)
        library.close()
        library.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_comparison_cancel_and_close_wait_for_worker(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    first = make_group(tmp_path, "first")
    second = make_group(tmp_path, "second", second=True)
    dialog = ComparisonDialog([first, second])
    original = comparison_ui.load_comparison

    def delayed(*args, **kwargs):
        cancelled = kwargs["cancelled"]
        for _ in range(200):
            if cancelled():
                raise OperationCancelled("测试取消")
            time.sleep(.002)
        return original(*args, **kwargs)

    monkeypatch.setattr(comparison_ui, "load_comparison", delayed)
    dialog.show()
    try:
        wait(app, lambda: dialog.job is not None and dialog.job.isRunning())
        dialog.reject()
        wait(app, lambda: dialog.job is None)
        assert dialog.comparison_result is None
        assert "已取消" in dialog.status.text()
        dialog.reload()
        wait(app, lambda: dialog.job is not None and dialog.job.isRunning())
        dialog.close()
        wait(app, lambda: dialog.job is None)
        assert dialog.result() == W.QDialog.DialogCode.Rejected
    finally:
        close(dialog, app)


def test_comparison_absolute_time_requires_real_timestamps(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    first = make_group(tmp_path, "first")
    second = make_group(tmp_path, "second", second=True)
    metadata_path = second / "metadata.json"
    import json
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["started_at_utc"] = None
    metadata["acquisition_started_at_utc"] = None
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    dialog = ComparisonDialog([first, second])
    try:
        wait(app, lambda: dialog.comparison_result is not None and dialog.job is None)
        assert not dialog.axis.model().item(1).isEnabled()
        dialog.axis.setCurrentIndex(1)
        dialog._axis_changed()
        wait(app, lambda: dialog.job is None)
        assert dialog.axis.currentData() == "relative"
        assert "缺少可靠采集起点" in dialog.status.text()
    finally:
        close(dialog, app)


def test_gui_recording_to_library_comparison_end_to_end(tmp_path):
    from labdaq.gui import MainWindow, configure_application
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "no-core.json")
    library = None
    comparison = None
    paths = []
    try:
        window.scenario.setCurrentIndex(window.scenario.findData("drag"))
        window.recording_root = tmp_path
        window.experiment.setText("端到端重复实验")
        window.condition.setText("室温 · 重复两次")
        window.channels[0] = ChannelSettings(0, "第一路", unit="V", point_number=7,
                                             direction="X+", engineering_offset=2.5)
        window.channels[2] = ChannelSettings(2, "第二路", unit="V", point_number=8,
                                             direction="Y+", engineering_offset=-.5)
        window.active_channels = (0, 2)
        window._sync_channel_labels()
        window.show()
        for group in (1, 2):
            assert window.recording_sequence.value() == group
            window.record_button.click()
            wait(app, lambda: window.engine is not None and window.engine.recorded_scans >= 30)
            window.record_button.click()
            assert not window.engine.recording
            assert window.recording_sequence.value() == group + 1
            paths.append(window.latest_recording)
        window.stop_button.click()
        library = RecordingLibraryDialog(tmp_path)
        library.table.setCurrentCell(0, 0)
        flags = (QtCore.QItemSelectionModel.SelectionFlag.Select |
                 QtCore.QItemSelectionModel.SelectionFlag.Rows)
        for row in range(2):
            library.table.selectionModel().select(library.table.model().index(row, 0), flags)
        assert library.compare_button.isEnabled()
        library.compare_button.click()
        comparison = library._comparison_windows[-1]
        wait(app, lambda: comparison.comparison_result is not None and comparison.job is None)
        assert {source.path for source in comparison.comparison_result.sources} == set(paths)
        for source in comparison.comparison_result.sources:
            assert source.session.metadata["recording_options"]["condition"] == "室温 · 重复两次"
            assert source.session.metadata["recording_options"]["specimen"] == ""
            assert source.session.metadata["recording_options"]["notes"] == ""
            assert source.channels[0].point_number == 7
            assert source.channels[0].engineering_offset == 2.5
            assert source.session.sample_count >= 30
    finally:
        if comparison is not None:
            close(comparison, app)
        if library is not None:
            library.close()
            library.deleteLater()
        window.stop_acquisition()
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_modal_library_closes_before_comparison_interaction(tmp_path, monkeypatch):
    from labdaq import gui as gui_module
    app = W.QApplication.instance() or W.QApplication([])
    gui_module.configure_application(app)
    make_group(tmp_path, "first")
    make_group(tmp_path, "second", second=True)
    window = gui_module.MainWindow(core_endpoint=tmp_path / "no-core.json")
    window.recording_root = tmp_path
    holder = {"library": None}
    comparison = None

    def capture_library(root, parent):
        holder["library"] = RecordingLibraryDialog(root, parent)
        return holder["library"]

    monkeypatch.setattr(gui_module, "RecordingLibraryDialog", capture_library)

    def open_comparison():
        library = holder["library"]
        library.table.setCurrentCell(0, 0)
        flags = (QtCore.QItemSelectionModel.SelectionFlag.Select |
                 QtCore.QItemSelectionModel.SelectionFlag.Rows)
        for row in range(2):
            library.table.selectionModel().select(library.table.model().index(row, 0), flags)
        assert library._compare_selected() is None
        assert library.result() == W.QDialog.DialogCode.Rejected

    QtCore.QTimer.singleShot(0, open_comparison)
    window.show()
    window.open_recording_library()
    comparison = window._comparison_windows[-1]
    try:
        assert comparison is not None and comparison.parentWidget() is None
        wait(app, lambda: comparison.comparison_result is not None and comparison.job is None)
        assert app.activeModalWidget() is None
        key = f"0:{comparison.sources[0].channel_indices[0]}"
        check = comparison.trace_checks[key]
        assert check.isVisible()
        previous = check.isChecked()
        comparison.activateWindow()
        app.processEvents()
        QTest.qWait(10)
        option = W.QStyleOptionButton()
        option.initFrom(check)
        click_rect = check.style().subElementRect(W.QStyle.SubElement.SE_CheckBoxClickRect, option, check)
        QTest.mouseClick(check, QtCore.Qt.MouseButton.LeftButton, pos=click_rect.center())
        app.processEvents()
        assert check.isChecked() != previous
    finally:
        if comparison is not None:
            close(comparison, app)
        if holder["library"] is not None:
            holder["library"].close()
            holder["library"].deleteLater()
        window.stop_acquisition()
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
