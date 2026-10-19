"""Small reusable widgets shared by the LabDAQ window and its dialogs.

Everything here reads colors from :mod:`labdaq.theme` at construction time, so
the same components work with every palette.
"""
from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W

from . import theme

__all__ = [
    "label", "card", "icon_button", "chip_button", "section_title", "field_label",
    "field", "divider", "status_dot", "status_chip", "Sparkline", "MetricCard",
    "CollapsibleSection", "soft_shadow", "legend_chip",
]


def label(text: str = "", name: str = "", wrap: bool = False) -> W.QLabel:
    result = W.QLabel(text)
    result.setObjectName(name)
    result.setWordWrap(wrap)
    return result


def card(name: str = "card", margins=(16, 14, 16, 14), spacing: int = 10):
    frame = W.QFrame()
    frame.setObjectName(name)
    layout = W.QVBoxLayout(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return frame, layout


def icon_button(icon_name: str, tooltip: str = "", callback=None, size: int = 16,
                name: str = "iconButton") -> W.QToolButton:
    p = theme.current()
    button = W.QToolButton()
    button.setObjectName(name)
    button.setIcon(theme.icon(icon_name, p.text_muted, size))
    button.setIconSize(QtCore.QSize(size, size))
    button.setToolTip(tooltip)
    button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    button.setAutoRaise(True)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def chip_button(text: str, icon_name: str | None = None, callback=None,
                checkable: bool = False) -> W.QPushButton:
    p = theme.current()
    button = W.QPushButton(text)
    button.setObjectName("chip")
    button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    if icon_name:
        button.setIcon(theme.icon(icon_name, p.text_muted, 14))
        button.setIconSize(QtCore.QSize(14, 14))
    button.setCheckable(checkable)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def section_title(text: str, icon_name: str | None = None) -> W.QWidget:
    p = theme.current()
    holder = W.QWidget()
    holder.setObjectName("transparentHost")
    layout = W.QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    if icon_name:
        mark = W.QLabel()
        mark.setPixmap(theme.icon(icon_name, p.accent, 16).pixmap(16, 16))
        layout.addWidget(mark)
    layout.addWidget(label(text, "sectionTitle"))
    layout.addStretch()
    return holder


def field_label(text: str) -> W.QLabel:
    return label(text, "fieldLabel")


def field(title: str, widget: W.QWidget, hint: str | None = None) -> W.QWidget:
    holder = W.QWidget()
    holder.setObjectName("transparentHost")
    layout = W.QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(field_label(title))
    layout.addWidget(widget)
    if hint:
        hint_label = label(hint, "muted", True)
        layout.addWidget(hint_label)
    return holder


def divider() -> W.QFrame:
    return theme.divider()


def status_dot(color: str, size: int = 8) -> W.QLabel:
    dot = W.QLabel()
    dot.setFixedSize(size, size)
    dot.setPixmap(theme.dot_pixmap(color, size))
    return dot


def status_chip(text: str = "", tone: str = "neutral") -> W.QLabel:
    chip = label(text, "statusPill")
    chip.setProperty("tone", tone)
    return chip


def legend_chip(text: str, color: str) -> W.QWidget:
    """Colored dot plus a short legend label."""
    p = theme.current()
    holder = W.QWidget()
    holder.setObjectName("transparentHost")
    layout = W.QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(status_dot(color, 8))
    layout.addWidget(label(text, "legendLabel"))
    return holder


def soft_shadow(widget: W.QWidget) -> None:
    theme.soft_shadow(widget)


class Sparkline(W.QWidget):
    """Tiny trend line for a metric card; no axes, no interaction."""

    def __init__(self, color: str, height: int = 28, parent=None):
        super().__init__(parent)
        self.color = QtGui.QColor(color)
        self.setFixedHeight(height)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._values = np.empty(0)
        self._range: tuple[float, float] | None = None

    def set_values(self, values, y_range: tuple[float, float] | None = None) -> None:
        data = np.asarray(values, dtype=float).ravel()
        data = data[np.isfinite(data)]
        if data.size > 180:
            step = max(1, data.size // 180)
            data = data[::step][-180:]
        self._values = data
        self._range = y_range
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 3, -1, -2)
        if rect.width() <= 1 or rect.height() <= 1:
            return
        if self._values.size < 2:
            painter.setPen(QtGui.QPen(theme.alpha(self.color.name(), 0.35), 1.2))
            painter.drawLine(rect.left(), rect.center().y(), rect.right(), rect.center().y())
            return
        if self._range is not None:
            low, high = self._range
        else:
            low, high = float(np.min(self._values)), float(np.max(self._values))
            if high - low < 1e-12:
                low, high = low - 1.0, high + 1.0
            pad = (high - low) * 0.12
            low, high = low - pad, high + pad
        xs = np.linspace(rect.left(), rect.right(), self._values.size)
        ys = rect.bottom() - (self._values - low) / (high - low) * rect.height()
        line = QtGui.QPainterPath(QtCore.QPointF(float(xs[0]), float(ys[0])))
        for x, y in zip(xs[1:], ys[1:]):
            line.lineTo(QtCore.QPointF(float(x), float(y)))
        fill = QtGui.QPainterPath(line)
        fill.lineTo(rect.right(), rect.bottom())
        fill.lineTo(rect.left(), rect.bottom())
        fill.closeSubpath()
        gradient = QtGui.QLinearGradient(0, rect.top(), 0, rect.bottom())
        top = QtGui.QColor(self.color)
        top.setAlpha(56)
        bottom = QtGui.QColor(self.color)
        bottom.setAlpha(0)
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.fillPath(fill, QtGui.QBrush(gradient))
        pen = QtGui.QPen(self.color, 1.6)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(line)


class MetricCard(W.QFrame):
    """Channel readout card: identity, big number and trend line."""

    def __init__(self, channel_label: str, color: str, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.color = color
        p = theme.current()
        outer = W.QVBoxLayout(self)
        outer.setContentsMargins(15, 12, 15, 11)
        outer.setSpacing(4)

        top = W.QHBoxLayout()
        top.setSpacing(7)
        top.addStrut(24)
        self.dot = status_dot(color, 8)
        self.title_label = label(channel_label, "metricTitle")
        top.addWidget(self.dot)
        top.addWidget(self.title_label)
        top.addStretch()
        self.visibility_button = chip_button("显示", checkable=True)
        self.visibility_button.setChecked(True)
        self.visibility_button.setFixedSize(48, 24)
        self.visibility_button.setAccessibleName(f"{channel_label} 曲线显示")
        self.visibility_button.toggled.connect(
            lambda visible: self.visibility_button.setText("显示" if visible else "隐藏"))
        top.addWidget(self.visibility_button)
        outer.addLayout(top)

        self.name_label = label("", "metricName")
        outer.addWidget(self.name_label)

        value_row = W.QHBoxLayout()
        value_row.setSpacing(6)
        self.value_label = label("—", "metric")
        self.value_label.setStyleSheet(f"color: {color};")
        self.value_label.setFont(theme.mono_font(19, 600))
        self.unit_label = label("", "metricUnit")
        value_row.addWidget(self.value_label)
        value_row.addWidget(self.unit_label, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        value_row.addStretch()
        outer.addLayout(value_row)

        self.sparkline = Sparkline(color, 27)
        outer.addWidget(self.sparkline)

        self.sub_label = label("等待采样", "muted")
        outer.addWidget(self.sub_label)

        theme.soft_shadow(self, blur=24, dy=6, strength=16)

    def set_value_text(self, text: str) -> None:
        """Set the readout and shrink the font when the card is narrow."""
        self.value_label.setText(text)
        self._fit_value_font()

    def _fit_value_font(self) -> None:
        available = self.width() - 30 - self.unit_label.sizeHint().width() - 6
        if available <= 20:
            return
        text = self.value_label.text()
        chosen = 18
        for size in (32, 30, 28, 26, 24, 22, 20, 18):
            font = QtGui.QFont(self.value_label.font())
            font.setPixelSize(size)
            if QtGui.QFontMetrics(font).horizontalAdvance(text) <= available:
                chosen = size
                break
        stylesheet = f"color: {self.color}; font-size: {chosen}px; font-weight: 600;"
        if self.value_label.styleSheet() != stylesheet:
            self.value_label.setStyleSheet(stylesheet)

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._fit_value_font()

class CollapsibleSection(W.QFrame):
    """A card with a compact header that hides or reveals its controls."""

    def __init__(self, title: str, icon_name: str | None = None, *, expanded: bool = True,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._p = theme.current()
        outer = W.QVBoxLayout(self)
        outer.setContentsMargins(15, 11, 15, 13)
        outer.setSpacing(9)

        header = W.QHBoxLayout()
        header.setSpacing(6)
        self.toggle = W.QToolButton()
        self.toggle.setObjectName("sectionToggle")
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setIconSize(QtCore.QSize(15, 15))
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        if icon_name:
            self.toggle.setProperty("iconName", icon_name)
        header.addWidget(self.toggle)
        header.addStretch()
        self.trailing = W.QHBoxLayout()
        self.trailing.setSpacing(6)
        header.addLayout(self.trailing)
        outer.addLayout(header)

        self.body = W.QWidget()
        self.body.setObjectName("transparentHost")
        self.body_layout = W.QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(9)
        outer.addWidget(self.body)

        self.toggle.toggled.connect(self.set_expanded)
        self._sync()

    def _sync(self) -> None:
        expanded = self.toggle.isChecked()
        icon_name = self.toggle.property("iconName") or "chevron_down"
        p = self._p
        self.toggle.setIcon(theme.icon(icon_name if expanded else "chevron_right",
                                      p.text_muted, 15))
        self.body.setVisible(expanded)

    def set_expanded(self, expanded: bool) -> None:
        if self.toggle.isChecked() != expanded:
            self.toggle.blockSignals(True)
            self.toggle.setChecked(expanded)
            self.toggle.blockSignals(False)
        self._sync()

    def add(self, widget: W.QWidget) -> None:
        self.body_layout.addWidget(widget)

    def add_layout(self, layout) -> None:
        self.body_layout.addLayout(layout)

    def add_trailing(self, widget: W.QWidget) -> None:
        self.trailing.addWidget(widget)
