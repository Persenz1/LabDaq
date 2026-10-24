"""Read-only, multi-session waveform comparison with bounded background loading."""

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W

from . import theme, ui_kit
from .comparison import (ComparisonSource, comparison_bounds, comparison_settings,
                         export_comparison_csv, load_comparison, load_comparison_profile,
                         save_comparison_profile)
from .data_dialog import _DataJob, _dialog_layout, _header, _notice, _progress_bar, _request_cancel
from .plot_measurements import PlotMeasurements


class ComparisonDialog(W.QDialog):
    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.sources = [ComparisonSource.from_path(path) for path in paths]
        if len(self.sources) < 2:
            raise ValueError("多组比较至少需要两组记录")
        self.appearance = self._default_appearance(self.sources)
        self.comparison_result = None
        self.output_path = None
        self.job = None
        self._job_kind = ""
        self._close_when_finished = False
        self._reset_window = True
        self.plots = []
        self.curves = {}
        self.trace_checks = {}
        self.color_buttons = {}
        self.measurements = None
        self.setWindowTitle("多组记录比较")
        self.resize(1420, 880)
        self.setMinimumSize(1050, 680)
        p = theme.current()
        layout = _dialog_layout(self)
        layout.addWidget(_header(
            "多组记录比较",
            "各组使用自己的采样率、通道配置和真实样本；只读叠加，不重采样或修改原始记录。",
            "wave"))
        self.status = _notice("正在准备比较窗口…", "info")
        layout.addWidget(self.status)
        self.progress = _progress_bar(layout)

        controls = W.QHBoxLayout()
        controls.setSpacing(8)
        self.axis = W.QComboBox()
        for title, value in (("相对时间", "relative"), ("绝对时间（电脑时钟）", "absolute"),
                             ("样本号", "samples")):
            self.axis.addItem(title, value)
        self.values = W.QComboBox()
        self.values.addItem("工程值（按单位分图）", "engineering")
        self.values.addItem("原始电压 V", "raw")
        self.span = W.QDoubleSpinBox()
        self.span.setRange(.000001, 1e12)
        self.span.setDecimals(9)
        self.span.setValue(10)
        self.start = W.QDoubleSpinBox()
        self.start.setRange(0, 1e12)
        self.start.setDecimals(9)
        self.previous_button = ui_kit.chip_button("上一窗口", "chevron_left", lambda: self.step(-1))
        self.next_button = ui_kit.chip_button("下一窗口", "chevron_right", lambda: self.step(1))
        for title, widget in (("时间轴", self.axis), ("显示数值", self.values),
                              ("显示宽度", self.span), ("窗口起点", self.start)):
            controls.addWidget(ui_kit.field_label(title))
            controls.addWidget(widget)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.next_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.splitter = W.QSplitter(QtCore.Qt.Orientation.Horizontal)
        side = W.QWidget()
        side.setObjectName("transparentHost")
        side_layout = W.QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 8, 0)
        side_layout.setSpacing(8)
        side_layout.addWidget(ui_kit.section_title("记录与通道", "library"))
        self.source_table = W.QTableWidget(0, 5)
        self.source_table.setHorizontalHeaderLabels(["记录 / 通道", "显示", "颜色", "S/s", "状态"])
        self.source_table.verticalHeader().hide()
        self.source_table.setShowGrid(False)
        self.source_table.setAlternatingRowColors(True)
        self.source_table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.source_table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.source_table.horizontalHeader().setSectionResizeMode(0, W.QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.source_table.horizontalHeader().setSectionResizeMode(column, W.QHeaderView.ResizeMode.ResizeToContents)
        self.source_table.setMinimumWidth(390)
        self.source_table.setMaximumHeight(290)
        self.source_table.cellClicked.connect(self._select_table_group)
        side_layout.addWidget(self.source_table)
        side_layout.addWidget(ui_kit.section_title("基本对齐", "sliders"))
        self.group_combo = W.QComboBox()
        self.group_combo.currentIndexChanged.connect(self._sync_alignment_editor)
        side_layout.addWidget(ui_kit.field("记录组", self.group_combo))
        self.zero_input = W.QDoubleSpinBox()
        self.zero_input.setDecimals(9)
        self.zero_input.setRange(0, 1e12)
        self.align_button = W.QPushButton("应用对齐零点")
        self.align_button.setIcon(theme.icon("check", p.text_muted, 15))
        self.align_button.setIconSize(QtCore.QSize(15, 15))
        self.align_button.clicked.connect(self.apply_alignment)
        align_row = W.QHBoxLayout()
        align_row.addWidget(self.zero_input, 1)
        align_row.addWidget(self.align_button)
        side_layout.addLayout(align_row)
        self.align_hint = ui_kit.label("", "faint", True)
        side_layout.addWidget(self.align_hint)
        side_layout.addStretch(1)
        self.splitter.addWidget(side)

        self.plot_container = W.QWidget()
        self.plot_container.setObjectName("transparentHost")
        self.plot_layout = W.QVBoxLayout(self.plot_container)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)
        self.plot_layout.setSpacing(8)
        scroll = W.QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        scroll.setWidget(self.plot_container)
        self.splitter.addWidget(scroll)
        self.splitter.setSizes([410, 970])
        self.splitter.splitterMoved.connect(lambda *_: self._view_changed())
        layout.addWidget(self.splitter, 1)
        self.cursor_host = W.QWidget()
        self.cursor_host.setObjectName("transparentHost")
        self.cursor_layout = W.QVBoxLayout(self.cursor_host)
        self.cursor_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.cursor_host)

        layout.addWidget(theme.divider())
        footer = W.QHBoxLayout()
        footer.setSpacing(8)
        self.save_layout_button = ui_kit.chip_button("保存比较布局", "save", self.save_layout)
        self.load_layout_button = ui_kit.chip_button("载入比较布局", "open", self.load_layout)
        self.image_button = ui_kit.chip_button("保存图像", "save", self.save_image)
        footer.addWidget(self.save_layout_button)
        footer.addWidget(self.load_layout_button)
        footer.addWidget(self.image_button)
        footer.addStretch(1)
        self.export_values = W.QComboBox()
        for title, value in (("原始 V 与工程值", "both"), ("仅原始 V", "raw"), ("仅工程值", "engineering")):
            self.export_values.addItem(title, value)
        self.file_format = W.QComboBox()
        self.file_format.addItems(["CSV", "TXT"])
        self.export_button = ui_kit.chip_button("导出当前窗口", "export", self.choose_export)
        footer.addWidget(ui_kit.field_label("导出"))
        footer.addWidget(self.export_values)
        footer.addWidget(self.file_format)
        footer.addWidget(self.export_button)
        self.cancel_button = W.QPushButton("关闭")
        self.cancel_button.setObjectName("ghost")
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)

        self._view = {"splitter_sizes": self.splitter.sizes()}
        self.axis.currentIndexChanged.connect(self._axis_changed)
        self.values.currentIndexChanged.connect(lambda *_: self.reload())
        self.start.editingFinished.connect(lambda: self.reload())
        self.span.editingFinished.connect(lambda: self.reload())
        self._rebuild_trace_table()
        self._sync_axis_fields()
        self._set_busy(False)
        QtCore.QTimer.singleShot(0, self.reload)

    @staticmethod
    def _key(source_index, channel_index):
        return f"{source_index}:{channel_index}"

    @staticmethod
    def _default_appearance(sources):
        p = theme.current()
        result = {}
        for source_index, source in enumerate(sources):
            for channel_index in source.channel_indices:
                color = QtGui.QColor(p.channels[(source_index + channel_index) % len(p.channels)])
                if source_index >= len(p.channels):
                    color = color.lighter(110 + 20 * (source_index // len(p.channels)) % 50)
                result[ComparisonDialog._key(source_index, channel_index)] = {
                    "visible": True, "color": color.name()}
        return result

    def _merged_appearance(self, sources, saved):
        result = self._default_appearance(sources)
        for key, style in saved.items():
            color = QtGui.QColor(style["color"])
            if not color.isValid():
                raise ValueError(f"比较布局中的颜色无效：{style['color']}")
            result[key] = {"visible": style["visible"], "color": color.name()}
        return result

    def _rebuild_trace_table(self):
        current_group = self.group_combo.currentIndex()
        self.source_table.clearContents()
        self.trace_checks.clear()
        self.color_buttons.clear()
        rows = [(source_index, channel) for source_index, source in enumerate(self.sources)
                for channel in source.channels]
        self.source_table.setRowCount(len(rows))
        p = theme.current()
        for row, (source_index, channel) in enumerate(rows):
            source = self.sources[source_index]
            key = self._key(source_index, channel.index)
            annotation = (f"P{channel.point_number} " if channel.point_number is not None else "") + channel.direction
            item = W.QTableWidgetItem(f"{source.name} / AI{channel.index} · {channel.name}"
                                      + (f" · {annotation}" if annotation else ""))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, source_index)
            item.setToolTip(f"{source.path}\nAI{channel.index} · {channel.name} · {channel.unit}")
            self.source_table.setItem(row, 0, item)
            check = W.QCheckBox()
            check.setChecked(self.appearance[key]["visible"])
            check.setToolTip("只控制比较视图显隐，不改变记录或导出已有单组数据")
            check.toggled.connect(lambda checked, k=key: self._visibility_changed(k, checked))
            self.source_table.setCellWidget(row, 1, check)
            self.trace_checks[key] = check
            color = W.QToolButton()
            color.setFixedSize(36, 23)
            color.setToolTip("选择比较曲线颜色")
            color.clicked.connect(lambda _=False, k=key: self._choose_color(k))
            self.source_table.setCellWidget(row, 2, color)
            self.color_buttons[key] = color
            self._sync_color_button(key)
            self.source_table.setItem(row, 3, W.QTableWidgetItem(f"{source.session.sample_rate:,.3f}"))
            status = W.QTableWidgetItem("异常" if source.has_acquisition_error else "已收尾")
            if source.has_acquisition_error:
                status.setForeground(QtGui.QBrush(QtGui.QColor(p.danger)))
                status.setToolTip(str(source.session.metadata.get("error") or
                                      source.session.metadata.get("termination_reason")))
            self.source_table.setItem(row, 4, status)
            self.source_table.setRowHeight(row, 30)
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        for index, source in enumerate(self.sources):
            self.group_combo.addItem(f"{index + 1}. {source.name}", index)
        self.group_combo.setCurrentIndex(min(max(0, current_group), len(self.sources) - 1))
        self.group_combo.blockSignals(False)
        self._sync_alignment_editor()

    def _select_table_group(self, row, _column):
        item = self.source_table.item(row, 0)
        if item is not None:
            self.group_combo.setCurrentIndex(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def _sync_color_button(self, key):
        self.color_buttons[key].setStyleSheet(
            f"background: {self.appearance[key]['color']}; border: 1px solid {theme.current().border};"
            " border-radius: 4px;")

    def _sync_axis_fields(self):
        samples = self.axis.currentData() == "samples"
        for spin in (self.start, self.span):
            spin.setDecimals(0 if samples else 9)
            spin.setSuffix(" 样本" if samples else " s")
        self.span.setMinimum(1 if samples else .000001)
        self.span.setSingleStep(1 if samples else .1)
        self.start.setSingleStep(1 if samples else .1)
        absolute_available = all(source.epoch is not None for source in self.sources)
        item = self.axis.model().item(1)
        if item is not None:
            item.setEnabled(absolute_available)
        self.axis.setItemData(1, "所有记录都需要可靠的采集起点；不会使用文件修改时间代替",
                              QtCore.Qt.ItemDataRole.ToolTipRole)

    def _sync_alignment_editor(self, *_):
        if not hasattr(self, "group_combo") or self.group_combo.currentData() is None:
            return
        source = self.sources[self.group_combo.currentData()]
        axis = self.axis.currentData()
        with QtCore.QSignalBlocker(self.zero_input):
            if axis == "relative":
                value, suffix, decimals = source.zero_seconds, " s（本组相对时间）", 9
                low, high = 0, source.session.elapsed_duration
                hint = "输入本组相对时间，自动取最近真实样本作为零点；曲线不重采样。"
            elif axis == "samples":
                value, suffix, decimals = source.sample_origin, "（源样本号）", 0
                last = (int(source.session.sample_indices(source.session.sample_count - 1, 1)[0])
                        if source.session.sample_count else 0)
                low, high = 0, last
                hint = "输入实际存在的源样本号作为零点；暂停缺口不补点。"
            else:
                value, suffix, decimals = source.shift_seconds, " s（显示偏移）", 9
                low, high = -1e12, 1e12
                hint = "绝对时间使用电脑采集时钟；此处只设置显示偏移，不改变记录时间。"
            self.zero_input.setRange(low, high)
            self.zero_input.setDecimals(decimals)
            self.zero_input.setSuffix(suffix)
            self.zero_input.setValue(value)
        self.align_hint.setText(hint)

    def _axis_changed(self, *_):
        if self.axis.currentData() == "absolute" and not all(source.epoch is not None for source in self.sources):
            self.axis.blockSignals(True)
            self.axis.setCurrentIndex(0)
            self.axis.blockSignals(False)
            self.status.setText("存在缺少可靠采集起点的记录，不能使用绝对时间；已保留相对时间轴。")
            theme.set_tone(self.status, "warning")
        self._reset_window = True
        self._sync_axis_fields()
        self._sync_alignment_editor()
        self.reload()

    def reload(self, *, sources=None, appearance=None):
        if self.job is not None and self.job.isRunning():
            return
        source_snapshot = tuple(self.sources if sources is None else sources)
        axis = self.axis.currentData()
        try:
            low, high = comparison_bounds(source_snapshot, axis)
        except (OSError, ValueError) as exc:
            self.status.setText(f"比较窗口无效：{exc}")
            theme.set_tone(self.status, "danger")
            return
        span = self.span.value()
        maximum = max(low, high - span)
        value = low if self._reset_window else min(max(self.start.value(), low), maximum)
        with QtCore.QSignalBlocker(self.start):
            self.start.setRange(min(0, low, maximum), max(0, low, maximum))
            self.start.setValue(value)
        self._reset_window = False
        options = {"axis": axis, "values": self.values.currentData(),
                   "start": self.start.value(), "span": span}
        self._start_job(lambda job: load_comparison(
            source_snapshot, **options, progress=job.report_progress,
            cancelled=job.isInterruptionRequested), "load", source_snapshot, appearance)

    def _start_job(self, operation, kind, sources=(), appearance=None):
        self._close_when_finished = False
        self.job = _DataJob(operation, self)
        self.job.source_snapshot = sources
        self.job.profile_appearance = appearance
        self._job_kind = kind
        self.job.progressed.connect(self.progress.setValue)
        self.job.finished.connect(self._finished)
        self._set_busy(True)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status.setText("正在分块读取比较窗口…" if kind == "load" else "正在导出当前比较窗口…")
        theme.set_tone(self.status, "info")
        self.job.start()

    def _set_busy(self, busy):
        for widget in (self.axis, self.values, self.start, self.span, self.previous_button,
                       self.next_button, self.source_table, self.group_combo, self.zero_input,
                       self.align_button, self.save_layout_button, self.load_layout_button):
            widget.setEnabled(not busy)
        has_result = self.comparison_result is not None
        self.image_button.setEnabled(not busy and has_result)
        self.export_button.setEnabled(not busy and has_result and bool(self.comparison_result.traces))
        self.export_values.setEnabled(not busy)
        self.file_format.setEnabled(not busy)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("取消任务" if busy else "关闭")
        if not busy:
            self.progress.setVisible(False)

    def _finished(self):
        job, self.job = self.job, None
        kind = self._job_kind
        job.deleteLater()
        succeeded = not job.error and not job.cancelled
        if succeeded and kind == "load":
            self.sources = list(job.source_snapshot)
            if job.profile_appearance is not None:
                self.appearance = job.profile_appearance
            self.comparison_result = job.result
            self._rebuild_trace_table()
            self._sync_axis_fields()
        self._set_busy(False)
        if job.cancelled:
            self.status.setText("加载已取消，保留上一次成功显示。" if kind == "load" else
                                "导出已取消；未替换目标文件，临时文件已清理。")
            theme.set_tone(self.status, "warning")
        elif job.error:
            self.status.setText(f"{'比较加载' if kind == 'load' else '比较导出'}失败：{job.error}")
            theme.set_tone(self.status, "danger")
        elif kind == "load":
            self._render_result()
        else:
            self.output_path = job.result
            self.status.setText(f"比较窗口已导出 · {self.output_path}")
            theme.set_tone(self.status, "success")
        if self._close_when_finished:
            self._close_when_finished = False
            self.reject()

    def _render_result(self):
        result = self.comparison_result
        self._rebuild_plots()
        unit = "样本" if result.axis == "samples" else "s"
        text = (f"{len(result.sources)} 组 · {len(result.traces)} 条曲线 · "
                f"{result.frame_count:,} 帧 · {result.value_count:,} 个通道值\n"
                f"窗口 [{result.start:.9g}, {result.start + result.span:.9g}) {unit}；"
                "曲线只连接同组连续样本，暂停处断开。")
        abnormal = [source.name for source in result.sources if source.has_acquisition_error]
        if result.truncated:
            text += "\n已达到每组 200,000 帧或总计 2,000,000 个通道值的上限；请缩小窗口或切换下一窗口。"
        if abnormal:
            text += "\n采集异常组（只读比较，仍不可批量导出）：" + "、".join(abnormal)
        if any(source.epoch is None for source in result.sources):
            text += "\n存在缺少可靠采集起点的记录，不能使用绝对时间轴。"
        if not result.traces:
            text += "\n当前窗口没有实录样本；可移动窗口或调整对齐零点。"
        self.status.setText(text)
        theme.set_tone(self.status, "warning" if result.truncated or abnormal else "success")

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _new_plot(self, unit):
        p = theme.current()
        axis = pg.DateAxisItem() if self.comparison_result.axis == "absolute" else pg.AxisItem("bottom")
        plot = pg.PlotWidget(background=p.plot_bg, axisItems={"bottom": axis})
        plot.showGrid(x=True, y=True, alpha=.1)
        plot.setMenuEnabled(False)
        plot.hideButtons()
        plot.getAxis("left").enableAutoSIPrefix(False)
        plot.setLabel("left", "工程值" if self.comparison_result.values == "engineering" else "原始电压", units=unit)
        plot.setLabel("bottom", {"relative": "相对时间", "absolute": "电脑时钟（本地时间）",
                                  "samples": "样本号"}[self.comparison_result.axis],
                      units="" if self.comparison_result.axis == "absolute" else
                      "样本" if self.comparison_result.axis == "samples" else "s")
        plot.setMinimumHeight(210)
        plot.plotItem.setDownsampling(auto=True, mode="peak")
        theme.style_plot(plot, p)
        return plot

    def _rebuild_plots(self):
        self._clear_layout(self.plot_layout)
        self._clear_layout(self.cursor_layout)
        self.measurements = None
        self.plots.clear()
        self.curves.clear()
        result = self.comparison_result
        units = list(dict.fromkeys(trace.unit for trace in result.traces)) or ["V"]
        for unit in units:
            plot = self._new_plot(unit)
            if self.plots:
                plot.setXLink(self.plots[0])
            self.plots.append(plot)
            self.plot_layout.addWidget(plot, 1)
            for trace in result.traces:
                if trace.unit != unit:
                    continue
                key = self._key(trace.source_index, trace.channel.index)
                values = trace.raw_values if result.values == "raw" else trace.engineering_values
                gaps = trace.gaps
                x = np.insert(trace.x, gaps, trace.x[gaps])
                y = np.insert(values, gaps, np.nan)
                curve = plot.plot(x, y, pen=pg.mkPen(self.appearance[key]["color"], width=1.2),
                                  connect="finite", autoDownsample=True, downsampleMethod="peak")
                curve.setVisible(self.appearance[key]["visible"])
                self.curves[key] = curve
        self.measurements = PlotMeasurements(self.plots[0], self.cursor_host)
        for plot in self.plots[1:]:
            self.measurements.link_plot(plot)
        self.cursor_layout.addWidget(self.measurements)
        self._update_measurements()
        for plot in self.plots:
            plot.enableAutoRange()

    def _update_measurements(self):
        if self.measurements is None or self.comparison_result is None:
            return
        result = self.comparison_result
        series = []
        for trace in result.traces:
            key = self._key(trace.source_index, trace.channel.index)
            if not self.appearance[key]["visible"]:
                continue
            channel = trace.channel
            annotation = (f"P{channel.point_number} " if channel.point_number is not None else "") + channel.direction
            series.append({"key": key, "label": trace.label + (f" · {annotation}" if annotation else ""),
                           "unit": trace.unit, "times": trace.times, "x_values": trace.x,
                           "values": trace.raw_values if result.values == "raw" else trace.engineering_values})
        self.measurements.set_series(series)

    def _visibility_changed(self, key, checked):
        self.appearance[key]["visible"] = bool(checked)
        if key in self.curves:
            self.curves[key].setVisible(bool(checked))
        self._update_measurements()

    def _choose_color(self, key):
        color = W.QColorDialog.getColor(QtGui.QColor(self.appearance[key]["color"]), self, "选择比较曲线颜色")
        if not color.isValid():
            return
        self.appearance[key]["color"] = color.name()
        self._sync_color_button(key)
        if key in self.curves:
            self.curves[key].setPen(pg.mkPen(color, width=1.2))

    def apply_alignment(self):
        if not self.sources or self.group_combo.currentData() is None:
            return
        index = self.group_combo.currentData()
        source = self.sources[index]
        try:
            if self.axis.currentData() == "relative":
                source = source.set_time_zero(self.zero_input.value())
            elif self.axis.currentData() == "samples":
                source = source.set_sample_origin(int(self.zero_input.value()))
            else:
                source = source.with_shift(self.zero_input.value())
        except ValueError as exc:
            self.status.setText(f"无法设置对齐零点：{exc}")
            theme.set_tone(self.status, "danger")
            return
        self.sources[index] = source
        self._reset_window = True
        self.reload()

    def step(self, direction):
        self.start.setValue(self.start.value() + direction * self.span.value())
        self.reload()

    def _view_changed(self):
        self._view = {"splitter_sizes": self.splitter.sizes()}

    def _settings(self):
        result = self.comparison_result
        if result is None:
            raise ValueError("请先等待比较数据加载完成")
        self._view_changed()
        return comparison_settings(result.sources, result.axis, result.start, result.span,
                                   result.values, self.appearance, view=self._view)

    def save_profile_to(self, path):
        return save_comparison_profile(Path(path), self._settings())

    def save_layout(self):
        path, _ = W.QFileDialog.getSaveFileName(
            self, "保存比较布局", str(self.sources[0].path.parent / "comparison-layout.json"),
            "比较布局 (*.json)")
        if path:
            try:
                self.save_profile_to(Path(path))
                self.status.setText(f"比较布局已保存 · {path}")
                theme.set_tone(self.status, "success")
            except (OSError, ValueError) as exc:
                self.status.setText(f"比较布局无法保存：{exc}")
                theme.set_tone(self.status, "danger")

    def load_profile_from(self, path):
        profile = load_comparison_profile(path)
        if len(profile.sources) < 2:
            raise ValueError("比较布局至少需要两组记录")
        comparison_bounds(profile.sources, profile.axis)
        appearance = self._merged_appearance(profile.sources, profile.appearance)
        with QtCore.QSignalBlocker(self.axis), QtCore.QSignalBlocker(self.values), \
                QtCore.QSignalBlocker(self.start), QtCore.QSignalBlocker(self.span):
            self.axis.setCurrentIndex(self.axis.findData(profile.axis))
            self.values.setCurrentIndex(self.values.findData(profile.values))
            self.start.setValue(profile.start)
            self.span.setValue(profile.span)
        self._reset_window = False
        self._view = dict(profile.view or {})
        if self._view.get("splitter_sizes"):
            self.splitter.setSizes(self._view["splitter_sizes"])
        self.reload(sources=profile.sources, appearance=appearance)

    def load_layout(self):
        path, _ = W.QFileDialog.getOpenFileName(
            self, "载入比较布局", str(self.sources[0].path.parent), "比较布局 (*.json)")
        if path:
            try:
                self.load_profile_from(Path(path))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.status.setText(f"比较布局无法载入：{exc}")
                theme.set_tone(self.status, "danger")

    def save_image_to(self, path):
        path = Path(path)
        if path.suffix.lower() != ".png":
            raise ValueError("比较图请保存为 .png 文件")
        if self.comparison_result is None:
            raise ValueError("请先等待比较数据加载完成")
        if not self.plot_container.grab().save(str(path), "PNG"):
            raise OSError(f"无法保存比较图：{path}")
        return path

    def save_image(self):
        path, _ = W.QFileDialog.getSaveFileName(
            self, "保存比较图", str(self.sources[0].path.parent / "comparison.png"), "PNG 图像 (*.png)")
        if path:
            try:
                self.save_image_to(Path(path))
                self.status.setText(f"比较图已保存 · {path}")
                theme.set_tone(self.status, "success")
            except (OSError, ValueError) as exc:
                self.status.setText(f"比较图无法保存：{exc}")
                theme.set_tone(self.status, "danger")

    def choose_export(self):
        extension = ".txt" if self.file_format.currentIndex() else ".csv"
        path, _ = W.QFileDialog.getSaveFileName(
            self, "导出当前比较窗口", str(self.sources[0].path.parent / ("comparison-window" + extension)),
            f"数据 (*{extension})")
        if path:
            self.begin_export(path, overwrite=True)

    def begin_export(self, path, *, delimiter=None, overwrite=False):
        if self.job is not None and self.job.isRunning() or self.comparison_result is None:
            return
        if not self.comparison_result.traces:
            self.status.setText("当前窗口没有可导出的真实样本。")
            theme.set_tone(self.status, "warning")
            return
        self.output_path = None
        delimiter = ("\t" if self.file_format.currentIndex() else ",") if delimiter is None else delimiter
        target = Path(path)
        self._start_job(lambda job: export_comparison_csv(
            self.comparison_result, target, values=self.export_values.currentData(), delimiter=delimiter,
            overwrite=overwrite, progress=job.report_progress,
            cancelled=job.isInterruptionRequested), "export")

    def reject(self):
        if self.job is not None and self.job.isRunning():
            _request_cancel(self, self.cancel_button)
        else:
            super().reject()

    def closeEvent(self, event):
        if self.job is not None and self.job.isRunning():
            self._close_when_finished = True
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
