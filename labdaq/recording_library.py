"""Find recorded sessions from metadata and file lengths, without reading samples."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path

from PySide6 import QtCore, QtWidgets as W

from .recording import load_session
from .data_dialog import BatchExportDialog


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


class RecordingLibraryDialog(W.QDialog):
    """Read-only local session picker; selected_path is set only on acceptance."""

    def __init__(self, recording_root, parent=None):
        super().__init__(parent)
        self.recording_root = Path(recording_root)
        self.selected_path: Path | None = None
        self.entries = []
        self.setWindowTitle("记录库")
        self.resize(1120, 560)
        layout = W.QVBoxLayout(self)
        root_label = W.QLabel(f"记录目录：{self.recording_root}")
        root_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(root_label)
        toolbar = W.QHBoxLayout()
        self.search = W.QLineEdit()
        self.search.setPlaceholderText("搜索实验名、备注、目录名或结束状态")
        self.refresh_button = W.QPushButton("刷新")
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.table = W.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["开始时间（本地）", "实验名", "组号", "时长（秒）", "通道数", "结束状态", "备注", "目录名"])
        self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(W.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(6, W.QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((155, 130, 55, 90, 65, 135, 220, 210)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)
        self.detail = W.QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Cancel)
        self.batch_export_button = buttons.addButton("批量导出", W.QDialogButtonBox.ButtonRole.ActionRole)
        self.open_button = buttons.addButton("打开", W.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.button(W.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)
        buttons.rejected.connect(self.reject)
        self.open_button.clicked.connect(self._open_selected)
        self.batch_export_button.clicked.connect(self._batch_export)
        self.table.itemSelectionChanged.connect(self._update_selection)
        self.table.currentCellChanged.connect(self._update_selection)
        self.table.itemDoubleClicked.connect(lambda _item: self._open_selected())
        self.search.textChanged.connect(self._filter_rows)
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh()

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
        entry = self._selected_entry()
        selected = self._selected_entries()
        self.open_button.setEnabled(entry is not None and entry.error is None)
        can_export = bool(selected) and all(item.error is None and item.finalized for item in selected)
        self.batch_export_button.setEnabled(can_export)
        self.batch_export_button.setToolTip("导出全部选中记录组的全部样本和通道" if can_export else
                                            "请使用 Ctrl/Shift 选择可读且已收尾的记录组")
        detail = f"{entry.path}\n{entry.error or entry.status}" if entry else getattr(self, "_count_text", "")
        if len(selected) > 1:
            detail += f"\n已选择 {len(selected)} 组；打开仅打开当前组，批量导出处理全部选中组。"
        if any(item.error is None and not item.finalized for item in selected):
            detail += "\n选中记录尚未收尾，不能批量导出；活动记录请先结束，异常中断记录可单独打开后导出。"
        self.detail.setText(detail)

    def _open_selected(self):
        entry = self._selected_entry()
        if entry is not None and entry.error is None:
            self.selected_path = entry.path
            self.accept()

    def _batch_export(self):
        entries = self._selected_entries()
        if entries and all(entry.error is None and entry.finalized for entry in entries):
            BatchExportDialog([entry.path for entry in entries], self).exec()
