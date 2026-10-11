"""Schematic preview with mouse-wheel zoom (towards the pointer), drag to pan (left or middle button) and
double-click to fit. The SVG is drawn as vectors at every zoom level, so it stays sharp.

Reloading (after every design change) keeps the zoom and the visible spot, unless the view is in 'fit' mode -
then it fits the new drawing. Zooming or panning leaves fit mode; Fit / double-click returns to it.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

MIN_ZOOM, MAX_ZOOM = 0.02, 60.0
STEP = 1.25


class SvgView(QGraphicsView):
    zoomed = Signal(float)

    def __init__(self, parent=None, background: str = "#1b1d21"):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor(background))
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setDragMode(QGraphicsView.NoDrag)
        self.viewport().setCursor(Qt.OpenHandCursor)
        self._item = None
        self._renderer = None
        self._fit = True
        self._pan_from = None

    # ------------------------------------------------------------------ content
    def load(self, data):
        data = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        keep = not self._fit and self._item is not None
        if keep:
            centre = self.mapToScene(self.viewport().rect().center())
            t = self.transform()
        sc = self.scene()
        if self._item is not None:
            sc.removeItem(self._item)
        self._item = QGraphicsSvgItem()
        self._renderer = QSvgRenderer(QByteArray(data), self._item)
        self._item.setSharedRenderer(self._renderer)
        self._item.setCacheMode(QGraphicsItem.CacheMode.NoCache)   # vector redraw: sharp at any zoom
        sc.addItem(self._item)
        r = self._item.boundingRect()
        m = max(r.width(), r.height())                         # room to pan past the edges
        sc.setSceneRect(r.adjusted(-m, -m, m, m))
        if keep:
            self.setTransform(t)
            self.centerOn(centre)
        else:
            self.fit()

    def fit(self):
        self._fit = True
        if self._item is not None and not self._item.boundingRect().isEmpty():
            self.fitInView(self._item.boundingRect(), Qt.KeepAspectRatio)
        self.zoomed.emit(self.zoom())

    def zoom(self) -> float:
        return self.transform().m11()

    def zoom_by(self, factor: float, anchor: QPointF | None = None):
        z = self.zoom()
        factor = max(MIN_ZOOM / z, min(MAX_ZOOM / z, factor)) if z else factor
        if anchor is None:
            self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.scale(factor, factor)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._fit = False
        self.zoomed.emit(self.zoom())

    # ------------------------------------------------------------------ mouse
    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.pixelDelta().y()
        if dy:
            self.zoom_by(1.0015 ** dy, e.position())
        e.accept()

    def mousePressEvent(self, e):
        if e.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._pan_from = e.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan_from is not None:
            d = e.position() - self._pan_from
            self._pan_from = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            if d.manhattanLength() > 0:
                self._fit = False
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan_from is not None and e.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._pan_from = None
            self.viewport().setCursor(Qt.OpenHandCursor)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.fit()
        e.accept()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_by(STEP)
        elif k == Qt.Key_Minus:
            self.zoom_by(1 / STEP)
        elif k in (Qt.Key_0, Qt.Key_Home, Qt.Key_F):
            self.fit()
        else:
            super().keyPressEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fit:
            self.fit()


class SvgPreview(QWidget):
    """SvgView with a small bar: Fit, -, +, zoom %, and how to use the mouse."""

    def __init__(self, parent=None):
        super().__init__(parent)
        l = QVBoxLayout(self); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(2)
        bar = QHBoxLayout(); bar.setContentsMargins(4, 2, 4, 0)
        self.view = SvgView(self)
        for label, tip, fn in (("Fit", "Fit the whole schematic (double-click, F or 0)", self.view.fit),
                               ("−", "Zoom out (wheel down, -)", lambda: self.view.zoom_by(1 / STEP)),
                               ("+", "Zoom in (wheel up, +)", lambda: self.view.zoom_by(STEP))):
            b = QPushButton(label); b.setToolTip(tip); b.clicked.connect(fn); bar.addWidget(b)
        self.pct = QLabel("")
        bar.addWidget(self.pct)
        hint = QLabel("wheel: zoom · drag: pan · double-click: fit")
        hint.setStyleSheet("color:#9aa4b2;")
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # clipped, never holds the panel wide
        hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar.addWidget(hint, 1)
        l.addLayout(bar)
        l.addWidget(self.view, 1)
        self.view.zoomed.connect(lambda z: self.pct.setText(f"{z * 100:.0f}%"))

    def load(self, data: bytes):
        self.view.load(data)
