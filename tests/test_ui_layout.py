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
        window.nav.setCurrentRow(1)
        app.processEvents()
        assert window.output_panel.start_button.isVisible()
        assert window.output_panel.stop_button.isVisible()

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
        assert window.live_value_mode.isVisible()
        assert window.value_decimals.isVisible()
        assert window.live_seconds.isVisible()
        assert isinstance(window.rate, W.QComboBox) and not window.rate.isEditable()
        assert window.rate.currentData() == 5000.0
        assert window.scenario.currentData() == "usb"
        assert not window.scenario.isVisible() and not window.input_button.isVisible()
        assert window.duration_mode.isVisible()
        assert window.notice.isVisible()
        assert window.zero_button.isVisible()
        assert window.undo_zero_button.isVisible()
        assert window.undo_zero_button.text() == "撤销调零"
        assert not window.findChildren(W.QToolButton, "cardMenu")

        sections = [section for section in window.findChildren(CollapsibleSection)]
        assert len(sections) == 4
        storage = next(section for section in sections if section.toggle.text() == "配置与存储")
        assert not storage.body.isVisible()
        storage.set_expanded(True)
        app.processEvents()
        assert storage.body.isVisible()
        storage.set_expanded(False)

        dialog = ChannelDialog(window)
        try:
            assert dialog.table.columnCount() == 9
            assert dialog.table.rowCount() == 4
            assert dialog.table.columnWidth(6) >= 90  # range combo needs its width
            assert dialog.table.cellWidget(0, 0).isChecked()
        finally:
            dialog.close()
            dialog.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
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
