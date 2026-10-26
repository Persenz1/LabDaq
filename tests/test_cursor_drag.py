"""Mouse interaction regressions for measurement cursor hit areas and handles."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets as W

from labdaq.measurement_cursor import MeasurementCursor


@pytest.fixture
def cursor_plot():
    app = W.QApplication.instance() or W.QApplication([])
    mouse_rate = pg.getConfigOption("mouseRateLimit")
    pg.setConfigOption("mouseRateLimit", 0)
    plot = pg.PlotWidget()
    plot.resize(800, 400)
    plot.setRange(xRange=(0, 1), yRange=(-1, 1), padding=0)
    lines = [MeasurementCursor("A", "#DD8800"), MeasurementCursor("B", "#8855EE")]
    for line, value in zip(lines, (.3, .7)):
        plot.addItem(line, ignoreBounds=True)
        line.setZValue(30)
        line.setValue(value)
    plot.show()
    QtTest.QTest.qWait(30)  # Let initial axis and label layout settle.
    yield app, plot, lines
    plot.close()
    plot.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    pg.setConfigOption("mouseRateLimit", mouse_rate)


def drag(app, plot, start, distance, vertical=0):
    viewport = plot.viewport()
    left = QtCore.Qt.MouseButton.LeftButton
    none = QtCore.Qt.MouseButton.NoButton
    end = start + QtCore.QPoint(distance, vertical)
    for kind, point, button, buttons in (
        (QtCore.QEvent.Type.MouseMove, start, none, none),
        (QtCore.QEvent.Type.MouseButtonPress, start, left, left),
        (QtCore.QEvent.Type.MouseMove, end, none, left),
        (QtCore.QEvent.Type.MouseButtonRelease, end, left, none),
    ):
        event = QtGui.QMouseEvent(kind, QtCore.QPointF(point),
                                 QtCore.QPointF(viewport.mapToGlobal(point)),
                                 button, buttons, QtCore.Qt.KeyboardModifier.NoModifier)
        W.QApplication.sendEvent(viewport, event)
        app.processEvents()


@pytest.mark.parametrize("origin,span", [(0, 1), (1792370000., 1), (0, 5000)])
def test_drag_near_line_moves_cursor_without_panning(cursor_plot, origin, span):
    app, plot, (line_a, line_b) = cursor_plot
    view = plot.getViewBox()
    plot.setRange(xRange=(origin, origin + span), padding=0)
    line_a.setValue(origin + .3 * span)
    line_b.setValue(origin + .7 * span)
    app.processEvents()
    before = view.viewRange()
    start = plot.mapFromScene(view.mapViewToScene(QtCore.QPointF(line_a.value(), 0)))
    # The former thin-line hit area missed a press eight pixels off the line.
    drag(app, plot, start + QtCore.QPoint(8, 0), 90)
    assert line_a.value() > origin + .4 * span
    assert line_b.value() == origin + .7 * span
    assert view.viewRange() == before


def test_coincident_cursors_can_be_dragged_independently_by_handle(cursor_plot):
    app, plot, (line_a, line_b) = cursor_plot
    line_b.setValue(line_a.value())
    app.processEvents()
    for line, other, distance in ((line_a, line_b, 70), (line_b, line_a, -70)):
        start_value, other_value = line.value(), other.value()
        handle_center = line.label.mapToScene(line.label.boundingRect().center())
        drag(app, plot, plot.mapFromScene(handle_center), distance)
        assert (line.value() - start_value) * distance > 0
        assert other.value() == other_value


def test_drag_away_from_cursors_still_pans_plot(cursor_plot):
    app, plot, lines = cursor_plot
    view = plot.getViewBox()
    before = view.viewRange()[0]
    start = plot.mapFromScene(view.mapViewToScene(QtCore.QPointF(.5, 0)))
    drag(app, plot, start, 60)
    assert view.viewRange()[0] != before
    assert [line.value() for line in lines] == [.3, .7]


def test_vertical_line_drag_does_not_move_label_or_change_line_origin(cursor_plot):
    app, plot, (line_a, _) = cursor_plot
    view = plot.getViewBox()
    label_y = line_a.label.sceneBoundingRect().center().y()
    start = plot.mapFromScene(view.mapViewToScene(QtCore.QPointF(.3, 0)))
    drag(app, plot, start, 0, 60)
    assert line_a.value() == pytest.approx(.3)
    assert line_a.pos().y() == 0
    assert line_a.label.sceneBoundingRect().center().y() == pytest.approx(label_y)


@pytest.mark.parametrize("horizontal,vertical", [(0, 60), (70, 0), (70, 60)])
def test_label_drag_moves_label_with_mouse_and_line_only_horizontally(cursor_plot, horizontal, vertical):
    app, plot, (line_a, line_b) = cursor_plot
    label_before = line_a.label.sceneBoundingRect().center()
    start = plot.mapFromScene(label_before)
    drag(app, plot, start, horizontal, vertical)
    label_after = line_a.label.sceneBoundingRect().center()
    assert label_after.x() - label_before.x() == pytest.approx(horizontal, abs=1)
    assert label_after.y() - label_before.y() == pytest.approx(vertical, abs=1)
    assert line_a.pos().y() == 0
    assert line_b.value() == .7
    if horizontal:
        assert line_a.value() > .3
    else:
        assert line_a.value() == pytest.approx(.3)


def test_label_stays_inside_plot_and_keeps_height_after_zoom(cursor_plot):
    app, plot, (line_a, _) = cursor_plot
    view = plot.getViewBox()
    for distance in (800, -800):
        drag(app, plot, plot.mapFromScene(line_a.label.sceneBoundingRect().center()), 0, distance)
        rect = line_a.label.sceneBoundingRect()
        bounds = view.sceneBoundingRect()
        assert rect.top() >= bounds.top() - 1
        assert rect.bottom() <= bounds.bottom() + 1
    relative = line_a.label.orthoPos
    plot.setRange(xRange=(.1, .5), yRange=(-3, 3), padding=0)
    QtTest.QTest.qWait(30)
    assert line_a.label.orthoPos == relative
    assert line_a.pos().y() == 0
    assert line_a.label.sceneBoundingRect().center().x() == pytest.approx(
        view.mapViewToScene(QtCore.QPointF(line_a.value(), 0)).x(), abs=1)
