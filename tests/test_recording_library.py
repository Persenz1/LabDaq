import json
import os
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtWidgets as W

from labdaq.processing import ChannelSettings
from labdaq.recording import SessionRecorder
from labdaq.recording_library import RecordingLibraryDialog, list_recordings


def make_recording(root, name, started, *, notes="", specimen="", condition="",
                   reason="user_stop", finalized=True):
    path = root / name
    writer = SessionRecorder(path, 4, [ChannelSettings(0, "力"), ChannelSettings(2, "位移")],
                             experiment="拉伸试验",
                             recording_options={"group_number": 2, "notes": notes,
                                                "specimen": specimen, "condition": condition})
    writer.write([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]])
    writer.close(termination_reason=reason)
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if started:
        metadata["started_at_utc"] = started
    else:
        metadata.pop("started_at_utc")
    metadata["finalized"] = finalized
    if not finalized:
        metadata["sample_count"] = 0
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def recordings(tmp_path):
    older = make_recording(tmp_path, "20261018_拉伸_002", "2026-10-18T06:30:00+00:00", notes="粗试样")
    newest = make_recording(tmp_path, "20261019_拉伸_002", "2026-10-19T02:10:00Z",
                            notes="细试样 Run B", reason="duration_complete")
    interrupted = make_recording(tmp_path, "未结束", None, finalized=False)
    broken = tmp_path / "损坏记录"
    broken.mkdir()
    (broken / "metadata.json").write_text("{ broken", encoding="utf-8")
    # A nested session must not become a second search root.
    make_recording(tmp_path / "archive", "nested", "2026-10-20T02:00:00Z")
    return tmp_path, older, newest, interrupted, broken


def test_list_records_metadata_without_reading_voltage(recordings, monkeypatch):
    root, older, newest, interrupted, broken = recordings
    original_open = Path.open

    def metadata_only_open(path, *args, **kwargs):
        assert path.name != "raw.f64", "the library must only stat raw data"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", metadata_only_open)
    entries = list_recordings(root)
    assert [entry.path for entry in entries[:2]] == [newest, older]
    assert {entry.path for entry in entries} == {older, newest, interrupted, broken}
    current = entries[0]
    assert current.started_at == datetime(2026, 10, 19, 2, 10, tzinfo=timezone.utc)
    assert current.duration_seconds == pytest.approx(0.75)
    assert current.channel_count == 2 and current.group_number == 2
    assert current.status == "定长完成"
    assert current.matches("run b") and current.matches("拉伸") and current.matches("定长")
    assert current.matches("20261019") and not current.matches("粗试样")
    by_path = {entry.path: entry for entry in entries}
    assert by_path[older].status == "手动结束"
    assert by_path[interrupted].status == "未正常收尾"
    assert by_path[interrupted].duration_seconds == pytest.approx(.75)
    assert by_path[broken].status == "不可打开"
    assert "JSONDecodeError" in by_path[broken].error


def test_missing_recording_root_stays_missing(tmp_path):
    root = tmp_path / "not-created"
    assert list_recordings(root) == []
    assert not root.exists()


def test_library_edits_details_and_preserves_selection(tmp_path, monkeypatch):
    from labdaq.recording import load_session
    from labdaq.recording_library import RecordingDetailsDialog
    app = W.QApplication.instance() or W.QApplication([])
    first = make_recording(tmp_path, "first", "2026-10-18T00:00:00Z", specimen="A-01")
    second = make_recording(tmp_path, "second", "2026-10-19T00:00:00Z")
    unfinished = make_recording(tmp_path, "unfinished", None, finalized=False)
    original_raw = (first / "raw.f64").read_bytes()

    def apply_changes(editor):
        editor.experiment.setText("修改后的实验")
        editor.specimen.setText("B-02")
        editor.condition.setText("50 °C")
        editor.notes.setPlainText("第二次重复实验")
        editor.accept()
        return editor.result()

    monkeypatch.setattr(RecordingDetailsDialog, "exec", apply_changes)
    dialog = RecordingLibraryDialog(tmp_path)
    changed = []
    dialog.details_changed.connect(changed.append)
    try:
        first_row = next(row for row in range(dialog.table.rowCount())
                         if dialog.table.item(row, 8).text() == first.name)
        dialog.table.selectRow(first_row)
        assert dialog.edit_button.isEnabled()
        dialog.edit_button.click()
        assert changed == [first]
        assert (first / "raw.f64").read_bytes() == original_raw
        metadata = load_session(first).metadata
        assert metadata["experiment"] == "修改后的实验"
        assert metadata["recording_options"]["specimen"] == "B-02"
        assert metadata["recording_options"]["condition"] == "50 °C"
        assert metadata["recording_options"]["notes"] == "第二次重复实验"
        assert dialog._selected_entry().path == first
        dialog.search.setText("B-02")
        assert dialog._selected_entry().path == first
        dialog.search.setText("50 °C")
        assert dialog._selected_entry().path == first
        dialog.search.clear()

        flags = (QtCore.QItemSelectionModel.SelectionFlag.Select |
                 QtCore.QItemSelectionModel.SelectionFlag.Rows)
        dialog.table.clearSelection()
        dialog.table.setCurrentCell(0, 0)
        for row in range(2):
            dialog.table.selectionModel().select(dialog.table.model().index(row, 0), flags)
        assert {entry.path for entry in dialog._selected_entries()} == {first, second}
        assert dialog.compare_button.isEnabled()
        dialog.table.selectRow(next(row for row in range(dialog.table.rowCount())
                                    if dialog.table.item(row, 8).text() == unfinished.name))
        assert not dialog.compare_button.isEnabled()
        assert not dialog.edit_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_comparison_selection_allows_finalized_errors_only(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    good = make_recording(tmp_path, "good", "2026-10-18T00:00:00Z")
    abnormal = make_recording(tmp_path, "abnormal", "2026-10-19T00:00:00Z", reason="acquisition_error")
    unfinished = make_recording(tmp_path, "unfinished", None, finalized=False)
    dialog = RecordingLibraryDialog(tmp_path)
    try:
        flags = (QtCore.QItemSelectionModel.SelectionFlag.Select |
                 QtCore.QItemSelectionModel.SelectionFlag.Rows)
        dialog.table.setCurrentCell(0, 0)
        for row in range(2):
            dialog.table.selectionModel().select(dialog.table.model().index(row, 0), flags)
        assert {entry.path for entry in dialog._selected_entries()} == {good, abnormal}
        assert dialog.compare_button.isEnabled()
        assert not dialog.batch_export_button.isEnabled()
        assert "参与只读比较" in dialog.detail.text()
        dialog.table.selectRow(next(row for row in range(dialog.table.rowCount())
                                    if dialog.table.item(row, 8).text() == unfinished.name))
        dialog.table.selectionModel().select(dialog.table.model().index(0, 0), flags)
        assert not dialog.compare_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_library_filters_sorts_selects_and_refreshes(recordings):
    app = W.QApplication.instance() or W.QApplication([])
    root, older, newest, interrupted, broken = recordings
    dialog = RecordingLibraryDialog(root)
    try:
        assert dialog.selected_path is None and not dialog.open_button.isEnabled()
        assert dialog.table.rowCount() == 4
        assert dialog.table.item(0, 8).text() == newest.name
        assert dialog.table.item(0, 0).text() == datetime.fromisoformat(
            "2026-10-19T02:10:00+00:00").astimezone().strftime("%Y-%m-%d %H:%M:%S")
        assert dialog.table.editTriggers() == W.QAbstractItemView.EditTrigger.NoEditTriggers
        dialog.search.setText("粗试样")
        visible = [row for row in range(dialog.table.rowCount()) if not dialog.table.isRowHidden(row)]
        assert len(visible) == 1
        dialog.table.selectRow(visible[0])
        assert dialog.open_button.isEnabled()
        dialog.search.setText("定长完成")
        assert not dialog.open_button.isEnabled()  # Hidden selection cannot be opened.
        dialog.search.clear()
        bad_row = next(row for row in range(dialog.table.rowCount())
                       if dialog.table.item(row, 8).text() == broken.name)
        dialog.table.selectRow(bad_row)
        assert not dialog.open_button.isEnabled()
        assert "JSONDecodeError" in dialog.detail.text()
        dialog.table.itemDoubleClicked.emit(dialog.table.item(bad_row, 8))
        assert dialog.selected_path is None
        dialog.table.sortItems(8, QtCore.Qt.SortOrder.AscendingOrder)
        selected_row = next(row for row in range(dialog.table.rowCount())
                            if dialog.table.item(row, 8).text() == older.name)
        dialog.table.selectRow(selected_row)
        dialog.open_button.click()
        assert dialog.selected_path == older
        assert dialog.result() == W.QDialog.DialogCode.Accepted
        make_recording(root, "fresh", "2026-10-20T01:00:00Z")
        dialog.refresh_button.click()
        assert dialog.table.rowCount() == 5
        assert dialog.table.item(0, 8).text() == "fresh"
        assert dialog.selected_path is None
    finally:
        dialog.close()
        dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("changes, expected", [
    ({"termination_reason": "acquisition_error", "error": "模拟中断"}, "异常（acquisition_error）：模拟中断"),
    ({"termination_reason": "future_reason"}, "future_reason"),
    ({"recording_options": {"group_number": "bad"}}, "不可打开"),
])
def test_library_preserves_end_reason_and_reports_invalid_metadata(tmp_path, changes, expected):
    path = make_recording(tmp_path, "record", None)
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(changes)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    entry, = list_recordings(tmp_path)
    assert entry.status == expected
    if expected == "不可打开":
        assert "组号" in entry.error


def test_finalized_acquisition_error_can_open_but_not_batch_export(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    path = make_recording(tmp_path, "采集异常", None)
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(termination_reason="acquisition_error", error="模拟设备断连")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    entry, = list_recordings(tmp_path)
    assert entry.finalized and entry.abnormal and not entry.batch_exportable

    dialog = RecordingLibraryDialog(tmp_path)
    try:
        dialog.table.selectRow(0)
        assert dialog.open_button.isEnabled()
        assert not dialog.batch_export_button.isEnabled()
        assert "采集异常" in dialog.detail.text()
        assert "可单独打开" in dialog.detail.text()
        dialog.open_button.click()
        assert dialog.selected_path == path
        assert dialog.result() == W.QDialog.DialogCode.Accepted
    finally:
        dialog.close()
        dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
