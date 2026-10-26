"""Layout and theme smoke tests for the redesigned workbench."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtWidgets as W

from labdaq import theme
from labdaq.gui import ChannelDialog, MainWindow, configure_application
from labdaq.ui_kit import CollapsibleSection


def test_every_palette_builds_a_complete_stylesheet():
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    for name in theme.available():
        palette = theme.palette(name)
        stylesheet = theme.stylesheet(palette)
        assert stylesheet.strip()
        assert "$" not in stylesheet
        assert "QComboBox::down-arrow" in stylesheet
        assert palette.accent.lower() in stylesheet.lower()


def test_workbench_pages_and_command_bar_build_at_minimum_size(tmp_path):
    from PySide6 import QtGui

    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    window.resize(1180, 780)
    window.show()
    try:
        for row in range(5):
            window.nav.setCurrentRow(row)
            app.processEvents()
            assert window.pages.currentIndex() == row
            assert window.title.text() == ["实时采集", "信号输出", "信号分析", "数据回看", "设备与协议"][row]
        assert [row for row in range(window.nav.count()) if not window.nav.item(row).isHidden()] == [0, 1, 3]
        window.nav.setCurrentRow(1)
        app.processEvents()
        assert window.output_panel.start_button.isVisible()
        assert window.output_panel.stop_button.isVisible()
        left = window.output_monitor.mapTo(window, QtCore.QPoint())
        right = window.output_panel.mapTo(window, QtCore.QPoint())
        assert left.x() + window.output_monitor.width() < right.x()
        assert window.output_plot.isVisible() and window.output_plot.height() >= 300
        assert .85 < window.output_monitor.width() / window.output_panel.width() < 1.2

        window.nav.setCurrentRow(0)
        app.processEvents()
        assert not window.output_panel.isVisible()
        assert window.preview_button.text() == "开始预览"
        assert window.record_button.text() == "开始记录"
        assert window.pause_record_button.text() == "暂停记录"
        assert window.stop_button.text() == "停止采集"
        assert window.status_label.objectName() == "statusPill"
        # Display options moved from the parameter panel into the waveform card.
        assert window.live_time_axis.isVisible()
        assert not hasattr(window, "live_value_mode")
        assert len(window.metric_mode_combos) == 4
        assert all(combo.isVisible() and combo.currentData() == "last" for combo in window.metric_mode_combos)
        assert window.value_decimals.isVisible()
        assert window.live_seconds.isVisible()
        assert type(window.rate).__name__ == "NoWheelComboBox" and not window.rate.isEditable()
        assert window.rate.currentData() == 5000.0
        old_rate = window.rate.currentIndex()
        local = QtCore.QPointF(window.rate.rect().center())
        wheel = QtGui.QWheelEvent(local, QtCore.QPointF(window.rate.mapToGlobal(local.toPoint())),
                                  QtCore.QPoint(), QtCore.QPoint(0, 120), QtCore.Qt.MouseButton.NoButton,
                                  QtCore.Qt.KeyboardModifier.NoModifier,
                                  QtCore.Qt.ScrollPhase.NoScrollPhase, False)
        W.QApplication.sendEvent(window.rate, wheel)
        assert window.rate.currentIndex() == old_rate and not wheel.isAccepted()
        window.rate.setCurrentIndex(window.rate.findData(500.0))
        assert window.live_seconds.minimum() == .001 and window.live_seconds.maximum() == 60
        assert window.live_seconds.decimals() == 3 and window.live_seconds.singleStep() == .001
        window.live_seconds.setValue(0)
        assert window.live_seconds.value() == .001
        window.live_seconds.setValue(61)
        assert window.live_seconds.value() == 60
        assert window.scenario.currentData() == "usb"
        assert not window.scenario.isVisible() and not window.input_button.isVisible()
        assert window.duration_mode.isVisible()
        assert window.notice.isVisible()
        assert window.zero_button.isVisible()
        assert window.undo_zero_button.isVisible()
        assert window.undo_zero_button.text() == "撤销调零"
        assert not window.findChildren(W.QToolButton, "cardMenu")
        assert not hasattr(window, "specimen") and not hasattr(window, "recording_notes")
        assert not hasattr(window, "device_button")
        assert not hasattr(window, "save_config_button") and not hasattr(window, "load_config_button")
        assert not hasattr(window, "directory_label") and not hasattr(window, "disk_space_label")
        assert window.directory_button.text() == "选择保存文件夹"
        assert isinstance(window.recording_sequence, W.QSpinBox)
        assert window.recording_sequence.value() == 1 and window.recording_sequence.minimum() == 1

        sections = [section for section in window.findChildren(CollapsibleSection)]
        assert len(sections) == 3
        assert "配置与存储" not in [section.toggle.text() for section in sections]
        storage = next(section for section in sections if section.toggle.text() == "采集参数")
        assert storage.body.isVisible()
        storage.set_expanded(False)
        app.processEvents()
        assert not storage.body.isVisible()
        storage.set_expanded(True)

        dialog = ChannelDialog(window)
        try:
            assert dialog.table.columnCount() == 12
            assert dialog.table.rowCount() == 4
            assert dialog.table.columnWidth(9) >= 90  # range combo needs its width
            assert dialog.table.columnWidth(3) >= 96
            assert dialog.table.cellWidget(0, 0).isChecked()
            dialog.cells[0][0].setChecked(False)
            dialog.bulk_unit.setCurrentText("Pa")
            dialog.bulk_range.setCurrentText("±1 V")
            dialog.bulk_coupling.setCurrentText("AC")
            dialog.bulk_iepe.setCurrentText("开启")
            dialog.bulk_button.click()
            assert dialog.cells[0][4].currentText() == "N"
            assert dialog.cells[0][9].currentText() == "±10 V"
            assert all(row[4].currentText() == "Pa" for row in dialog.cells[1:])
            assert all(row[9].currentText() == "±1 V" for row in dialog.cells[1:])
            assert all(row[10].currentText() == "AC" for row in dialog.cells[1:])
            assert all(row[11].isChecked() for row in dialog.cells[1:])
            dialog.cells[0][0].setChecked(True)
            dialog.cells[0][2].setValue(12)
            dialog.cells[0][3].setCurrentText("X-")
            dialog.cells[0][6].setValue(1.25)
            dialog.apply()
            assert window.channels[0].point_number == 12
            assert window.channels[0].direction == "X-"
            assert window.channels[0].engineering_offset == 1.25
            assert window.active_channels == (0, 1, 2, 3)
        finally:
            dialog.close()
            dialog.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_profile_dialogs_use_experiment_directory_and_save_creates_parents(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    window = MainWindow(core_endpoint=tmp_path / "unused.json")
    try:
        window.recording_root = tmp_path / "records"
        window.experiment.setText("实验/一")
        expected = tmp_path / "records" / "实验_一"
        destination = tmp_path / "new" / "nested" / "profile.json"
        save_calls = []
        monkeypatch.setattr(W.QFileDialog, "getSaveFileName",
                            lambda *args: save_calls.append(args) or (str(destination), "JSON 配置 (*.json)"))
        window.save_profile()
        assert save_calls[0][2] == str(expected / "实验配置.json")
        assert destination.is_file() and destination.parent.is_dir()
        assert window.notice.text() == f"配置已保存 · {destination}"
        assert window.notice.styleSheet() == f"color: {window.p.success};"

        load_calls = []
        monkeypatch.setattr(W.QFileDialog, "getOpenFileName",
                            lambda *args: load_calls.append(args) or ("", ""))
        window.load_profile()
        assert load_calls[0][2] == str(expected)
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_theme_names_and_tokens_are_consistent():
    assert theme.DEFAULT_THEME in theme.available()
    for name in theme.available():
        palette = theme.palette(name)
        assert len(palette.channels) == 4
        assert palette.radius >= 8
        assert theme.blend(palette.accent, "#FFFFFF", 0.0) == palette.accent
