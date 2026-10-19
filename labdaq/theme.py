"""Design tokens, switchable palettes, line icons and the global Qt stylesheet.

The window structure never hard-codes colors.  Every widget pulls its look from
the active :class:`Palette`, so the whole application can be re-skinned by
changing one name (``LABDAQ_THEME``).  Palettes shipped here:

``saas``      light SaaS workspace with a near-white rail (default)
``navy``      deep-blue navigation rail with a light workspace
``midnight``  fully dark instrument console
``daylight``  bright, quiet laboratory look
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from string import Template

from PySide6 import QtCore, QtGui, QtWidgets as W


@dataclass(frozen=True)
class Palette:
    """One complete color set.  All color values are CSS hex strings."""

    name: str
    label: str
    dark: bool

    bg_top: str
    bg_bottom: str

    surface: str
    surface_alt: str
    surface_hover: str
    input_bg: str

    sidebar_top: str
    sidebar_bottom: str
    sidebar_hover: str
    sidebar_selected: str
    sidebar_text: str
    sidebar_muted: str
    sidebar_icon: str
    sidebar_icon_active: str
    sidebar_border: str
    device_bg: str
    device_border: str

    text: str
    text_muted: str
    text_faint: str
    border: str
    border_strong: str

    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    accent_text: str

    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    info_soft: str

    plot_bg: str
    plot_fg: str
    plot_grid: str

    channels: tuple[str, ...]
    radius: int = 14

    @property
    def radius_small(self) -> int:
        return max(4, self.radius - 5)

    @property
    def font_ui(self) -> str:
        return "Microsoft YaHei UI"


PALETTES: dict[str, Palette] = {
    "saas": Palette(
        name="saas",
        label="浅色 SaaS · 柔和卡片",
        dark=False,
        bg_top="#F8F9FE",
        bg_bottom="#F1F5FE",
        surface="#FFFFFF",
        surface_alt="#F6F8FD",
        surface_hover="#EEF3FF",
        input_bg="#FFFFFF",
        sidebar_top="#FDFDFF",
        sidebar_bottom="#F7F9FF",
        sidebar_hover="#F1F4FC",
        sidebar_selected="#EDF2FF",
        sidebar_text="#293247",
        sidebar_muted="#8A94A8",
        sidebar_icon="#8A94A8",
        sidebar_icon_active="#3E6DF6",
        sidebar_border="#ECF0F8",
        device_bg="#F5F8FE",
        device_border="#E4EAF6",
        text="#1E2433",
        text_muted="#6B7488",
        text_faint="#9AA3B5",
        border="#E8ECF4",
        border_strong="#D5DDEA",
        accent="#3E6DF6",
        accent_hover="#2F5BE8",
        accent_pressed="#2A50CC",
        accent_soft="#EDF2FF",
        accent_text="#FFFFFF",
        success="#10B981",
        success_soft="#E8F8F2",
        warning="#D97706",
        warning_soft="#FFF6E8",
        danger="#E5484D",
        danger_soft="#FDECEC",
        info_soft="#EEF4FF",
        plot_bg="#FFFFFF",
        plot_fg="#6B7488",
        plot_grid="#EAEEF6",
        channels=("#3B82F6", "#10B981", "#F59E0B", "#8B5CF6"),
    ),
    "navy": Palette(
        name="navy",
        label="深蓝侧栏 · 浅色工作区",
        dark=False,
        bg_top="#F3F6FA",
        bg_bottom="#EDF2FA",
        surface="#FFFFFF",
        surface_alt="#F6F8FC",
        surface_hover="#EDF3FE",
        input_bg="#FFFFFF",
        sidebar_top="#0E1D31",
        sidebar_bottom="#13263E",
        sidebar_hover="#172B45",
        sidebar_selected="#1C3552",
        sidebar_text="#E2EAF5",
        sidebar_muted="#8095B0",
        sidebar_icon="#7E93AE",
        sidebar_icon_active="#FFFFFF",
        sidebar_border="#0A1626",
        device_bg="#172B45",
        device_border="#24405F",
        text="#1A2A41",
        text_muted="#65768E",
        text_faint="#95A3B6",
        border="#E1E8F1",
        border_strong="#C9D5E4",
        accent="#2563EB",
        accent_hover="#1D4ED8",
        accent_pressed="#1B44BC",
        accent_soft="#EAF1FE",
        accent_text="#FFFFFF",
        success="#0E9F7A",
        success_soft="#E6F7F1",
        warning="#B26A12",
        warning_soft="#FFF4DE",
        danger="#CE3F4A",
        danger_soft="#FDECEE",
        info_soft="#EDF4FD",
        plot_bg="#FFFFFF",
        plot_fg="#5C7089",
        plot_grid="#D9E2EE",
        channels=("#2563EB", "#0E9F7A", "#DE8A2B", "#7B4FD1"),
        radius=12,
    ),
    "midnight": Palette(
        name="midnight",
        label="全深色仪器风",
        dark=True,
        bg_top="#0A1220",
        bg_bottom="#0C1526",
        surface="#111C2E",
        surface_alt="#16233A",
        surface_hover="#1B2C47",
        input_bg="#0D1828",
        sidebar_top="#070E19",
        sidebar_bottom="#0A1524",
        sidebar_hover="#142033",
        sidebar_selected="#1A2E4B",
        sidebar_text="#E4EDF9",
        sidebar_muted="#7D93AE",
        sidebar_icon="#6E849F",
        sidebar_icon_active="#FFFFFF",
        sidebar_border="#050A12",
        device_bg="#101B2C",
        device_border="#22344E",
        text="#E7EEF9",
        text_muted="#9BAFC7",
        text_faint="#6D839D",
        border="#213149",
        border_strong="#324763",
        accent="#4C9CFF",
        accent_hover="#6BB0FF",
        accent_pressed="#3A8BEF",
        accent_soft="#162C4A",
        accent_text="#081422",
        success="#35C79B",
        success_soft="#0F2D27",
        warning="#E8B256",
        warning_soft="#302512",
        danger="#FF7D86",
        danger_soft="#3A1D22",
        info_soft="#14273F",
        plot_bg="#0D1727",
        plot_fg="#9CB2CC",
        plot_grid="#22324A",
        channels=("#5AA2FF", "#35C79B", "#F0A94A", "#B78CFF"),
        radius=10,
    ),
    "daylight": Palette(
        name="daylight",
        label="明亮浅色极简",
        dark=False,
        bg_top="#F7F8FA",
        bg_bottom="#F2F5F9",
        surface="#FFFFFF",
        surface_alt="#F4F6FA",
        surface_hover="#EFF4FF",
        input_bg="#FFFFFF",
        sidebar_top="#FFFFFF",
        sidebar_bottom="#FBFCFE",
        sidebar_hover="#F0F4FA",
        sidebar_selected="#E9F1FE",
        sidebar_text="#22324A",
        sidebar_muted="#7A8AA1",
        sidebar_icon="#8494AA",
        sidebar_icon_active="#2563EB",
        sidebar_border="#E8ECF4",
        device_bg="#F4F6FA",
        device_border="#E2E7F0",
        text="#22324A",
        text_muted="#6A7A91",
        text_faint="#9AA7B8",
        border="#E5E9F0",
        border_strong="#CBD4E1",
        accent="#2F6BFF",
        accent_hover="#1F5AF0",
        accent_pressed="#1B4FD8",
        accent_soft="#EDF3FF",
        accent_text="#FFFFFF",
        success="#0E9F7A",
        success_soft="#E8F8F2",
        warning="#B26A12",
        warning_soft="#FFF4DE",
        danger="#D1495B",
        danger_soft="#FDEEF0",
        info_soft="#EFF5FF",
        plot_bg="#FFFFFF",
        plot_fg="#66788F",
        plot_grid="#DEE5EF",
        channels=("#2F6BFF", "#0E9F7A", "#DE8A2B", "#7B4FD1"),
        radius=12,
    ),
}

DEFAULT_THEME = "saas"
THEME_ENV = "LABDAQ_THEME"


def available() -> tuple[str, ...]:
    return tuple(PALETTES)


def palette(name: str | None = None) -> Palette:
    """Return a palette by name, falling back to the environment default."""
    key = name or os.environ.get(THEME_ENV) or DEFAULT_THEME
    return PALETTES.get(key, PALETTES[DEFAULT_THEME])


def current(app: W.QApplication | None = None) -> Palette:
    app = app or W.QApplication.instance()
    stored = getattr(app, "_labdaq_palette", None) if app is not None else None
    return stored if isinstance(stored, Palette) else palette()


# --------------------------------------------------------------------------
# Color helpers


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def blend(first: str, second: str, amount: float) -> str:
    """Mix two hex colors; ``amount`` is the weight of ``second`` (0–1)."""
    amount = min(1.0, max(0.0, amount))
    a, b = _rgb(first), _rgb(second)
    return "#" + "".join(f"{round(x + (y - x) * amount):02X}" for x, y in zip(a, b))


def alpha(color: str, value: float) -> QtGui.QColor:
    result = QtGui.QColor(color)
    result.setAlphaF(min(1.0, max(0.0, value)))
    return result


def tone_color(p: Palette, tone: str) -> tuple[str, str]:
    """Foreground and soft background for a status tone."""
    return {
        "info": (p.accent, p.info_soft),
        "success": (p.success, p.success_soft),
        "warning": (p.warning, p.warning_soft),
        "danger": (p.danger, p.danger_soft),
        "neutral": (p.text_muted, p.surface_alt),
    }.get(tone, (p.text_muted, p.surface_alt))


def repolish(widget: W.QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_tone(widget: W.QWidget, tone: str) -> None:
    """Set the ``tone`` dynamic property used by badge/notice styles."""
    if widget.property("tone") == tone:
        return
    widget.setProperty("tone", tone)
    repolish(widget)


def soft_shadow(widget: W.QWidget, *, blur: int = 26, dy: int = 7, strength: int = 26) -> None:
    """Attach a soft drop shadow; use on small/static cards, not the live plot."""
    effect = W.QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    p = current()
    if p.dark:
        effect.setColor(QtGui.QColor(0, 0, 0, min(255, strength + 35)))
    else:
        effect.setColor(QtGui.QColor(15, 23, 42, strength))
    widget.setGraphicsEffect(effect)


# --------------------------------------------------------------------------
# Fonts


def mono_font(points: int = 10, weight: int = 400) -> QtGui.QFont:
    """Pick an installed monospaced face so readouts do not jitter."""
    families = set(QtGui.QFontDatabase.families())
    for name in ("Cascadia Mono", "Cascadia Code", "Consolas", "JetBrains Mono", "DejaVu Sans Mono"):
        if name in families:
            font = QtGui.QFont(name, points)
            font.setWeight(QtGui.QFont.Weight(weight))
            return font
    font = QtGui.QFont("Microsoft YaHei UI", points)
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setWeight(QtGui.QFont.Weight(weight))
    return font


def _register_fonts() -> None:
    """Offscreen Windows Qt fails to discover CJK faces; add them explicitly."""
    if "Microsoft YaHei UI" in set(QtGui.QFontDatabase.families()):
        return
    windows = os.environ.get("WINDIR", "C:/Windows")
    for name in ("msyh.ttc", "msyhbd.ttc"):
        path = os.path.join(windows, "Fonts", name)
        if os.path.exists(path):
            QtGui.QFontDatabase.addApplicationFont(path)


# --------------------------------------------------------------------------
# Icons: painted in a 24×24 grid so they stay crisp and share one stroke weight


def _pen(color: str, width: float = 2.0) -> QtGui.QPen:
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    return pen


def _polyline(g: QtGui.QPainter, points) -> None:
    path = QtGui.QPainterPath(QtCore.QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QtCore.QPointF(*point))
    g.drawPath(path)


def _draw_icon(g: QtGui.QPainter, name: str, color: str) -> None:
    g.setPen(_pen(color, 2.0))
    no_brush = QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush)
    g.setBrush(no_brush)
    filled = QtGui.QBrush(QtGui.QColor(color))
    if name in ("wave", "wave_small"):
        _polyline(g, ((2, 15), (5.5, 15), (8, 7.5), (12, 19), (15.5, 10), (18.5, 15), (22, 15)))
    elif name == "bars":
        for x, top in ((5, 15), (9, 8), (13, 12), (17, 5)):
            g.drawLine(QtCore.QPointF(x, 19), QtCore.QPointF(x, top))
    elif name == "stats":
        g.drawLine(QtCore.QPointF(4, 20), QtCore.QPointF(20, 20))
        g.drawLine(QtCore.QPointF(4, 20), QtCore.QPointF(4, 4))
        for x, top in ((8, 13), (12.5, 7), (17, 10.5)):
            g.drawLine(QtCore.QPointF(x, 19.5), QtCore.QPointF(x, top))
    elif name in ("history", "clock"):
        g.drawEllipse(QtCore.QPointF(12, 12), 8.5, 8.5)
        g.drawLine(QtCore.QPointF(12, 7), QtCore.QPointF(12, 12))
        g.drawLine(QtCore.QPointF(12, 12), QtCore.QPointF(16, 14))
    elif name in ("chip", "device"):
        g.drawRoundedRect(QtCore.QRectF(6, 6, 12, 12), 2.2, 2.2)
        for value in (9, 12, 15):
            g.drawLine(QtCore.QPointF(2.5, value), QtCore.QPointF(6, value))
            g.drawLine(QtCore.QPointF(18, value), QtCore.QPointF(21.5, value))
            g.drawLine(QtCore.QPointF(value, 2.5), QtCore.QPointF(value, 6))
            g.drawLine(QtCore.QPointF(value, 18), QtCore.QPointF(value, 21.5))
    elif name == "play":
        g.setBrush(filled)
        g.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(8, 5), QtCore.QPointF(19, 12), QtCore.QPointF(8, 19)]))
    elif name == "record":
        g.setBrush(filled)
        g.drawEllipse(QtCore.QPointF(12, 12), 6.4, 6.4)
    elif name == "pause":
        g.setBrush(filled)
        g.drawRoundedRect(QtCore.QRectF(7, 4.5, 3.6, 15), 1.6, 1.6)
        g.drawRoundedRect(QtCore.QRectF(13.4, 4.5, 3.6, 15), 1.6, 1.6)
    elif name == "stop":
        g.setBrush(filled)
        g.drawRoundedRect(QtCore.QRectF(6, 6, 12, 12), 2.4, 2.4)
    elif name in ("save", "download"):
        g.drawLine(QtCore.QPointF(12, 3.5), QtCore.QPointF(12, 13))
        _polyline(g, ((8, 9.5), (12, 13.5), (16, 9.5)))
        _polyline(g, ((4.5, 15), (4.5, 20), (19.5, 20), (19.5, 15)))
    elif name == "export":
        g.drawLine(QtCore.QPointF(12, 17), QtCore.QPointF(12, 6))
        _polyline(g, ((8, 10), (12, 6), (16, 10)))
        _polyline(g, ((4.5, 16), (4.5, 20), (19.5, 20), (19.5, 16)))
    elif name == "fit":
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            cx, cy = 12 + sx * 8, 12 + sy * 8
            g.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(cx - sx * 4.5, cy))
            g.drawLine(QtCore.QPointF(cx, cy), QtCore.QPointF(cx, cy - sy * 4.5))
    elif name in ("folder", "open", "library"):
        path = QtGui.QPainterPath(QtCore.QPointF(3, 7))
        for point in ((9, 7), (11, 9.5), (21, 9.5), (21, 19), (3, 19)):
            path.lineTo(QtCore.QPointF(*point))
        path.closeSubpath()
        g.drawPath(path)
        if name == "open":
            g.drawLine(QtCore.QPointF(7, 13), QtCore.QPointF(17, 13))
            _polyline(g, ((13.5, 9.5), (17, 13), (13.5, 16.5)))
        if name == "library":
            g.drawLine(QtCore.QPointF(7, 13), QtCore.QPointF(17, 13))
            g.drawLine(QtCore.QPointF(7, 16), QtCore.QPointF(14, 16))
    elif name in ("settings", "sliders"):
        pairs = ((7, 8), (12, 15.5), (17, 10.5)) if name == "settings" else ((7, 9), (12, 14.5), (17, 7.5))
        for y, knob in pairs:
            g.drawLine(QtCore.QPointF(4, y), QtCore.QPointF(20, y))
            g.drawEllipse(QtCore.QPointF(knob, y), 2.1, 2.1)
    elif name == "refresh":
        path = QtGui.QPainterPath(QtCore.QPointF(19, 12))
        path.arcTo(QtCore.QRectF(5, 5, 14, 14), 0, 290)
        g.drawPath(path)
        g.setBrush(filled)
        g.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(19, 3.6), QtCore.QPointF(20.4, 9.2), QtCore.QPointF(15.2, 7.4)]))
    elif name in ("zero", "target"):
        g.drawEllipse(QtCore.QPointF(12, 12), 7.5, 7.5)
        g.drawEllipse(QtCore.QPointF(12, 12), 2.4, 2.4)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            g.drawLine(QtCore.QPointF(12 + dx * 8.5, 12 + dy * 8.5), QtCore.QPointF(12 + dx * 11.5, 12 + dy * 11.5))
    elif name == "undo":
        path = QtGui.QPainterPath(QtCore.QPointF(5, 12))
        path.arcTo(QtCore.QRectF(5, 5, 14, 14), 180, -250)
        g.drawPath(path)
        g.setBrush(filled)
        g.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(4.6, 4.4), QtCore.QPointF(10.2, 5.8), QtCore.QPointF(6.4, 10.4)]))
    elif name == "chevron_down":
        _polyline(g, ((6.5, 9), (12, 15), (17.5, 9)))
    elif name == "chevron_left":
        _polyline(g, ((15, 6.5), (9, 12), (15, 17.5)))
    elif name == "chevron_right":
        _polyline(g, ((9, 6.5), (15, 12), (9, 17.5)))
    elif name == "more":
        g.setBrush(filled)
        for x in (6.2, 12, 17.8):
            g.drawEllipse(QtCore.QPointF(x, 12), 1.5, 1.5)
    elif name == "edit":
        _polyline(g, ((4, 20), (5.2, 14.8), (15.6, 4.4), (19.6, 8.4), (9.2, 18.8), (4, 20)))
        g.drawLine(QtCore.QPointF(13.4, 6.6), QtCore.QPointF(17.4, 10.6))
    elif name == "info":
        g.drawEllipse(QtCore.QPointF(12, 12), 8.5, 8.5)
        g.drawLine(QtCore.QPointF(12, 11), QtCore.QPointF(12, 16.5))
        g.setBrush(filled)
        g.drawEllipse(QtCore.QPointF(12, 7.7), 1.1, 1.1)
    elif name == "check":
        _polyline(g, ((5, 12.5), (10, 17.5), (19, 6.5)))
    elif name == "warning":
        g.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(12, 3.5), QtCore.QPointF(21.5, 20), QtCore.QPointF(2.5, 20)]))
        g.drawLine(QtCore.QPointF(12, 9), QtCore.QPointF(12, 14.5))
        g.setBrush(filled)
        g.drawEllipse(QtCore.QPointF(12, 17.2), 1.15, 1.15)
    elif name == "error":
        g.drawEllipse(QtCore.QPointF(12, 12), 8.6, 8.6)
        g.drawLine(QtCore.QPointF(8.6, 8.6), QtCore.QPointF(15.4, 15.4))
        g.drawLine(QtCore.QPointF(15.4, 8.6), QtCore.QPointF(8.6, 15.4))
    elif name in ("dot", "link", "usb"):
        g.setBrush(filled)
        g.drawEllipse(QtCore.QPointF(12, 12), 3.4, 3.4)


_ICON_CACHE: dict[tuple[str, str, int], QtGui.QIcon] = {}
_ASSET_DIR = Path(__file__).resolve().parents[1] / ".runtime" / "theme"


def _asset(name: str, color: str, size: int = 11) -> str:
    """Write a themed icon to the runtime folder so QSS can reference it."""
    _ASSET_DIR.mkdir(parents=True, exist_ok=True)
    target = _ASSET_DIR / f"{name}-{color.lstrip('#')}-{size}.png"
    if not target.exists():
        icon(name, color, size).pixmap(size, size).save(str(target), "PNG")
    return target.as_posix()


def icon(name: str, color: str, size: int = 18) -> QtGui.QIcon:
    key = (name, color, size)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    pixmap = QtGui.QPixmap(size * 2, size * 2)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2.0)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24.0, size / 24.0)
    _draw_icon(painter, name, color)
    painter.end()
    result = QtGui.QIcon(pixmap)
    _ICON_CACHE[key] = result
    return result


def dot_pixmap(color: str, size: int = 9) -> QtGui.QPixmap:
    pixmap = QtGui.QPixmap(size * 2, size * 2)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2.0)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QBrush(QtGui.QColor(color)))
    painter.drawEllipse(QtCore.QPointF(size / 2, size / 2), size / 2 - 0.5, size / 2 - 0.5)
    painter.end()
    return pixmap


def logo_pixmap(p: Palette, size: int = 38) -> QtGui.QPixmap:
    """Rounded accent tile with the LabDAQ waveform mark."""
    pixmap = QtGui.QPixmap(size * 2, size * 2)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2.0)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QBrush(QtGui.QColor(p.accent)))
    painter.drawRoundedRect(QtCore.QRectF(0, 0, size, size), size * 0.29, size * 0.29)
    painter.setPen(_pen(p.accent_text, 1.4))
    scale = size / 24.0
    painter.translate(size * 0.5, size * 0.5)
    painter.scale(scale, scale)
    painter.translate(-12, -12)
    _polyline(painter, ((4, 14), (8, 14), (10, 7.5), (13, 17), (15.5, 10.5), (20, 14)))
    painter.end()
    return pixmap


# --------------------------------------------------------------------------
# Qt palette + stylesheet


def qpalette(p: Palette) -> QtGui.QPalette:
    pal = QtGui.QPalette()
    roles = {
        QtGui.QPalette.ColorRole.Window: p.bg_top,
        QtGui.QPalette.ColorRole.WindowText: p.text,
        QtGui.QPalette.ColorRole.Base: p.input_bg,
        QtGui.QPalette.ColorRole.AlternateBase: p.surface_alt,
        QtGui.QPalette.ColorRole.Text: p.text,
        QtGui.QPalette.ColorRole.Button: p.surface,
        QtGui.QPalette.ColorRole.ButtonText: p.text,
        QtGui.QPalette.ColorRole.BrightText: p.surface,
        QtGui.QPalette.ColorRole.Highlight: p.accent,
        QtGui.QPalette.ColorRole.HighlightedText: p.accent_text,
        QtGui.QPalette.ColorRole.ToolTipBase: p.text,
        QtGui.QPalette.ColorRole.ToolTipText: p.surface,
        QtGui.QPalette.ColorRole.PlaceholderText: p.text_faint,
        QtGui.QPalette.ColorRole.Link: p.accent,
        QtGui.QPalette.ColorRole.Light: p.border,
        QtGui.QPalette.ColorRole.Midlight: p.border,
        QtGui.QPalette.ColorRole.Mid: p.border_strong,
        QtGui.QPalette.ColorRole.Dark: p.text_muted,
    }
    for role, color in roles.items():
        pal.setColor(role, QtGui.QColor(color))
    for role, color in (
        (QtGui.QPalette.ColorRole.Text, p.text_faint),
        (QtGui.QPalette.ColorRole.ButtonText, p.text_faint),
        (QtGui.QPalette.ColorRole.WindowText, p.text_faint),
        (QtGui.QPalette.ColorRole.Base, p.surface_alt),
    ):
        pal.setColor(QtGui.QPalette.ColorGroup.Disabled, role, QtGui.QColor(color))
    return pal


_STYLESHEET = Template(
    """
QWidget {
    font-family: '$font_ui';
    font-size: 13px;
    color: $text;
}
QMainWindow, QWidget#workspace {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 $bg_top, stop:1 $bg_bottom);
}
QDialog { background: $bg_bottom; }
QWidget#pageHost, QWidget#panelContent, QWidget#transparentHost { background: transparent; }

/* ---------- navigation rail ---------- */
QFrame#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 $sidebar_top, stop:1 $sidebar_bottom);
    border: none; border-right: 1px solid $sidebar_border;
}
QFrame#sidebar QLabel { color: $sidebar_text; background: transparent; }
QFrame#sidebar QLabel#brand { color: $sidebar_text; font-size: 22px; font-weight: 700; }
QFrame#sidebar QLabel#sideCaption { color: $sidebar_muted; font-size: 11px; }
QFrame#sidebar QLabel#sideSection { color: $sidebar_muted; font-size: 10px; font-weight: 600; }
QFrame#sidebar QLabel#deviceName { color: $sidebar_text; font-size: 12px; font-weight: 600; }
QFrame#sidebar QLabel#deviceMeta { color: $sidebar_muted; font-size: 11px; }
QFrame#sidebar QFrame#deviceCard {
    background: $device_bg; border: 1px solid $device_border; border-radius: ${radius_small}px;
}
QFrame#sidebar QFrame#deviceCard QLabel { background: transparent; }
QListWidget#nav {
    background: transparent; border: none; color: $sidebar_muted; outline: 0; font-size: 13px;
}
QListWidget#nav::item { padding: 10px 12px; margin: 2px 0; border-radius: 9px; }
QListWidget#nav::item:selected {
    background: $sidebar_selected; color: $sidebar_text;
    border-left: 3px solid $accent;
}
QListWidget#nav::item:hover { background: $sidebar_hover; color: $sidebar_text; }

/* ---------- surfaces ---------- */
QFrame#card, QFrame#metricCard, QFrame#commandBar, QFrame#panel {
    background: $surface; border: 1px solid $border; border-radius: ${radius}px;
}
QFrame#card QLabel, QFrame#metricCard QLabel, QFrame#panel QLabel,
QFrame#commandBar QLabel { background: transparent; }
QFrame#divider { background: $border; border: none; max-height: 1px; min-height: 1px; }
QFrame#vDivider { background: $border; border: none; max-width: 1px; min-width: 1px; }
QFrame#statusStrip {
    background: $surface_alt; border: 1px solid $border; border-radius: ${radius_small}px;
}
QFrame#statusStrip QLabel { background: transparent; }
QScrollArea#panelScroll { background: transparent; border: none; }
QScrollArea#panelScroll QWidget#qt_scrollarea_viewport { background: transparent; }
QToolTip {
    background: $text; color: $surface; border: none; padding: 6px 8px; border-radius: 4px;
}

/* ---------- typography ---------- */
QLabel#pageTitle { font-size: 24px; font-weight: 700; color: $text; }
QLabel#pageSubtitle { color: $text_muted; font-size: 12px; }
QLabel#sectionTitle { font-size: 15px; font-weight: 700; color: $text; }
QLabel#subSectionTitle { font-size: 11px; font-weight: 600; color: $text_muted; }
QLabel#muted { color: $text_muted; font-size: 12px; }
QLabel#faint { color: $text_faint; font-size: 11px; }
QLabel#metric { font-size: 32px; font-weight: 600; }
QLabel#metricTitle { color: $text; font-size: 12px; font-weight: 600; }
QLabel#metricName { color: $text_muted; font-size: 11px; }
QLabel#metricUnit { color: $text_muted; font-size: 14px; padding-bottom: 4px; }
QLabel#unitChip {
    background: $surface_alt; border: 1px solid $border; border-radius: 5px;
    color: $text_muted; font-size: 11px; padding: 1px 6px;
}
QLabel#fieldLabel { color: $text_muted; font-size: 11px; font-weight: 600; }
QLabel#valueMono { font-family: 'Consolas'; font-size: 12px; color: $text; }
QLabel#keyHint { color: $text_faint; font-size: 10px; }
QLabel#statusStripText { color: $text_muted; font-size: 12px; }
QLabel#legendLabel { color: $text_muted; font-size: 11px; }
QLabel#legendValue { color: $text; font-size: 11px; font-weight: 600; }

QLabel#badge {
    background: $warning_soft; border: 1px solid $warning_border; border-radius: 8px;
    color: $warning; padding: 5px 11px; font-size: 12px; font-weight: 600;
}
QLabel#badge[tone="info"] { background: $info_soft; border-color: $accent_border; color: $accent; }
QLabel#badge[tone="success"] { background: $success_soft; border-color: $success_border; color: $success; }
QLabel#badge[tone="danger"] { background: $danger_soft; border-color: $danger_border; color: $danger; }
QLabel#badge[tone="neutral"] { background: $surface_alt; border-color: $border; color: $text_muted; }

QLabel#statusPill {
    background: $surface_alt; border: 1px solid $border; border-radius: 11px;
    color: $text_muted; padding: 3px 10px; font-size: 11px; font-weight: 600;
}
QLabel#statusPill[tone="info"] { background: $info_soft; border-color: $accent_border; color: $accent; }
QLabel#statusPill[tone="success"] { background: $success_soft; border-color: $success_border; color: $success; }
QLabel#statusPill[tone="warning"] { background: $warning_soft; border-color: $warning_border; color: $warning; }
QLabel#statusPill[tone="danger"] { background: $danger_soft; border-color: $danger_border; color: $danger; }

QLabel#notice {
    background: $surface_alt; border: 1px solid $border; border-radius: ${radius_small}px;
    color: $text_muted; padding: 7px 11px; font-size: 12px;
}
QLabel#notice[tone="info"] { background: $info_soft; border-color: $accent_border; color: $text; }
QLabel#notice[tone="success"] { background: $success_soft; border-color: $success_border; color: $text; }
QLabel#notice[tone="warning"] { background: $warning_soft; border-color: $warning_border; color: $text; }
QLabel#notice[tone="danger"] { background: $danger_soft; border-color: $danger_border; color: $text; }

/* ---------- buttons ---------- */
QPushButton {
    background: $surface; color: $text; border: 1px solid $border_strong;
    border-radius: ${radius_small}px; padding: 7px 12px; font-weight: 500;
}
QPushButton:hover { background: $surface_hover; border-color: $accent; color: $accent; }
QPushButton:pressed { background: $accent_soft; border-color: $accent; }
QPushButton:focus { outline: none; }
QPushButton:disabled { background: $surface_alt; color: $text_faint; border-color: $border; }
QPushButton:checked { background: $accent_soft; border-color: $accent; color: $accent; }

QPushButton#primary {
    background: $accent; color: $accent_text; border-color: $accent; font-weight: 600;
    padding: 8px 17px;
}
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; color: $accent_text; }
QPushButton#primary:pressed { background: $accent_pressed; border-color: $accent_pressed; }
QPushButton#primary:disabled {
    background: $accent_disabled; border-color: $accent_disabled; color: $accent_text_muted;
}

QPushButton#record {
    background: $surface; color: $danger; border-color: $danger_border; font-weight: 600;
    padding: 8px 17px;
}
QPushButton#record:hover { background: $danger_soft; border-color: $danger; color: $danger; }
QPushButton#record:checked, QPushButton#record:pressed {
    background: $danger; border-color: $danger; color: $danger_text;
}
QPushButton#record:disabled { background: $surface_alt; color: $text_faint; border-color: $border; }

QPushButton#danger { color: $danger; border-color: $danger_border; }
QPushButton#danger:hover { background: $danger_soft; border-color: $danger; color: $danger; }

QPushButton#ghost {
    background: transparent; border-color: transparent; color: $text_muted;
}
QPushButton#ghost:hover { background: $surface_hover; color: $accent; border-color: transparent; }
QPushButton#ghost:disabled { background: transparent; color: $text_faint; border-color: transparent; }

QPushButton#transport { padding: 9px 15px; }
QPushButton#chip {
    background: $surface_alt; border: 1px solid $border; border-radius: 14px;
    padding: 4px 11px; font-size: 11px; font-weight: 500; color: $text_muted;
}
QPushButton#chip:hover { background: $accent_soft; border-color: $accent_border; color: $accent; }
QPushButton#chip:checked { background: $accent_soft; border-color: $accent_border; color: $accent; }

QToolButton { background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 4px; }
QToolButton:hover { background: $surface_hover; }
QToolButton:pressed { background: $accent_soft; }
QToolButton:disabled { color: $text_faint; }
QToolButton#iconButton { padding: 4px; }
QToolButton#cardMenu { padding: 2px; }
QToolButton#sectionToggle {
    background: transparent; border: none; padding: 0; text-align: left;
    font-size: 13px; font-weight: 700; color: $text;
}
QToolButton#sectionToggle:hover { color: $accent; background: transparent; }

/* ---------- inputs ---------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {
    background: $input_bg; border: 1px solid $border_strong; border-radius: ${radius_small}px;
    padding: 6px 8px; min-height: 18px; selection-background-color: $accent;
    selection-color: $accent_text;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {
    border-color: $accent;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: $surface_alt; color: $text_faint; border-color: $border;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 0px; height: 0px; border: none;
}
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 20px; border: none; background: transparent;
}
QComboBox::down-arrow {
    image: url($combo_arrow); width: 10px; height: 10px;
}
QComboBox::down-arrow:disabled { image: url($combo_arrow_disabled); }
QComboBox { padding-right: 21px; }
QComboBox QAbstractItemView {
    background: $surface; border: 1px solid $border_strong; outline: 0;
    selection-background-color: $accent_soft; selection-color: $text;
}
QPlainTextEdit { font-family: 'Consolas'; font-size: 12px; }
QCheckBox { spacing: 7px; color: $text; }
QCheckBox:disabled { color: $text_faint; }
QCheckBox::indicator { width: 15px; height: 15px; }

/* ---------- tables ---------- */
QTableWidget, QTableView {
    background: $surface; alternate-background-color: $surface_alt;
    border: 1px solid $border; border-radius: ${radius_small}px;
    gridline-color: $border; selection-background-color: $accent_soft; selection-color: $text;
}
QTableWidget::item, QTableView::item { padding: 4px 6px; }
QHeaderView::section {
    background: $surface_alt; color: $text_muted; border: none;
    border-bottom: 1px solid $border; padding: 7px 8px; font-weight: 600;
}
QTableCornerButton::section { background: $surface_alt; border: none; }

/* ---------- tabs, sliders, misc ---------- */
QTabWidget::pane { border: 1px solid $border; background: $surface; border-radius: ${radius_small}px; }
QTabBar::tab {
    padding: 8px 16px; background: $surface_alt; color: $text_muted;
    border: 1px solid $border; border-bottom: none;
    border-top-left-radius: 7px; border-top-right-radius: 7px;
}
QTabBar::tab:selected { background: $surface; color: $accent; font-weight: 600; }
QTabBar::tab:hover { color: $accent; }
QSlider::groove:horizontal { height: 5px; background: $border; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $accent; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 15px; margin: -5px 0; border-radius: 7px; background: $accent;
    border: 2px solid $surface;
}
QSlider::handle:horizontal:hover { background: $accent_hover; }
QMenu { background: $surface; border: 1px solid $border_strong; padding: 4px; }
QMenu::item { padding: 6px 26px 6px 14px; border-radius: 5px; }
QMenu::item:selected { background: $accent_soft; color: $accent; }
QMenu::separator { height: 1px; background: $border; margin: 4px 8px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 3px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: $scroll_handle_hover; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scroll_handle; border-radius: 3px; min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: $scroll_handle_hover; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QProgressBar {
    background: $surface_alt; border: 1px solid $border; border-radius: 4px;
    text-align: center; color: $text_muted;
}
QProgressBar::chunk { background: $accent; border-radius: 3px; }
QDialogButtonBox QPushButton { min-width: 84px; }
"""
)


def _tokens(p: Palette) -> dict[str, str]:
    values = {field: str(getattr(p, field)) for field in p.__dataclass_fields__}
    values.update(
        radius=str(p.radius),
        radius_small=str(p.radius_small),
        font_ui=p.font_ui,
        combo_arrow=_asset("chevron_down", p.text_muted, 11),
        combo_arrow_disabled=_asset("chevron_down", p.text_faint, 11),
        warning_border=blend(p.warning, p.surface, 0.55),
        success_border=blend(p.success, p.surface, 0.55),
        danger_border=blend(p.danger, p.surface, 0.55),
        accent_border=blend(p.accent, p.surface, 0.55),
        accent_disabled=blend(p.accent, p.surface, 0.5),
        accent_text_muted=blend(p.accent_text, p.accent, 0.22),
        danger_text="#FFFFFF",
        scroll_handle=blend(p.border_strong, p.surface, 0.2),
        scroll_handle_hover=blend(p.text_faint, p.surface, 0.1),
    )
    values["channels"] = ", ".join(p.channels)
    return values


def stylesheet(p: Palette) -> str:
    return _STYLESHEET.substitute(_tokens(p))


def apply(app: W.QApplication, name: str | None = None) -> Palette:
    """Apply a palette to the whole application and remember it."""
    chosen = palette(name)
    _register_fonts()
    app.setStyle("Fusion")
    app.setPalette(qpalette(chosen))
    app.setStyleSheet(stylesheet(chosen))
    app.setFont(QtGui.QFont(chosen.font_ui, 10))
    app._labdaq_palette = chosen  # type: ignore[attr-defined]
    app.setWindowIcon(QtGui.QIcon(logo_pixmap(chosen, 64)))
    return chosen


# --------------------------------------------------------------------------
# Helpers shared by the window and dialogs


def divider(parent: W.QWidget | None = None) -> W.QFrame:
    line = W.QFrame(parent)
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


def vdivider(parent: W.QWidget | None = None) -> W.QFrame:
    line = W.QFrame(parent)
    line.setObjectName("vDivider")
    line.setFixedWidth(1)
    return line


def style_plot(plot, p: Palette) -> None:
    """Apply palette colors to a ``pyqtgraph.PlotWidget``."""
    import pyqtgraph as pg

    plot.setBackground(p.plot_bg)
    for name in ("left", "bottom", "right", "top"):
        axis = plot.getAxis(name)
        if axis is None:
            continue
        axis.setPen(pg.mkPen(p.plot_grid))
        axis.setTextPen(pg.mkPen(p.plot_fg))
    plot.getPlotItem().getViewBox().setBackgroundColor(p.plot_bg)


def legend_style(p: Palette) -> dict:
    return {"brush": alpha(p.surface, 0.92), "pen": QtGui.QPen(QtGui.QColor(p.border))}


def configure_pyqtgraph(p: Palette) -> None:
    import pyqtgraph as pg

    pg.setConfigOptions(antialias=True, foreground=p.plot_fg, background=p.plot_bg)
