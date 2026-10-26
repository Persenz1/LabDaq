"""Waveform cursors with a usable hit area and draggable labels."""
from __future__ import annotations

import pyqtgraph as pg
from pyqtgraph.graphicsItems.InfiniteLine import InfLineLabel
from PySide6 import QtCore, QtGui


class _CursorHandle(InfLineLabel):
    def hoverEvent(self, event):
        if not event.isExit() and self.line.movable:
            event.acceptDrags(QtCore.Qt.MouseButton.LeftButton)

    def mouseDragEvent(self, event):
        if not self.line.movable or event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        view = self.getViewBox()
        if event.isStart():
            self.line.moving = True
            self.line.startPosition = self.line.pos()
            self._startPosition = self.orthoPos
            self._cursorOffset = self._posToRel(event.buttonDownPos())
            self._lineOffset = self.line.value() - view.mapSceneToView(event.buttonDownScenePos()).x()
        event.accept()
        relative = self._posToRel(event.pos())
        self.line.setValue(self._lineOffset + view.mapSceneToView(event.scenePos()).x())
        first, last = self.getEndpoints()
        height = abs(self.line.mapToScene(last).y() - self.line.mapToScene(first).y())
        margin = min(.5, self.sceneBoundingRect().height() / (2 * height))
        self.setPosition(min(1 - margin, max(margin, self._startPosition + relative - self._cursorOffset)))
        self.line.sigDragged.emit(self.line)
        if event.isFinish():
            self.line.moving = False
            self.line.sigPositionChangeFinished.emit(self.line)


class MeasurementCursor(pg.InfiniteLine):
    """A thin vertical line with an 18-pixel hit band and an A/B handle."""

    def __init__(self, name: str, color: str):
        super().__init__(angle=90, movable=True,
                         pen=pg.mkPen(color, width=1.5),
                         hoverPen=pg.mkPen(color, width=2.5))
        self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        self.setToolTip(f"左右拖动游标 {name} 或其标签")
        # Opposite ends keep both handles accessible when A and B coincide.
        self.label = _CursorHandle(self, text=name,
                                   position=.94 if name == "A" else .06,
                                   anchors=[(.5, .5), (.5, .5)],
                                   color="#FFFFFF", fill=color, border=color)
        font = QtGui.QFont()
        font.setPointSize(10)
        font.setBold(True)
        self.label.setFont(font)
        self.label.setZValue(1)
        self.label.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        self.label.setToolTip(f"拖动标签 {name}：左右移动游标，上下调整标签位置")

    def _computeBoundingRect(self):
        rect = QtCore.QRectF(super()._computeBoundingRect())
        _, orthogonal = self.pixelVectors(direction=QtCore.QPointF(1, 0))
        if orthogonal is not None:
            radius = abs(orthogonal.y()) * 9
            rect.setTop(min(rect.top(), -radius))
            rect.setBottom(max(rect.bottom(), radius))
        return rect

    def mouseDragEvent(self, event):
        if not self.movable or event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        if event.isStart():
            self.moving = True
            self.cursorOffset = self.value() - self.mapToParent(event.buttonDownPos()).x()
            self.startPosition = self.pos()
        event.accept()
        if not self.moving:
            return
        # InfiniteLine's default drag changes its hidden y origin too. Keep the
        # origin fixed so vertical motion cannot displace the label oppositely.
        self.setValue(self.cursorOffset + self.mapToParent(event.pos()).x())
        self.sigDragged.emit(self)
        if event.isFinish():
            self.moving = False
            self.sigPositionChangeFinished.emit(self)
