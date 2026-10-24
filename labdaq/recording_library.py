"""Find recorded sessions from metadata and file lengths, without reading samples."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets as W

from . import theme, ui_kit
from .recording import has_acquisition_error, load_session, update_recording_details
from .data_dialog import BatchExportDialog


DIALOG_MARGINS = (22, 20, 22, 20)


@dataclass
class RecordingEntry:
    path: Path
    relative_path: str = ""
    started_at: datetime | None = None
    experiment: str = ""
    specimen: str = ""
    condition: str = ""
    group_number: int | None = None
    duration_seconds: float | None = None
    channel_count: int | None = None
    status: str = "不可打开"
    notes: str = ""
    error: str | None = None
    finalized: bool = False
    abnormal: bool = False

    @property
    def directory_name(self):
        return self.relative_path or self.path.name

    @property
    def batch_exportable(self):
        return self.error is None and self.finalized and not self.abnormal

    @property
    def comparable(self):
        return self.error is None and self.finalized

    def matches(self, query):
        return query.casefold() in " ".join(
            (self.experiment, self.specimen, self.condition, self.notes,
             self.directory_name, self.status)).casefold()

    @property
    def sort_key(self):
        return (self.started_at is not None,
                self.started_at.timestamp() if self.started_at else 0,
                self.directory_name.casefold())


def list_recordings(root) -> list[RecordingEntry]:
    """Inspect legacy direct sessions and native sessions one experiment level deep."""
    root = Path(root)
    if not root.exists():
        return []
    paths = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        if (path / "metadata.json").is_file():
            paths.append(path)
        else:
            paths.extend(candidate for candidate in path.iterdir()
                         if candidate.is_dir() and (candidate / "metadata.json").is_file())
    entries = []
    for path in paths:
        relative_path = path.relative_to(root).as_posix()
        entry = RecordingEntry(path, relative_path)
        try:
            session = load_session(path)
            metadata = session.metadata
            entry.finalized = metadata["finalized"]
            if not math.isfinite(session.sample_rate) or session.sample_rate <= 0:
                raise ValueError("记录的采样率必须为正的有限数")
            duration = session.duration
            options = metadata.get("recording_options", {})
            entry.experiment = str(metadata.get("experiment", ""))
            entry.group_number = options.get("group_number")
            if (entry.group_number is not None and
                    (type(entry.group_number) is not int or entry.group_number < 0)):
                raise ValueError("记录组号必须为非负整数")
            entry.specimen = str(options.get("specimen") or "")
            entry.condition = str(options.get("condition") or "")
            entry.notes = str(options.get("notes") or "")
            entry.duration_seconds = duration
            entry.channel_count = len(metadata["channels"])
            started_at = metadata.get("started_at_utc")
            if started_at:
                entry.started_at = datetime.fromisoformat(started_at)
                if entry.started_at.tzinfo is None:
                    entry.started_at = entry.started_at.replace(tzinfo=timezone.utc)
            reason = metadata.get("termination_reason")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("记录结束状态必须为文本")
            entry.abnormal = has_acquisition_error(metadata)
            if not metadata["finalized"]:
                entry.status = "未正常收尾"
            elif entry.abnormal:
                entry.status = f"异常（{reason or '未提供原因'}）"
            else:
                entry.status = {"duration_complete": "定长完成", "user_stop": "手动结束",
                                "end_of_file": "回放结束"}.get(reason, reason or "已结束（未提供原因）")
            if metadata.get("error"):
                entry.status += f"：{metadata['error']}"
        except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError, AttributeError) as exc:
            entry = RecordingEntry(path, relative_path, error=f"{type(exc).__name__}: {exc}")
        entries.append(entry)
    return sorted(entries, key=lambda entry: entry.sort_key, reverse=True)


class _SortItem(W.QTableWidgetItem):
    def __init__(self, text, sort_value):
        super().__init__(text)
        self.sort_value = sort_value

    def __lt__(self, other):
        return self.sort_value < other.sort_value


def _dialog_layout(dialog: W.QDialog, spacing: int = 13) -> W.QVBoxLayout:
    layout = W.QVBoxLayout(dialog)
    layout.setContentsMargins(*DIALOG_MARGINS)
    layout.setSpacing(spacing)
    return layout


def _header(title: str, subtitle: str, icon_name: str | None = None) -> W.QWidget:
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


def _magnifier_icon(color: str, size: int = 14) -> QtGui.QIcon:
    """Small search glyph, since the shared icon set has no magnifier entry."""
    pixmap = QtGui.QPixmap(size * 2, size * 2)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2.0)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(1.7)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.scale(size / 24.0, size / 24.0)
    painter.drawEllipse(QtCore.QPointF(10.5, 10.5), 6.2, 6.2)
    painter.drawLine(QtCore.QPointF(15.2, 15.2), QtCore.QPointF(20.4, 20.4))
    painter.end()
    return QtGui.QIcon(pixmap)


class RecordingDetailsDialog(W.QDialog):
    def __init__(self, entry: RecordingEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle("编辑实验信息")
        self.setMinimumWidth(540)
        layout = _dialog_layout(self)
        layout.addWidget(_header("编辑实验信息", f"{entry.path.name} · 第 {entry.group_number or 0:03d} 组", "library"))
        layout.addWidget(ui_kit.label(
            "只修改描述信息，不改记录目录、组号、原始波形、采样配置或结束状态。", "notice", True))
        self.experiment = W.QLineEdit(entry.experiment)
        self.specimen = W.QLineEdit(entry.specimen)
        self.condition = W.QLineEdit(entry.condition)
        self.notes = W.QPlainTextEdit(entry.notes)
        self.notes.setFixedHeight(120)
        self.notes.setTabChangesFocus(True)
        for title, widget in (("实验名称", self.experiment), ("试样", self.specimen),
                              ("工况", self.condition), ("本组备注", self.notes)):
            layout.addWidget(ui_kit.field(title, widget))
        if entry.abnormal:
            layout.addWidget(ui_kit.label(
                "此记录存在采集异常；补充说明不会清除异常或使其允许批量导出。", "notice", True))
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Save |
                                     W.QDialogButtonBox.StandardButton.Cancel)
        save = buttons.button(W.QDialogButtonBox.StandardButton.Save)
        save.setText("保存信息")
        save.setObjectName("primary")
        buttons.button(W.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def values(self):
        return {"experiment": self.experiment.text().strip(),
                "specimen": self.specimen.text().strip(),
                "condition": self.condition.text().strip(),
                "notes": self.notes.toPlainText()}

    def accept(self):
        if not self.experiment.text().strip():
            W.QMessageBox.information(self, "实验信息", "实验名称不能为空。")
            return
        super().accept()


class RecordingLibraryDialog(W.QDialog):
    """Manage descriptive metadata and open completed recordings without reading all samples."""

    details_changed = QtCore.Signal(object)

    def __init__(self, recording_root, parent=None):
        super().__init__(parent)
        p = theme.current()
        self.recording_root = Path(recording_root)
        self.selected_path: Path | None = None
        self.entries = []
        self._comparison_windows = []
        self.setWindowTitle("记录库")
        self.resize(1280, 700)
        self.setMinimumSize(920, 560)
        layout = _dialog_layout(self)

        layout.addWidget(_header("记录库", f"记录目录：{self.recording_root}", "library"))

        search_row = W.QHBoxLayout()
        search_row.setSpacing(8)
        self.search = W.QLineEdit()
        self.search.setPlaceholderText("搜索实验、试样、工况、备注、目录名或结束状态")
        self.search.setClearButtonEnabled(False)
        self.search.addAction(_magnifier_icon(p.text_faint, 14),
                              W.QLineEdit.ActionPosition.LeadingPosition)
        self._clear_action = self.search.addAction(
            theme.icon("error", p.text_faint, 13),
            W.QLineEdit.ActionPosition.TrailingPosition)
        self._clear_action.setToolTip("清除搜索")
        self._clear_action.triggered.connect(lambda: self.search.clear())
        self._clear_action.setVisible(False)
        self.search.textChanged.connect(self._sync_clear_action)
        self.refresh_button = W.QPushButton("刷新")
        self.refresh_button.setIcon(theme.icon("refresh", p.text_muted, 15))
        self.refresh_button.setIconSize(QtCore.QSize(15, 15))
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.refresh_button)
        layout.addLayout(search_row)

        self.table = W.QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["开始时间（本地）", "实验名", "试样 / 工况", "组号", "时长（秒）",
             "通道数", "结束状态", "备注", "目录名"])
        self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(W.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(31)
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(7, W.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setHighlightSections(False)
        for column, width in enumerate((155, 140, 170, 55, 90, 65, 150, 220, 220)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)

        detail_card, detail_layout = ui_kit.card(margins=(16, 11, 16, 12), spacing=7)
        detail_head = W.QHBoxLayout()
        detail_head.setSpacing(7)
        self.detail_dot = ui_kit.status_dot(p.text_faint, 8)
        detail_head.addWidget(self.detail_dot)
        detail_head.addWidget(ui_kit.field_label("记录详情"))
        detail_head.addStretch()
        detail_layout.addLayout(detail_head)
        self.detail = ui_kit.label("", "muted", True)
        self.detail.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setMinimumHeight(38)
        detail_layout.addWidget(self.detail)
        layout.addWidget(detail_card)

        layout.addWidget(theme.divider())
        footer = W.QHBoxLayout()
        footer.setSpacing(8)
        self.compare_button = W.QPushButton("比较所选")
        self.compare_button.setIcon(theme.icon("wave", p.text_muted, 15))
        self.compare_button.setIconSize(QtCore.QSize(15, 15))
        footer.addWidget(self.compare_button)
        self.edit_button = W.QPushButton("编辑信息")
        self.edit_button.setIcon(theme.icon("settings", p.text_muted, 15))
        self.edit_button.setIconSize(QtCore.QSize(15, 15))
        footer.addWidget(self.edit_button)
        self.batch_export_button = W.QPushButton("批量导出")
        self.batch_export_button.setIcon(theme.icon("export", p.text_muted, 15))
        self.batch_export_button.setIconSize(QtCore.QSize(15, 15))
        footer.addWidget(self.batch_export_button)
        footer.addStretch(1)
        cancel_button = W.QPushButton("取消")
        cancel_button.setObjectName("ghost")
        footer.addWidget(cancel_button)
        self.open_button = W.QPushButton("打开")
        self.open_button.setObjectName("primary")
        self.open_button.setIcon(theme.icon("open", p.accent_text, 15))
        self.open_button.setIconSize(QtCore.QSize(15, 15))
        footer.addWidget(self.open_button)
        layout.addLayout(footer)
        cancel_button.clicked.connect(self.reject)
        self.open_button.clicked.connect(self._open_selected)
        self.compare_button.clicked.connect(self._compare_selected)
        self.edit_button.clicked.connect(self._edit_selected)
        self.batch_export_button.clicked.connect(self._batch_export)
        self.table.itemSelectionChanged.connect(self._update_selection)
        self.table.currentCellChanged.connect(self._update_selection)
        self.table.itemDoubleClicked.connect(lambda _item: self._open_selected())
        self.search.textChanged.connect(self._filter_rows)
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh()

    def _sync_clear_action(self, text):
        self._clear_action.setVisible(bool(text.strip()))

    def refresh(self):
        current = self._selected_entry()
        current_path = current.path if current else None
        selected_paths = {entry.path for entry in self._selected_entries()}
        self.entries = list_recordings(self.recording_root)
        self.selected_path = None
        self.table.clearSelection()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            started = entry.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if entry.started_at else "—"
            sample_condition = " / ".join(value for value in (entry.specimen, entry.condition) if value) or "—"
            values = (started, entry.experiment or "—", sample_condition,
                      str(entry.group_number) if entry.group_number is not None else "—",
                      f"{entry.duration_seconds:.3f}" if entry.duration_seconds is not None else "—",
                      str(entry.channel_count) if entry.channel_count is not None else "—",
                      entry.status, entry.error or entry.notes, entry.directory_name)
            sorts = (entry.sort_key, entry.experiment.casefold(), sample_condition.casefold(),
                     entry.group_number or 0,
                     entry.duration_seconds if entry.duration_seconds is not None else -1,
                     entry.channel_count or 0, entry.status, entry.notes.casefold(),
                     entry.directory_name.casefold())
            for column, (value, key) in enumerate(zip(values, sorts)):
                item = _SortItem(value, key)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, entry)
                item.setToolTip(entry.error or value)
                if column in (3, 4, 5):
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight |
                                          QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
        self.table.clearSelection()
        self._filter_rows()
        selection_flags = (QtCore.QItemSelectionModel.SelectionFlag.Select |
                           QtCore.QItemSelectionModel.SelectionFlag.Rows)
        for row in range(self.table.rowCount()):
            entry = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            if entry.path in selected_paths and not self.table.isRowHidden(row):
                self.table.selectionModel().select(self.table.model().index(row, 0), selection_flags)
            if entry.path == current_path and not self.table.isRowHidden(row):
                self.table.setCurrentCell(row, 0)
        self._update_selection()

    def _selected_entry(self):
        row = self.table.currentRow()
        if row < 0 or self.table.isRowHidden(row) or not self.table.selectionModel().isRowSelected(
                row, QtCore.QModelIndex()):
            return None
        return self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)

    def _selected_entries(self):
        rows = self.table.selectionModel().selectedRows()
        return [self.table.item(index.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
                for index in sorted(rows, key=lambda index: index.row())
                if not self.table.isRowHidden(index.row())]

    def _filter_rows(self):
        query = self.search.text().strip()
        visible = 0
        for row in range(self.table.rowCount()):
            entry = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            matches = entry.matches(query)
            self.table.setRowHidden(row, not matches)
            visible += matches
        self._count_text = f"显示 {visible} / {len(self.entries)} 条记录"
        self._update_selection()

    def _update_selection(self, *_):
        p = theme.current()
        entry = self._selected_entry()
        selected = self._selected_entries()
        self.open_button.setEnabled(entry is not None and entry.error is None)
        self.edit_button.setEnabled(entry is not None and entry.comparable)
        can_compare = len(selected) >= 2 and all(item.comparable for item in selected)
        self.compare_button.setEnabled(can_compare)
        self.compare_button.setToolTip("只读叠加全部选中记录；不重采样，不修改原始数据" if can_compare else
                                       "请使用 Ctrl/Shift 选择至少两组可读、已收尾的记录")
        can_export = bool(selected) and all(item.batch_exportable for item in selected)
        self.batch_export_button.setEnabled(can_export)
        self.batch_export_button.setToolTip("导出全部选中记录组的全部样本和通道" if can_export else
                                            "请使用 Ctrl/Shift 选择可读、已收尾且无采集异常的记录组")
        incomplete = [item for item in selected if item.error is None and not item.finalized]
        abnormal = [item for item in selected if item.error is None and item.abnormal and item.finalized]
        detail = f"{entry.path}\n{entry.error or entry.status}" if entry else getattr(self, "_count_text", "")
        if entry and entry.error is None:
            descriptive = " · ".join(value for value in (entry.specimen, entry.condition) if value)
            detail += (f"\n{descriptive}" if descriptive else "") + (f"\n备注：{entry.notes}" if entry.notes else "")
        if len(selected) > 1:
            detail += f"\n已选择 {len(selected)} 组；打开仅打开当前组，比较和批量导出处理全部选中组。"
        if incomplete:
            detail += "\n选中记录尚未收尾，不能编辑、比较或批量导出；活动记录请先结束。"
        if abnormal:
            detail += "\n选中记录存在采集异常，不能批量导出；可单独打开、编辑说明或参与只读比较。"
        self.detail.setText(detail)
        if entry is not None and (entry.error or entry.abnormal):
            dot = p.danger
        elif incomplete:
            dot = p.warning
        elif entry is not None:
            dot = p.success
        else:
            dot = p.text_faint
        self.detail_dot.setPixmap(theme.dot_pixmap(dot, 8))

    def _open_selected(self):
        entry = self._selected_entry()
        if entry is not None and entry.error is None:
            self.selected_path = entry.path
            self.accept()

    def _edit_selected(self):
        entry = self._selected_entry()
        if entry is None or not entry.comparable:
            return
        dialog = RecordingDetailsDialog(entry, self)
        if dialog.exec() != W.QDialog.DialogCode.Accepted:
            return
        try:
            update_recording_details(entry.path, **dialog.values)
        except (OSError, ValueError, RuntimeError) as exc:
            W.QMessageBox.warning(self, "无法保存实验信息", str(exc))
            return
        self.details_changed.emit(entry.path)
        self.refresh()

    def _compare_selected(self):
        entries = self._selected_entries()
        if len(entries) < 2 or not all(entry.comparable for entry in entries):
            return None
        paths = [entry.path for entry in entries]
        if self.isModal():
            self.comparison_paths = paths
            self.reject()
            return None
        from .comparison_dialog import ComparisonDialog
        window = ComparisonDialog(paths, self)
        window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self._comparison_windows.append(window)
        window.destroyed.connect(
            lambda _=None, item=window: self._comparison_windows.remove(item)
            if item in self._comparison_windows else None)
        window.show()
        return window

    def _batch_export(self):
        entries = self._selected_entries()
        if entries and all(entry.batch_exportable for entry in entries):
            BatchExportDialog([entry.path for entry in entries], self).exec()
