"""Range export and statistics controls for recorded voltage data."""
from pathlib import Path

from PySide6 import QtCore, QtWidgets as W

from . import theme, ui_kit
from .recording import export_csv, load_session, session_statistics


DIALOG_MARGINS = (22, 20, 22, 20)


def _fill_time_formats(combo):
    for title, value in [("秒", "seconds"), ("毫秒", "milliseconds"), ("微秒", "microseconds"), ("样本号", "samples")]:
        combo.addItem(title, value)


def _dialog_layout(dialog: W.QDialog, spacing: int = 13) -> W.QVBoxLayout:
    layout = W.QVBoxLayout(dialog)
    layout.setContentsMargins(*DIALOG_MARGINS)
    layout.setSpacing(spacing)
    return layout


def _header(title: str, subtitle: str, icon_name: str | None = None) -> W.QWidget:
    """Page title plus one explanatory line, matching the main window headers."""
    p = theme.current()
    holder = W.QWidget()
    holder.setObjectName("transparentHost")
    row = W.QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)
    if icon_name:
        mark = W.QLabel()
        mark.setFixedSize(40, 40)
        mark.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(theme.icon(icon_name, p.accent, 20).pixmap(20, 20))
        mark.setStyleSheet(
            f"background: {p.accent_soft}; border: 1px solid {p.border};"
            f" border-radius: {p.radius_small}px;")
        row.addWidget(mark)
    texts = W.QVBoxLayout()
    texts.setContentsMargins(0, 0, 0, 0)
    texts.setSpacing(2)
    texts.addWidget(ui_kit.label(title, "pageTitle"))
    texts.addWidget(ui_kit.label(subtitle, "pageSubtitle", True))
    row.addLayout(texts, 1)
    return holder


def _notice(text: str = "", tone: str = "neutral") -> W.QLabel:
    bar = ui_kit.label(text, "notice", True)
    bar.setProperty("tone", tone)
    bar.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    return bar


def _metric_strip(items: list[tuple[str, str]]) -> W.QFrame:
    """A compact light strip of label / value pairs."""
    strip = W.QFrame()
    strip.setObjectName("statusStrip")
    row = W.QHBoxLayout(strip)
    row.setContentsMargins(16, 12, 16, 12)
    row.setSpacing(18)
    for index, (title, value) in enumerate(items):
        if index:
            row.addWidget(theme.vdivider(strip))
        cell = W.QWidget()
        cell.setObjectName("transparentHost")
        column = W.QVBoxLayout(cell)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        column.addWidget(ui_kit.field_label(title))
        column.addWidget(ui_kit.label(value, "valueMono"))
        row.addWidget(cell, 1)
    return strip


def _options_card(fields: list[tuple[str, W.QWidget]]) -> W.QFrame:
    frame, layout = ui_kit.card(margins=(16, 13, 16, 14), spacing=11)
    layout.addWidget(ui_kit.section_title("导出设置", "sliders"))
    row = W.QHBoxLayout()
    row.setSpacing(14)
    for title, widget in fields:
        row.addWidget(ui_kit.field(title, widget), 1)
    layout.addLayout(row)
    return frame


def _dialog_footer(layout: W.QVBoxLayout, primary_text: str,
                   icon_name: str) -> tuple[W.QPushButton, W.QPushButton]:
    """Shared footer: cancel then primary action, both right aligned."""
    p = theme.current()
    layout.addWidget(theme.divider())
    row = W.QHBoxLayout()
    row.setSpacing(8)
    row.addStretch(1)
    cancel = W.QPushButton("取消")
    cancel.setObjectName("ghost")
    row.addWidget(cancel)
    primary = W.QPushButton(primary_text)
    primary.setObjectName("primary")
    primary.setIcon(theme.icon(icon_name, p.accent_text, 15))
    primary.setIconSize(QtCore.QSize(15, 15))
    row.addWidget(primary)
    layout.addLayout(row)
    return primary, cancel


def export_recording_batch(sources, directory, *, values="both", time_format="seconds", delimiter=","):
    """Export complete finalized groups in order, checking all destinations first."""
    if not sources:
        raise ValueError("请先选择至少一组记录")
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError("请选择已存在的导出目录")
    extension = ".txt" if delimiter == "\t" else ".csv"
    sessions, outputs = [], []
    destinations = set()
    for source in sources:
        path = Path(source.path if hasattr(source, "path") else source)
        try:
            session = load_session(path)
            if not session.metadata["finalized"]:
                raise ValueError("记录尚未正常收尾，不能批量导出；活动记录请先结束，异常中断记录可单独打开后导出")
        except Exception as exc:
            raise ValueError(f"记录组 {path.name}：{exc}") from exc
        output = directory / (path.name + extension)
        key = str(output).casefold()
        if key in destinations:
            raise ValueError(f"多个记录组的输出文件同名：{output.name}，请分开导出")
        if output.exists() or output.is_symlink():
            raise ValueError(f"输出文件已存在：{output.name}，请选择其他目录")
        destinations.add(key)
        sessions.append(session)
        outputs.append(output)
    for session in sessions:
        source = session.path.resolve()
        if any(output.is_relative_to(source) for output in outputs):
            raise ValueError(f"导出目录不能位于原始记录组 {session.path.name} 内")
    completed = []
    for session, output in zip(sessions, outputs):
        try:
            export_csv(session, output, values=values, time_format=time_format,
                       delimiter=delimiter, overwrite=False)
        except Exception as exc:
            partial = "；失败组的输出文件可能不完整" if output.exists() else ""
            raise RuntimeError(f"记录组 {session.path.name} 导出失败：{exc}。"
                               f"已成功 {len(completed)} / {len(sessions)} 组，成功文件已保留{partial}") from exc
        completed.append(output)
    return completed


class _DataJob(QtCore.QThread):
    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation
        self.result = None
        self.error = ""

    def run(self):
        try:
            self.result = self.operation()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class ExportDialog(W.QDialog):
    def __init__(self, reader, start, stop, channel_indices, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出所选数据")
        self.setMinimumWidth(540)
        self.reader, self.start, self.stop = reader, start, stop
        self.channel_indices = channel_indices
        self.output_path = None
        self.job = None
        layout = _dialog_layout(self)

        channels = "、".join(f"AI {index}" for index in channel_indices)
        layout.addWidget(_header(
            "导出所选数据",
            "把回看区间写成 CSV 或 TXT；首行保留原配置、本次区间与通道说明。",
            "export"))
        layout.addWidget(_metric_strip([
            ("样本区间", f"[{start:,}, {stop:,}) · {stop - start:,} 帧"),
            ("物理通道", channels or "—"),
            ("时间列", "保留原记录位置"),
        ]))

        self.file_format = W.QComboBox()
        self.file_format.addItems(["CSV（逗号分隔）", "TXT（制表符分隔）"])
        self.values = W.QComboBox()
        for title, value in [("原始电压与工程值", "both"), ("仅原始电压 V", "raw"), ("仅工程值", "engineering")]:
            self.values.addItem(title, value)
        self.time_format = W.QComboBox()
        _fill_time_formats(self.time_format)
        layout.addWidget(_options_card([
            ("文件类型", self.file_format),
            ("数据内容", self.values),
            ("时间列", self.time_format),
        ]))

        self.status = _notice("文件首行包含原配置和本次导出区间、通道说明。", "info")
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.save_button, self.cancel_button = _dialog_footer(layout, "选择文件并导出", "export")
        self.save_button.clicked.connect(self.choose_output)
        self.cancel_button.clicked.connect(self.reject)

    def choose_output(self):
        extension = ".txt" if self.file_format.currentIndex() else ".csv"
        source = Path(self.reader.path)
        suggested = source.with_suffix(extension) if source.is_file() else source / ("export" + extension)
        path, _ = W.QFileDialog.getSaveFileName(self, "保存导出数据", str(suggested), f"数据 (*{extension})")
        if path:
            self.begin_export(path)

    def begin_export(self, path):
        options = {"start": self.start, "stop": self.stop, "channel_indices": self.channel_indices,
                   "values": self.values.currentData(), "time_format": self.time_format.currentData(),
                   "delimiter": "\t" if self.file_format.currentIndex() else ","}
        self.job = _DataJob(lambda: export_csv(self.reader, path, **options), self)
        self.job.finished.connect(self._finished)
        for widget in (self.save_button, self.cancel_button, self.file_format, self.values,
                       self.time_format):
            widget.setEnabled(False)
        self.status.setText("正在分块导出…采集核心和界面刷新会继续。")
        theme.set_tone(self.status, "info")
        self.job.start()

    def _finished(self):
        job, self.job = self.job, None
        job.deleteLater()
        if job.error:
            self.status.setText(f"导出失败：{job.error}")
            theme.set_tone(self.status, "danger")
            for widget in (self.save_button, self.cancel_button, self.file_format, self.values,
                           self.time_format):
                widget.setEnabled(True)
        else:
            self.output_path = job.result
            self.accept()

    def reject(self):
        if self.job is None or not self.job.isRunning():
            super().reject()

    def closeEvent(self, event):
        if self.job is not None and self.job.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)


class BatchExportDialog(W.QDialog):
    """Apply one export configuration to complete, individually stored groups."""

    def __init__(self, paths_or_readers, parent=None):
        super().__init__(parent)
        self.sources = list(paths_or_readers)
        self.output_paths = []
        self.job = None
        self.setWindowTitle("批量导出记录")
        self.setMinimumWidth(540)
        layout = _dialog_layout(self)

        layout.addWidget(_header(
            "批量导出记录",
            "把选中的已收尾记录组按同一配置逐组导出，各组保留全部样本与通道。",
            "export"))
        layout.addWidget(_metric_strip([
            ("记录组数", f"{len(self.sources)} 组"),
            ("导出范围", "全部样本 · 各自全部通道"),
            ("文件命名", "与记录组同名"),
        ]))

        self.file_format = W.QComboBox()
        self.file_format.addItems(["CSV（逗号分隔）", "TXT（制表符分隔）"])
        self.values = W.QComboBox()
        for title, value in [("原始电压与工程值", "both"), ("仅原始电压 V", "raw"), ("仅工程值", "engineering")]:
            self.values.addItem(title, value)
        self.time_format = W.QComboBox()
        _fill_time_formats(self.time_format)
        layout.addWidget(_options_card([
            ("文件类型", self.file_format),
            ("数据内容", self.values),
            ("时间列", self.time_format),
        ]))

        layout.addWidget(ui_kit.label(
            "每组生成同名 CSV/TXT 文件，已有同名文件时停止并提示另选目录。", "muted", True))
        self.status = _notice("仅可导出已经收尾的记录；导出逐组分块执行。", "warning")
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.save_button, self.cancel_button = _dialog_footer(layout, "选择目录并导出", "export")
        self.save_button.clicked.connect(self.choose_output)
        self.cancel_button.clicked.connect(self.reject)

    def choose_output(self):
        source = self.sources[0] if self.sources else Path.cwd()
        path = Path(source.path if hasattr(source, "path") else source)
        directory = W.QFileDialog.getExistingDirectory(self, "选择批量导出目录", str(path.parent))
        if directory:
            self.begin_export(directory)

    def begin_export(self, directory):
        options = {"values": self.values.currentData(), "time_format": self.time_format.currentData(),
                   "delimiter": "\t" if self.file_format.currentIndex() else ","}
        self.job = _DataJob(lambda: export_recording_batch(self.sources, directory, **options), self)
        self.job.finished.connect(self._finished)
        for widget in (self.save_button, self.cancel_button, self.file_format, self.values,
                       self.time_format):
            widget.setEnabled(False)
        self.status.setText(f"正在逐组分块导出 {len(self.sources)} 组记录…采集核心和界面刷新会继续。")
        theme.set_tone(self.status, "info")
        self.job.start()

    def _finished(self):
        job, self.job = self.job, None
        job.deleteLater()
        for widget in (self.file_format, self.values, self.time_format):
            widget.setEnabled(True)
        if job.error:
            self.save_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
            self.status.setText(f"批量导出未完成：{job.error}")
            theme.set_tone(self.status, "danger")
        else:
            self.output_paths = job.result
            self.status.setText(f"已成功导出 {len(self.output_paths)} 组记录到：\n{self.output_paths[0].parent}")
            theme.set_tone(self.status, "success")
            self.save_button.hide()
            self.cancel_button.setText("关闭")
            self.cancel_button.setEnabled(True)
            self.cancel_button.setObjectName("primary")
            theme.repolish(self.cancel_button)

    def reject(self):
        if self.job is None or not self.job.isRunning():
            super().reject()

    def closeEvent(self, event):
        if self.job is not None and self.job.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)


class StatisticsDialog(W.QDialog):
    def __init__(self, reader, start, stop, channel_indices, parent=None):
        super().__init__(parent)
        self.setWindowTitle("所选数据统计")
        self.resize(980, 380)
        self.setMinimumSize(760, 340)
        self.reader, self.start, self.stop, self.channel_indices = reader, start, stop, channel_indices
        self.job = None
        p = theme.current()
        layout = _dialog_layout(self)

        layout.addWidget(_header(
            "所选数据统计",
            "对当前回看区间逐通道计算统计量，表格可直接复制到外部软件。",
            "stats"))
        self.status = _notice(
            f"样本区间 [{start:,}, {stop:,}) · {stop - start:,} 帧；标准差采用总体定义（ddof=0）。",
            "info")
        layout.addWidget(self.status)

        self.values = W.QComboBox()
        self.values.addItem("工程值", "engineering")
        self.values.addItem("原始电压 V", "raw")
        picker = W.QHBoxLayout()
        picker.setSpacing(10)
        picker.addWidget(ui_kit.field_label("统计内容"))
        picker.addWidget(self.values)
        picker.addStretch(1)
        layout.addLayout(picker)

        self.table = W.QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(["通道", "单位", "帧数", "最小值", "最大值", "峰峰值", "均值", "标准差", "RMS"])
        self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, W.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setHighlightSections(False)
        layout.addWidget(self.table, 1)

        layout.addWidget(theme.divider())
        row = W.QHBoxLayout()
        row.setSpacing(8)
        self.copy_button = W.QPushButton("复制统计表")
        self.copy_button.setIcon(theme.icon("save", p.text_muted, 15))
        self.copy_button.setIconSize(QtCore.QSize(15, 15))
        self.copy_button.clicked.connect(self.copy_table)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        self.close_button = W.QPushButton("关闭")
        self.close_button.setObjectName("primary")
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.values.currentIndexChanged.connect(self.calculate)
        self.calculate()

    def calculate(self, *_):
        mode = self.values.currentData()
        self.job = _DataJob(lambda: session_statistics(self.reader, start=self.start, stop=self.stop,
                                                      channel_indices=self.channel_indices, values=mode), self)
        self.job.finished.connect(self._finished)
        for widget in (self.values, self.copy_button, self.close_button):
            widget.setEnabled(False)
        self.job.start()

    def _finished(self):
        job, self.job = self.job, None
        job.deleteLater()
        self.values.setEnabled(True)
        self.close_button.setEnabled(True)
        if job.error:
            self.status.setText(f"统计失败：{job.error}")
            theme.set_tone(self.status, "danger")
            return
        self.table.setRowCount(len(job.result))
        for row, stat in enumerate(job.result):
            values = [f"AI {stat['index']} · {stat['name']}", stat["unit"], str(stat["count"])]
            values += [f"{stat[key]:.10g}" for key in ("min", "max", "delta", "mean", "std", "rms")]
            for col, value in enumerate(values):
                item = W.QTableWidgetItem(value)
                if col >= 2:
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight |
                                          QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
        theme.set_tone(self.status, "info")
        self.copy_button.setEnabled(True)

    def copy_table(self):
        rows = [[self.table.horizontalHeaderItem(i).text() for i in range(self.table.columnCount())]]
        rows += [[self.table.item(row, col).text() for col in range(self.table.columnCount())]
                 for row in range(self.table.rowCount())]
        W.QApplication.clipboard().setText("\n".join("\t".join(row) for row in rows))

    def reject(self):
        if self.job is None or not self.job.isRunning():
            super().reject()

    def closeEvent(self, event):
        if self.job is not None and self.job.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)
