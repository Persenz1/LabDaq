"""Find recorded sessions from metadata and file lengths, without reading samples."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets as W

from . import theme, ui_kit
from .recording import load_session
from .data_dialog import BatchExportDialog


DIALOG_MARGINS = (22, 20, 22, 20)


@dataclass
class RecordingEntry:
    path: Path
    started_at: datetime | None = None
    experiment: str = ""
    group_number: int | None = None
    duration_seconds: float | None = None
    channel_count: int | None = None
    status: str = "不可打开"
    notes: str = ""
    error: str | None = None
    finalized: bool = False

    def matches(self, query):
        return query.casefold() in " ".join(
            (self.experiment, self.notes, self.path.name, self.status)).casefold()

    @property
    def sort_key(self):
        return (self.started_at is not None,
                self.started_at.timestamp() if self.started_at else 0,
                self.path.name.casefold())


def list_recordings(root) -> list[RecordingEntry]:
    """Inspect direct child session folders; malformed sessions remain visible."""
    root = Path(root)
    if not root.exists():
        return []
    entries = []
    for path in root.iterdir():
        if not path.is_dir() or not (path / "metadata.json").is_file():
            continue
        entry = RecordingEntry(path)
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
            entry.notes = str(options.get("notes", ""))
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
            if not metadata["finalized"]:
                entry.status = "未正常收尾"
            elif metadata.get("error") or reason in ("error", "acquisition_error"):
                entry.status = f"异常（{reason or '未提供原因'}）"
            else:
                entry.status = {"duration_complete": "定长完成", "user_stop": "手动结束",
                                "end_of_file": "回放结束"}.get(reason, reason or "已结束（未提供原因）")
            if metadata.get("error"):
                entry.status += f"：{metadata['error']}"
        except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError, AttributeError) as exc:
            entry = RecordingEntry(path, error=f"{type(exc).__name__}: {exc}")
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


class RecordingLibraryDialog(W.QDialog):
    """Read-only local session picker; selected_path is set only on acceptance."""

    def __init__(self, recording_root, parent=None):
        super().__init__(parent)
        p = theme.current()
        self.recording_root = Path(recording_root)
        self.selected_path: Path | None = None
        self.entries = []
        self.setWindowTitle("记录库")
        self.resize(1120, 620)
        self.setMinimumSize(820, 520)
        layout = _dialog_layout(self)

        layout.addWidget(_header("记录库", f"记录目录：{self.recording_root}", "library"))

        search_row = W.QHBoxLayout()
        search_row.setSpacing(8)
        self.search = W.QLineEdit()
        self.search.setPlaceholderText("搜索实验名、备注、目录名或结束状态")
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

        self.table = W.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["开始时间（本地）", "实验名", "组号", "时长（秒）", "通道数", "结束状态", "备注", "目录名"])
        self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(W.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(31)
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(6, W.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setHighlightSections(False)
        for column, width in enumerate((155, 130, 55, 90, 65, 135, 220, 210)):
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
        self.entries = list_recordings(self.recording_root)
        self.selected_path = None
        self.table.clearSelection()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            started = entry.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if entry.started_at else "—"
            values = (started, entry.experiment or "—",
                      str(entry.group_number) if entry.group_number is not None else "—",
                      f"{entry.duration_seconds:.3f}" if entry.duration_seconds is not None else "—",
                      str(entry.channel_count) if entry.channel_count is not None else "—",
                      entry.status, entry.error or entry.notes, entry.path.name)
            sorts = (entry.sort_key, entry.experiment.casefold(), entry.group_number or 0,
                     entry.duration_seconds if entry.duration_seconds is not None else -1,
                     entry.channel_count or 0, entry.status, entry.notes.casefold(), entry.path.name.casefold())
            for column, (value, key) in enumerate(zip(values, sorts)):
                item = _SortItem(value, key)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, entry)
                item.setToolTip(entry.error or value)
                if column in (2, 3, 4):
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight |
                                          QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
        self.table.clearSelection()
        self._filter_rows()

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
        can_export = bool(selected) and all(item.error is None and item.finalized for item in selected)
        self.batch_export_button.setEnabled(can_export)
        self.batch_export_button.setToolTip("导出全部选中记录组的全部样本和通道" if can_export else
                                            "请使用 Ctrl/Shift 选择可读且已收尾的记录组")
        incomplete = [item for item in selected if item.error is None and not item.finalized]
        detail = f"{entry.path}\n{entry.error or entry.status}" if entry else getattr(self, "_count_text", "")
        if len(selected) > 1:
            detail += f"\n已选择 {len(selected)} 组；打开仅打开当前组，批量导出处理全部选中组。"
        if incomplete:
            detail += "\n选中记录尚未收尾，不能批量导出；活动记录请先结束，异常中断记录可单独打开后导出。"
        self.detail.setText(detail)
        if entry is not None and entry.error:
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

    def _batch_export(self):
        entries = self._selected_entries()
        if entries and all(entry.error is None and entry.finalized for entry in entries):
            BatchExportDialog([entry.path for entry in entries], self).exec()
