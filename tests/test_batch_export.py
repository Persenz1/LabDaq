"""Full-group batch export with simulated records and read-only source files."""
import csv
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6 import QtCore, QtWidgets as W
from PySide6.QtTest import QTest

from labdaq import data_dialog
from labdaq.data_dialog import BatchExportDialog, export_recording_batch
from labdaq.processing import ChannelSettings
from labdaq.recording import SessionRecorder, load_session
from labdaq.recording_library import RecordingLibraryDialog


def make_group(root, name, indices):
    path = root / name
    samples = np.column_stack([np.arange(5) / 10 + index for index in indices])
    channels = [ChannelSettings(index, f"通道 {index}", sensitivity_mv_per_unit=500) for index in indices]
    with SessionRecorder(path, 4, channels, experiment="模拟批量导出") as recorder:
        recorder.write(samples)
    return path, samples


def read_export(path, delimiter):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
        rows = list(csv.reader(handle, delimiter=delimiter))
    return metadata, rows[0], np.asarray(rows[1:], dtype=float)


@pytest.mark.parametrize("delimiter,values,time_format,scale,extension", [
    (",", "both", "seconds", .25, ".csv"),
    ("\t", "engineering", "milliseconds", 250, ".txt"),
    (",", "raw", "samples", 1, ".csv"),
])
def test_batch_uses_each_groups_complete_channels_and_interval(tmp_path, delimiter, values, time_format, scale, extension):
    first, raw_first = make_group(tmp_path, "001", [0, 3])
    second, raw_second = make_group(tmp_path, "002", [2])
    output = tmp_path / "exported"
    output.mkdir()
    originals = {path: (path / "raw.f64").read_bytes() for path in (first, second)}
    outputs = export_recording_batch([first, load_session(second)], output, delimiter=delimiter,
                                     values=values, time_format=time_format)
    assert outputs == [output / ("001" + extension), output / ("002" + extension)]
    for path, source, raw in zip(outputs, (first, second), (raw_first, raw_second)):
        metadata, headings, table = read_export(path, delimiter)
        np.testing.assert_array_equal(table[:, 0], np.arange(5) * scale)
        expected = np.column_stack((raw, raw * 2)) if values == "both" else raw * (2 if values == "engineering" else 1)
        np.testing.assert_allclose(table[:, 1:], expected)
        assert len(headings) == expected.shape[1] + 1
        assert metadata["export"]["exported_sample_count"] == 5
        assert metadata["export"]["channel_indices"] == [channel.index for channel in load_session(source).channels]
        assert (source / "raw.f64").read_bytes() == originals[source]


def test_batch_preflights_all_outputs_and_rejects_unfinished_groups(tmp_path, monkeypatch):
    first, _ = make_group(tmp_path, "001", [0])
    second, _ = make_group(tmp_path, "002", [1])
    output = tmp_path / "exported"
    output.mkdir()
    conflict = output / "002.csv"
    conflict.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ValueError, match="002.csv"):
        export_recording_batch([first, second], output)
    assert not (output / "001.csv").exists()
    assert conflict.read_text(encoding="utf-8") == "do not overwrite"
    meta_path = second / "metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["finalized"] = False
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    def no_raw_reads(*_args, **_kwargs):
        pytest.fail("preflight must not read raw samples")

    monkeypatch.setattr(type(load_session(first)), "read", no_raw_reads)
    with pytest.raises(ValueError, match="002.*尚未正常收尾"):
        export_recording_batch([first, second], output)
    assert not (output / "001.csv").exists()


def test_batch_duplicate_basenames_and_source_directory_are_rejected(tmp_path):
    first, _ = make_group(tmp_path / "A", "same", [0])
    second, _ = make_group(tmp_path / "B", "same", [1])
    output = tmp_path / "exported"
    output.mkdir()
    with pytest.raises(ValueError, match="同名"):
        export_recording_batch([first, second], output)
    with pytest.raises(ValueError, match="原始记录组"):
        export_recording_batch([first], first)
    assert list(output.iterdir()) == []


def test_batch_failure_names_group_keeps_successes_and_stops(tmp_path, monkeypatch):
    paths = [make_group(tmp_path, name, [index])[0] for index, name in enumerate(("001", "002", "003"))]
    output = tmp_path / "exported"
    output.mkdir()
    original_export = data_dialog.export_csv
    calls = []

    def fail_second(session, destination, **kwargs):
        calls.append(session.path.name)
        if session.path == paths[1]:
            raise OSError("模拟磁盘写入失败")
        return original_export(session, destination, **kwargs)

    monkeypatch.setattr(data_dialog, "export_csv", fail_second)
    with pytest.raises(RuntimeError, match="记录组 002 导出失败.*模拟磁盘写入失败.*已成功 1 / 3 组"):
        export_recording_batch(paths, output)
    assert calls == ["001", "002"]
    assert (output / "001.csv").is_file()
    assert not (output / "003.csv").exists()


def test_library_multiple_selection_keeps_open_current_and_disables_invalid_batch(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    first, _ = make_group(tmp_path, "001", [0])
    second, _ = make_group(tmp_path, "002", [1])
    unfinished, _ = make_group(tmp_path, "003", [2])
    metadata_path = unfinished / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["finalized"] = False
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    dialog = RecordingLibraryDialog(tmp_path)
    try:
        rows = {dialog.table.item(row, 7).text(): row for row in range(dialog.table.rowCount())}
        selection = dialog.table.selectionModel()
        flags = QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows
        for source in (first, second):
            selection.select(dialog.table.model().index(rows[source.name], 0), flags)
        dialog.table.setCurrentCell(rows[first.name], 0, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        assert len(dialog._selected_entries()) == 2
        assert dialog.batch_export_button.isEnabled()
        assert dialog.open_button.isEnabled()
        assert dialog._selected_entry().path == first
        selection.select(dialog.table.model().index(rows[unfinished.name], 0), flags)
        assert not dialog.batch_export_button.isEnabled()
        assert "尚未收尾" in dialog.detail.text()
        dialog.open_button.click()
        assert dialog.selected_path == first
    finally:
        dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_batch_dialog_background_success_and_error(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    path, _ = make_group(tmp_path, "001", [0, 2])
    output = tmp_path / "exported"
    output.mkdir()
    dialogs = [BatchExportDialog([path]), BatchExportDialog([path])]
    try:
        for dialog in dialogs:
            dialog.file_format.setCurrentIndex(1)
            dialog.values.setCurrentIndex(dialog.values.findData("raw"))
            dialog.time_format.setCurrentIndex(dialog.time_format.findData("samples"))
            dialog.begin_export(output)
            deadline = time.monotonic() + 5
            while dialog.job is not None and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(10)
            app.processEvents()
            assert dialog.job is None
        assert dialogs[0].output_paths == [output / "001.txt"]
        assert "已成功导出 1 组" in dialogs[0].status.text()
        assert dialogs[1].output_paths == []
        assert "未完成" in dialogs[1].status.text() and "已存在" in dialogs[1].status.text()
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
