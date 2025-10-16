# -*- coding: utf-8 -*-
"""
ImageViewer - mouse-centered zoom + tile-based pixel-grid mode (PySide6)
+ 实时像素信息显示（坐标 + 像素值）

保持之前的修复：
- 避免白屏的强制 repaint/processEvents
- 瓦片化像素格子与 PIXEL_CELL 对齐
- 全图灰度检测 -> 灰度/彩色绘制策略
"""
from __future__ import annotations

import sys, os, math, collections
from PySide6.QtCore import Qt, QRectF, QPoint, QPointF, QTimer, QSize, Signal
from PySide6.QtGui import QPixmap, QPainter, QMouseEvent, QWheelEvent, QColor, QFont, QTransform, QCursor, QImage
from PySide6.QtWidgets import (
    QWidget, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QVBoxLayout, QApplication, QPushButton, QHBoxLayout, QFileDialog,
    QGraphicsItem, QMessageBox, QLabel
)

# ---------------- CONFIG (tile / pixel) ----------------
PIXEL_CELL = 64          # 每个原像素在设备像素上的大小（最大缩放倍数）
TILE_ORIG_PX = 16        # 每个瓦片包含多少原始像素
CACHE_MAX_TILES = 200    # 瓦片缓存上限
FONT_PIXEL = 12          # 字体像素大小（设备像素）用于瓦片内文本
LINE_GAP = 3
PADDING = 5
# ---------------- end config ----------------


class TileCache:
    """简单 LRU 瓦片缓存：键 (tx,ty) -> QPixmap"""
    def __init__(self, max_tiles: int):
        self.max_tiles = max_tiles
        self.cache = {}
        self.order = collections.deque()

    def get(self, key):
        if key in self.cache:
            try:
                self.order.remove(key)
            except ValueError:
                pass
            self.order.append(key)
            return self.cache[key]
        return None

    def put(self, key, pixmap):
        if key in self.cache:
            try:
                self.order.remove(key)
            except ValueError:
                pass
        self.cache[key] = pixmap
        self.order.append(key)
        while len(self.order) > self.max_tiles:
            old = self.order.popleft()
            if old in self.cache:
                del self.cache[old]

    def clear(self):
        self.cache.clear()
        self.order.clear()


class GraphicsImageView(QGraphicsView):
    prevRequested = Signal()
    nextRequested = Signal()
    # 新增：鼠标下像素信息变化信号 (x:int, y:int, value: tuple|int|None)
    pixelInfoChanged = Signal(int, int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))

        # original pixmap item
        self._pixmap_item = None
        self._orig_pixmap = None  # QPixmap

        # render & interaction
        self.setRenderHints(self.renderHints() | QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)

        # panning
        self._panning = False
        self._last_pan_point = None  # QPoint

        # hotspot UI
        self._hover_side = None
        self._hotspot_width = 40
        self.setMouseTracking(True)

        # auto-fit and zoom parameters
        self._auto_fit_mode = True
        self._smooth_threshold = 2.5
        self._smooth_enabled = True
        self._max_scale = 64.0  # 可调，但 pixel-mode 会 snap 到 PIXEL_CELL

        # zoom detection
        self._zooming = False
        self._zoom_end_timer = QTimer(self)
        self._zoom_end_timer.setSingleShot(True)
        self._zoom_end_timer.setInterval(120)
        self._zoom_end_timer.timeout.connect(self._on_zoom_end)

        # resize debounce
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(180)
        self._resize_debounce_timer.timeout.connect(self._on_debounced_resize)

        self._last_viewport_size = QSize(self.viewport().width(), self.viewport().height())

        # ---------------- tile / pixel-mode state ----------------
        self._tile_cache = TileCache(CACHE_MAX_TILES)
        self._tile_items = {}  # (tx,ty) -> QGraphicsPixmapItem
        self._orig_qimage = None  # QImage for tile extraction (Format_RGB888)
        self._pixel_mode = False
        self._saved_transform: QTransform | None = None
        self._saved_scale: float | None = None
        self._is_grayscale = False  # 全图是否灰度（由 load_image 采样决定）
        # debounce timer to update visible tiles while panning/scrolling
        self._visible_update_timer = QTimer(self)
        self._visible_update_timer.setSingleShot(True)
        self._visible_update_timer.setInterval(60)
        self._visible_update_timer.timeout.connect(self._update_visible_tiles)
        # ---------------- end tile state ----------------

    # ---------------- utilities: pick reference point (mouse > viewport center) ----------------
    def _get_reference_viewport_point(self, explicit_point: QPoint = None) -> QPoint:
        vw = self.viewport().width()
        vh = self.viewport().height()
        if explicit_point is not None:
            if 0 <= explicit_point.x() < vw and 0 <= explicit_point.y() < vh:
                return explicit_point
        gpos = QCursor.pos()
        local = self.viewport().mapFromGlobal(gpos)
        if 0 <= local.x() < vw and 0 <= local.y() < vh:
            return local
        return QPoint(vw // 2, vh // 2)

    # ---------------- load image (from QPixmap) ----------------
    def load_image(self, qpixmap: QPixmap):
        if qpixmap is None or qpixmap.isNull():
            QMessageBox.warning(self, "加载失败", "无法打开图像")
            return

        # clear scene and tile cache
        self.scene().clear()
        self._tile_cache.clear()
        self._tile_items.clear()
        self._orig_qimage = None
        self._pixel_mode = False
        self._is_grayscale = False

        self._orig_pixmap = qpixmap
        self._pixmap_item = QGraphicsPixmapItem(self._orig_pixmap)
        self._pixmap_item.setFlags(QGraphicsItem.GraphicsItemFlag(0))
        try:
            self._pixmap_item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        except Exception:
            pass
        self.scene().addItem(self._pixmap_item)

        # set explicit scene rect to image original pixel size (important)
        img_rect = QRectF(self._orig_pixmap.rect())
        self.scene().setSceneRect(img_rect)

        # store qimage for tile extraction
        img = self._orig_pixmap.toImage()
        if not img.isNull():
            self._orig_qimage = img.convertToFormat(QImage.Format_RGB888)
            # detect grayscale by sampling
            self._detect_grayscale()

        # reset transform and auto-fit
        self.resetTransform()
        self._zooming = False
        self._zoom_end_timer.stop()
        self._auto_fit_mode = True

        QTimer.singleShot(0, lambda: self.fit_long_side(None))
        self._last_viewport_size = QSize(self.viewport().width(), self.viewport().height())

    def _detect_grayscale(self):
        """快速采样判断整张图是否为灰度（r==g==b 对于所有采样点）"""
        if self._orig_qimage is None:
            self._is_grayscale = True
            return
        iw = self._orig_qimage.width()
        ih = self._orig_qimage.height()
        # 若图片较小，逐像素检查；较大时按网格采样最多约100x100点
        max_grid = 100
        step_x = max(1, iw // max_grid)
        step_y = max(1, ih // max_grid)
        grayscale = True
        for y in range(0, ih, step_y):
            for x in range(0, iw, step_x):
                col = QColor(self._orig_qimage.pixel(x, y))
                r, g, b = col.red(), col.green(), col.blue()
                if not (r == g == b):
                    grayscale = False
                    break
            if not grayscale:
                break
        self._is_grayscale = grayscale

    # ---------------- compute long-side scale ----------------
    def _compute_long_side_scale(self) -> float:
        if self._pixmap_item is None:
            return 1.0
        pix = self._pixmap_item.pixmap()
        iw = pix.width(); ih = pix.height()
        vw = max(1, self.viewport().width()); vh = max(1, self.viewport().height())
        if iw == 0 or ih == 0:
            return 1.0
        sx = vw / iw; sy = vh / ih
        s = min(sx, sy)
        if iw * s > vw:
            s = (vw - 1) / iw
        if ih * s > vh:
            s = min(s, (vh - 1) / ih)
        return max(0.0001, s)

    # ---------------- fit long side (preserve reference point) ----------------
    def fit_long_side(self, ref_viewport_point: QPoint = None):
        if self._pixmap_item is None:
            return
        ref_pt = self._get_reference_viewport_point(ref_viewport_point)
        old_scene_pt = self.mapToScene(ref_pt)
        target = self._compute_long_side_scale()
        t = QTransform()
        t.scale(target, target)
        self.setTransform(t)
        new_scene_pt = self.mapToScene(ref_pt)
        dx = new_scene_pt.x() - old_scene_pt.x()
        dy = new_scene_pt.y() - old_scene_pt.y()
        self.translate(dx, dy)
        self._adjust_scrollbar_policy_for_scale(target)
        self._auto_fit_mode = True

    def _adjust_scrollbar_policy_for_scale(self, scale: float):
        if self._pixmap_item is None:
            return
        pix = self._pixmap_item.pixmap()
        iw = pix.width(); ih = pix.height()
        vw = max(1, self.viewport().width()); vh = max(1, self.viewport().height())
        tw = iw * scale; th = ih * scale
        if tw <= vw and th <= vh:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def current_scale(self) -> float:
        return self.transform().m11()

    # ---------------- visible tile range (in scene original-pixel coords) ----------------
    def _visible_tile_range(self, margin_tiles=1):
        if self._orig_qimage is None:
            return (0, -1, 0, -1)
        # map viewport rect to scene robustly
        view_rect = self.viewport().rect()
        scene_polygon = self.mapToScene(view_rect)
        if scene_polygon.isEmpty():
            scene_rect = self.scene().sceneRect()
        else:
            scene_rect = scene_polygon.boundingRect()
        iw = self._orig_qimage.width(); ih = self._orig_qimage.height()
        left = max(0, int(math.floor(scene_rect.left())))
        top = max(0, int(math.floor(scene_rect.top())))
        right = min(iw - 1, int(math.ceil(scene_rect.right())))
        bottom = min(ih - 1, int(math.ceil(scene_rect.bottom())))
        tw = TILE_ORIG_PX; th = TILE_ORIG_PX
        tx0 = max(0, (left // tw) - margin_tiles)
        ty0 = max(0, (top // th) - margin_tiles)
        tx1 = min((iw - 1) // tw, (right // tw) + margin_tiles)
        ty1 = min((ih - 1) // th, (bottom // th) + margin_tiles)
        return (tx0, tx1, ty0, ty1)

    # ---------------- create tile pixmap ----------------
    def _make_tile_pixmap(self, tx, ty):
        if self._orig_qimage is None:
            return None
        iw = self._orig_qimage.width(); ih = self._orig_qimage.height()
        tw = TILE_ORIG_PX; th = TILE_ORIG_PX
        x0 = tx * tw; y0 = ty * th
        w = min(tw, iw - x0); h = min(th, ih - y0)
        if w <= 0 or h <= 0:
            return None
        sub = self._orig_qimage.copy(x0, y0, w, h)
        target_w = int(w * PIXEL_CELL); target_h = int(h * PIXEL_CELL)
        if target_w <= 0 or target_h <= 0:
            return None
        scaled = sub.scaled(target_w, target_h, Qt.IgnoreAspectRatio, Qt.FastTransformation)
        pix = QPixmap.fromImage(scaled)
        painter = QPainter(pix)
        font = QFont("Consolas")
        font.setPixelSize(FONT_PIXEL)
        painter.setFont(font)
        fm = painter.fontMetrics()
        # 使用 self._is_grayscale 决定每像素显示行数
        for yy in range(h):
            for xx in range(w):
                col = QColor(sub.pixel(xx, yy))
                r, g, b = col.red(), col.green(), col.blue()
                rx = int(xx * PIXEL_CELL)
                ry = int(yy * PIXEL_CELL)

                if self._is_grayscale:
                    # 全图灰度：只显示单行并居中
                    lines = [str(r)]
                    top_content = ry + (PIXEL_CELL - fm.height()) / 2
                else:
                    # 彩色图：每个像素始终显示 R/G/B 三行
                    lines = [str(r), str(g), str(b)]
                    top_content = ry + PADDING

                lum = 0.299 * r + 0.587 * g + 0.114 * b
                pen_color = QColor(0, 0, 0) if lum > 140 else QColor(255, 255, 255)
                painter.setPen(pen_color)
                for i, line in enumerate(lines):
                    y_center = top_content + i * (fm.height() + LINE_GAP) + fm.height() / 2
                    rect_x = rx
                    rect_y = int(y_center - fm.height() / 2)
                    painter.drawText(rect_x, rect_y, PIXEL_CELL, fm.height(), Qt.AlignHCenter | Qt.AlignVCenter, line)

        painter.setPen(QColor(100, 100, 100, 120))
        for xx in range(w + 1):
            xline = xx * PIXEL_CELL
            painter.drawLine(xline, 0, xline, target_h)
        for yy in range(h + 1):
            yline = yy * PIXEL_CELL
            painter.drawLine(0, yline, target_w, yline)
        painter.end()
        return pix

    # ---------------- update visible tiles: add missing, remove extra ----------------
    def _update_visible_tiles(self):
        if self._orig_qimage is None or not self._pixel_mode:
            return
        tx0, tx1, ty0, ty1 = self._visible_tile_range(margin_tiles=1)
        required = set()
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                required.add((tx, ty))

        # remove tile items not in required
        to_remove = [k for k in list(self._tile_items.keys()) if k not in required]
        for k in to_remove:
            item = self._tile_items.pop(k)
            try:
                self.scene().removeItem(item)
                item.deleteLater()
            except Exception:
                pass

        # create/show required tiles
        for (tx, ty) in required:
            if (tx, ty) in self._tile_items:
                continue
            pix = self._tile_cache.get((tx, ty))
            if pix is None:
                pix = self._make_tile_pixmap(tx, ty)
                if pix is None:
                    continue
                self._tile_cache.put((tx, ty), pix)
            item = QGraphicsPixmapItem(pix)
            # important: ensure offset 0,0 and scale so that pixmap's pix->scene mapping equals original pixels
            item.setOffset(0, 0)
            item.setScale(1.0 / PIXEL_CELL)
            item.setPos(tx * TILE_ORIG_PX, ty * TILE_ORIG_PX)
            try:
                item.setTransformationMode(Qt.FastTransformation)
            except Exception:
                pass
            item.setZValue(100)
            item.setVisible(True)
            self.scene().addItem(item)
            self._tile_items[(tx, ty)] = item

    # ---------------- preserve ref mapping when changing transforms ----------------
    def _preserve_ref(self, viewport_pt: QPoint, old_scene_pt: QPointF):
        try:
            new_vp = self.mapFromScene(old_scene_pt)
            dx_v = new_vp.x() - viewport_pt.x()
            dy_v = new_vp.y() - viewport_pt.y()
            center_vp = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
            desired_center_vp = QPoint(center_vp.x() + dx_v, center_vp.y() + dy_v)
            desired_center_scene = self.mapToScene(desired_center_vp)
            self.centerOn(desired_center_scene)
        except Exception:
            try:
                self.centerOn(old_scene_pt)
            except Exception:
                pass

    # ---------------- enter / exit pixel-mode (FIXED: no blank) ----------------
    def enter_pixel_mode(self, ref_viewport_point: QPoint = None):
        if self._orig_qimage is None:
            return
        if self._pixel_mode:
            return
        if ref_viewport_point is None:
            ref_viewport_point = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        old_scene_pt = self.mapToScene(ref_viewport_point)

        # save transform
        try:
            self._saved_transform = QTransform(self.transform())
            self._saved_scale = float(self.current_scale())
        except Exception:
            self._saved_transform = None
            self._saved_scale = None

        # set transform so scene units (image pixel) -> device px = PIXEL_CELL
        t = QTransform()
        t.scale(PIXEL_CELL, PIXEL_CELL)
        self.setTransform(t)
        # keep internal scale consistent (optional)
        try:
            self._scale = PIXEL_CELL
        except Exception:
            pass

        # preserve mapping
        self._preserve_ref(ref_viewport_point, old_scene_pt)

        # Temporarily force full viewport updates to ensure tiles are painted immediately
        prev_update_mode = self.viewportUpdateMode()
        try:
            self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        except Exception:
            prev_update_mode = prev_update_mode

        # mark pixel_mode True before update so _update_visible_tiles uses correct branch
        self._pixel_mode = True

        # Generate & add visible tiles while original image still visible
        self._update_visible_tiles()

        # Force immediate repaint so tiles become visible synchronously
        try:
            self.scene().update()
            self.viewport().repaint()
            QApplication.processEvents()
        except Exception:
            pass

        # hide original image only after tiles are painted to avoid white flash
        if self._pixmap_item:
            self._pixmap_item.setVisible(False)

        # final refresh
        try:
            self.scene().update()
            self.viewport().repaint()
            QApplication.processEvents()
        except Exception:
            pass

        # restore prior update mode
        try:
            self.setViewportUpdateMode(prev_update_mode)
        except Exception:
            pass

        # connect scrollbars to trigger tile updates
        try:
            self.horizontalScrollBar().valueChanged.connect(self._on_viewport_changed)
            self.verticalScrollBar().valueChanged.connect(self._on_viewport_changed)
        except Exception:
            pass

    def exit_pixel_mode(self, ref_viewport_point: QPoint = None):
        if not self._pixel_mode:
            return
        if self._orig_qimage is None:
            return
        if ref_viewport_point is None:
            ref_viewport_point = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        old_scene_pt = self.mapToScene(ref_viewport_point)

        # disconnect scroll callbacks first
        try:
            self.horizontalScrollBar().valueChanged.disconnect(self._on_viewport_changed)
            self.verticalScrollBar().valueChanged.disconnect(self._on_viewport_changed)
        except Exception:
            pass

        # stop visible update timer to avoid race
        if self._visible_update_timer.isActive():
            self._visible_update_timer.stop()

        # remove all tile items
        for k, item in list(self._tile_items.items()):
            try:
                self.scene().removeItem(item)
                item.deleteLater()
            except Exception:
                pass
        self._tile_items.clear()

        # force scene / viewport refresh to ensure tiles really gone
        try:
            self.scene().update()
            self.viewport().repaint()
            QApplication.processEvents()
        except Exception:
            pass

        # restore transform if available
        if self._saved_transform is not None:
            try:
                self.setTransform(self._saved_transform)
            except Exception:
                self.resetTransform()
                QTimer.singleShot(0, lambda: self.fit_long_side(None))
        else:
            self.resetTransform()
            QTimer.singleShot(0, lambda: self.fit_long_side(None))

        # preserve mapping: keep same pixel under cursor
        self._preserve_ref(ref_viewport_point, old_scene_pt)

        # show original image and ensure z-order
        if self._pixmap_item:
            self._pixmap_item.setVisible(True)
            self._pixmap_item.setZValue(0)

        self._pixel_mode = False

    def _on_viewport_changed(self, *args):
        if not self._visible_update_timer.isActive():
            self._visible_update_timer.start()

    # ---------------- mouse / panning / hotspot logic ----------------
    def mousePressEvent(self, event: QMouseEvent):
        if self._pixmap_item is None:
            super().mousePressEvent(event)
            return
        posf = event.position()
        x = posf.x()
        w = self.viewport().width()
        hw = self._hotspot_width
        if event.button() == Qt.LeftButton:
            if x <= hw:
                self.prevRequested.emit()
                return
            elif x >= w - hw:
                self.nextRequested.emit()
                return
            # panning
            self._panning = True
            self.setCursor(Qt.ClosedHandCursor)
            self._last_pan_point = posf.toPoint()
            # emit pixel info immediately on press
            self._emit_pixel_info_at_viewport_point(posf.toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._pixmap_item is None:
            # clear pixel info when no image
            self.pixelInfoChanged.emit(-1, -1, None)
            self._hover_side = None
            if not self._panning:
                self.viewport().setCursor(Qt.ArrowCursor)
            return super().mouseMoveEvent(event)

        posf = event.position()
        x = posf.x()
        w = self.viewport().width()
        hw = self._hotspot_width
        old = self._hover_side
        if x <= hw:
            self._hover_side = 'left'
            self.viewport().setCursor(Qt.PointingHandCursor)
        elif x >= w - hw:
            self._hover_side = 'right'
            self.viewport().setCursor(Qt.PointingHandCursor)
        else:
            self._hover_side = None
            if not self._panning:
                self.viewport().setCursor(Qt.ArrowCursor)

        if self._hover_side != old:
            self.viewport().update()

        # emit pixel info for current mouse pos
        self._emit_pixel_info_at_viewport_point(posf.toPoint())

        # panning
        if self._panning and self._last_pan_point is not None:
            curr = posf.toPoint()
            delta = curr - self._last_pan_point
            self._last_pan_point = curr
            hbar = self.horizontalScrollBar()
            vbar = self.verticalScrollBar()
            hbar.setValue(hbar.value() - delta.x())
            vbar.setValue(vbar.value() - delta.y())
            if self._pixel_mode:
                self._on_viewport_changed()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        # clear pixel info on leave
        self.pixelInfoChanged.emit(-1, -1, None)
        if self._hover_side is not None:
            self._hover_side = None
            self.viewport().update()
        super().leaveEvent(event)

    def _emit_pixel_info_at_viewport_point(self, vp_point: QPoint):
        """
        将视口坐标转换到 scene/image 像素坐标并通过信号发出像素信息。
        如果不在图像范围内发出 (-1,-1,None)。
        """
        if self._orig_qimage is None:
            self.pixelInfoChanged.emit(-1, -1, None)
            return
        try:
            scene_pt = self.mapToScene(vp_point)
        except Exception:
            self.pixelInfoChanged.emit(-1, -1, None)
            return
        x = int(math.floor(scene_pt.x()))
        y = int(math.floor(scene_pt.y()))
        iw = self._orig_qimage.width()
        ih = self._orig_qimage.height()
        if x < 0 or y < 0 or x >= iw or y >= ih:
            self.pixelInfoChanged.emit(-1, -1, None)
            return
        col = QColor(self._orig_qimage.pixel(x, y))
        r, g, b = col.red(), col.green(), col.blue()
        if self._is_grayscale:
            # emit single int for grayscale
            self.pixelInfoChanged.emit(x, y, int(r))
        else:
            self.pixelInfoChanged.emit(x, y, (int(r), int(g), int(b)))

    # ---------------- paint hotspot UI ----------------
    def paintEvent(self, event):
        super().paintEvent(event)
        if self._pixmap_item is None:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.viewport().width()
        h = self.viewport().height()
        side_w = self._hotspot_width

        if self._hover_side == 'left':
            painter.fillRect(0, 0, side_w, h, QColor(255, 255, 255, 0))
        elif self._hover_side == 'right':
            painter.fillRect(w - side_w, 0, side_w, h, QColor(255, 255, 255, 0))

        font = QFont()
        font_size = max(12, int(side_w * 0.9))
        font.setPointSize(font_size)
        font.setBold(True)
        painter.setFont(font)
        fm = painter.fontMetrics()
        text_left = '《'
        text_right = '》'

        if self._hover_side == 'left':
            tw = fm.horizontalAdvance(text_left)
            th = fm.height()
            x = (side_w - tw) / 2 - 10
            y = (h + th) / 2 - fm.descent()
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    if dx == 0 and dy == 0:
                        continue
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(int(x + dx), int(y + dy), text_left)
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(int(x), int(y), text_left)
        elif self._hover_side == 'right':
            tw = fm.horizontalAdvance(text_right)
            th = fm.height()
            x = w - side_w + (side_w - tw) / 2 + 10
            y = (h + th) / 2 - fm.descent()
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    if dx == 0 and dy == 0:
                        continue
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(int(x + dx), int(y + dy), text_right)
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(int(x), int(y), text_right)

        painter.end()

    # ---------------- double click (hotspot preserved) ----------------
    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if self._pixmap_item is None:
            return
        posf = event.position()
        x = posf.x()
        w = self.viewport().width()
        hw = self._hotspot_width
        if x <= hw:
            self.prevRequested.emit()
            event.accept()
            return
        if x >= w - hw:
            self.nextRequested.emit()
            event.accept()
            return

        ref = QPoint(int(posf.x()), int(posf.y()))
        if self._visible_update_timer.isActive():
            self._visible_update_timer.stop()
        if self._pixel_mode:
            self.exit_pixel_mode(ref)
        try:
            self.scene().update()
            self.viewport().repaint()
            QApplication.processEvents()
        except Exception:
            pass
        self.fit_long_side(ref)
        self._auto_fit_mode = True
        event.accept()

    # ---------------- wheelEvent: Ctrl/Shift scroll, mouse-centered zoom, pixel-mode switching ----------------
    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        modifiers = QApplication.keyboardModifiers()
        # Ctrl -> vertical scroll
        if modifiers & Qt.ControlModifier:
            step = int(-delta / 120 * 30)
            vbar = self.verticalScrollBar()
            vbar.setValue(vbar.value() + step)
            event.accept()
            return
        # Shift -> horizontal scroll
        if modifiers & Qt.ShiftModifier:
            step = int(-delta / 120 * 30)
            hbar = self.horizontalScrollBar()
            hbar.setValue(hbar.value() + step)
            event.accept()
            return

        if self._pixmap_item is None:
            return

        # reference point is mouse (if inside viewport) else center
        ref_pt = event.position().toPoint()
        old_scene_pt = self.mapToScene(ref_pt)

        factor = 1.0 + (0.0015 * delta)
        target_scale = self.current_scale() * factor
        MIN_SCALE = 0.05
        new_scale = max(MIN_SCALE, min(target_scale, self._max_scale))
        factor_to_apply = new_scale / self.current_scale()

        # if currently in pixel-mode and zooming out -> exit pixel mode
        if self._pixel_mode and delta < 0:
            self.exit_pixel_mode(ref_pt)
            event.accept()
            return

        # perform scaling centered at mouse
        self.scale(factor_to_apply, factor_to_apply)
        new_scene_pt = self.mapToScene(ref_pt)
        dx = new_scene_pt.x() - old_scene_pt.x()
        dy = new_scene_pt.y() - old_scene_pt.y()
        self.translate(dx, dy)

        # user interactive zoom housekeeping
        self._zooming = True
        self._zoom_end_timer.start()
        try:
            if self._pixmap_item is not None:
                self._pixmap_item.setCacheMode(QGraphicsItem.NoCache)
        except Exception:
            pass

        # smooth / fast transform handling
        if new_scale > self._smooth_threshold and self._smooth_enabled:
            hints = self.renderHints()
            hints &= ~QPainter.SmoothPixmapTransform
            self.setRenderHints(hints)
            self._smooth_enabled = False
        if new_scale <= self._smooth_threshold and not self._smooth_enabled:
            hints = self.renderHints()
            hints |= QPainter.SmoothPixmapTransform
            self.setRenderHints(hints)
            self._smooth_enabled = True

        self._auto_fit_mode = False

        # if reach threshold to enter pixel-mode: we snap transform to exact PIXEL_CELL then generate tiles
        if new_scale >= (PIXEL_CELL - 1) and delta > 0 and (self._orig_qimage is not None):
            # enter pixel mode with strict PIXEL_CELL transform
            self.enter_pixel_mode(ref_pt)

        event.accept()

    def _on_zoom_end(self):
        self._zooming = False
        try:
            if self._pixmap_item is not None:
                self._pixmap_item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        except Exception:
            pass
        if self.current_scale() <= self._smooth_threshold and not self._smooth_enabled:
            hints = self.renderHints()
            hints |= QPainter.SmoothPixmapTransform
            self.setRenderHints(hints)
            self._smooth_enabled = True
        self.viewport().update()

    # ---------------- resize handling ----------------
    def _on_debounced_resize(self):
        if self._zooming:
            return
        if self._pixmap_item is None:
            return
        if self._auto_fit_mode:
            ref = self._get_reference_viewport_point(None)
            target = self._compute_long_side_scale()
            if abs(self.current_scale() - target) > 1e-3:
                self.fit_long_side(ref)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        vw = self.viewport().width()
        vh = self.viewport().height()
        prev = getattr(self, '_last_viewport_size', None)
        if prev is None:
            self._last_viewport_size = QSize(vw, vh)
            return
        self._last_viewport_size = QSize(vw, vh)
        if self._auto_fit_mode and not self._zooming:
            ref = self._get_reference_viewport_point(None)
            self.fit_long_side(ref)
        else:
            self._resize_debounce_timer.start()


class ImageViewer(QWidget):
    prevRequested = Signal()
    nextRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = GraphicsImageView()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        toolbar = QHBoxLayout()
        btn_open_file = QPushButton('打开图片')
        btn_open_folder = QPushButton('打开文件夹')
        btn_prev = QPushButton('上一张')
        btn_next = QPushButton('下一张')

        toolbar.addWidget(btn_open_file)
        toolbar.addWidget(btn_open_folder)
        toolbar.addWidget(btn_prev)
        toolbar.addWidget(btn_next)
        toolbar.addStretch()

        layout.addLayout(toolbar)
        layout.addWidget(self.view)

        # 状态栏：显示鼠标下像素信息
        self._status_bar = QHBoxLayout()
        self._status_label = QLabel("Pos: -   Val: -")
        self._status_label.setContentsMargins(6, 4, 6, 4)
        self._status_label.setMinimumHeight(24)
        self._status_bar.addWidget(self._status_label)
        self._status_bar.addStretch()
        layout.addLayout(self._status_bar)

        self._folder_images = []
        self._current_index = -1

        # connect hotspot and buttons
        self.view.prevRequested.connect(self.show_prev)
        self.view.nextRequested.connect(self.show_next)
        btn_prev.clicked.connect(self.show_prev)
        btn_next.clicked.connect(self.show_next)

        btn_open_file.clicked.connect(self._on_open_file)
        btn_open_folder.clicked.connect(self._on_open_folder)

        self.view.prevRequested.connect(lambda: self.prevRequested.emit())
        self.view.nextRequested.connect(lambda: self.nextRequested.emit())

        # connect pixel info signal
        self.view.pixelInfoChanged.connect(self._on_pixel_info_changed)

    def _on_pixel_info_changed(self, x: int, y: int, val):
        """接收视图发过来的像素信息并更新状态栏显示"""
        if x < 0 or y < 0 or val is None:
            self._status_label.setText("位置: -   像素值: -")
            return
        if isinstance(val, tuple):
            r, g, b = val
            text = f"位置: {x}, {y}   像素值: RGB={r}, {g}, {b}"
            text = (f"位置: {x}, {y}   Val: RGB="
                    f"<span style='color:#cc0000;font-weight:bold;'>{r}, </span> "
                    f"<span style='color:#008800;font-weight:bold;'>{g}, </span> "
                    f"<span style='color:#0000cc;font-weight:bold;'>{b}</span>")
        else:
            # grayscale int
            text = f"位置: {x}, {y}   像素值: Gray={val}"
        self._status_label.setText(text)

    def _on_open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择图片', '', 'Images (*.png *.jpg *.bmp *.jpeg *.gif *.tif *.tiff *.webp)')
        if path:
            self._folder_images = []
            self._current_index = -1
            self._load_image(path)

    def _on_open_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择图片文件夹', '')
        if folder:
            self.open_folder(folder)

    def open_folder(self, folder_path: str):
        if not os.path.isdir(folder_path):
            return
        exts = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tif', '.tiff', '.webp'}
        files = [os.path.join(folder_path, f) for f in os.listdir(folder_path)]
        images = [f for f in files if os.path.splitext(f)[1].lower() in exts]
        images.sort()
        self._folder_images = images
        if images:
            self._current_index = 0
            self._load_image(images[0])
        else:
            self._current_index = -1

    def _load_image(self, path: str):
        if not os.path.exists(path):
            return
        pix = QPixmap(path)
        if pix.isNull():
            QMessageBox.warning(self, "加载失败", "无法打开图像")
            return
        self.view.load_image(pix)

    def show_next(self):
        if not self._folder_images:
            self.nextRequested.emit()
            return
        self._current_index = (self._current_index + 1) % len(self._folder_images)
        self._load_image(self._folder_images[self._current_index])

    def show_prev(self):
        if not self._folder_images:
            self.prevRequested.emit()
            return
        self._current_index = (self._current_index - 1) % len(self._folder_images)
        self._load_image(self._folder_images[self._current_index])


# ---------------- demo ----------------
if __name__ == '__main__':
    app = QApplication(sys.argv)
    win = QWidget()
    layout = QVBoxLayout(win)

    viewer = ImageViewer()
    layout.addWidget(viewer)

    viewer.prevRequested.connect(lambda: print('外部：上一张 信号触发'))
    viewer.nextRequested.connect(lambda: print('外部：下一张 信号触发'))

    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.isdir(path):
            viewer.open_folder(path)
        elif os.path.exists(path):
            viewer._load_image(path)

    win.resize(1200, 800)
    win.setWindowTitle('ImageViewer - mouse-centered zoom + tile pixel mode (with pixel info)')
    win.show()
    sys.exit(app.exec())
