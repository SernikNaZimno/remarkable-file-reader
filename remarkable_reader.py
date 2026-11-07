#!/usr/bin/env python3
"""
Czytnik plików reMarkable na komputerze.

Otwiera:
  * pliki .rmdoc (kopie z interfejsu USB, np. ze skryptu remarkable_backup.py),
  * surowe foldery xochitl skopiowane z tabletu (pliki <uuid>.metadata itd.),
  * pojedyncze strony .rm (format v6, oprogramowanie 3.x, także Paper Pro).

Notatniki, PDF-y i EPUB-y z notatkami; eksport do PDF i PNG.

Wymagania: Python 3.9+ i PyQt6 (pip install PyQt6)

Użycie:
  python remarkable_reader.py                     # okno z biblioteką
  python remarkable_reader.py plik.rmdoc          # od razu otwórz plik
  python remarkable_reader.py --export-pdf WEJŚCIE.rmdoc WYJŚCIE.pdf
  python remarkable_reader.py --export-png WEJŚCIE.rmdoc FOLDER [--scale 1.0]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import (QBuffer, QByteArray, QIODevice, QPointF, QRectF,
                          QSettings, QSize, QSizeF, Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import (QAction, QColor, QIcon, QImage, QKeySequence,
                         QPageSize, QPainter, QPdfWriter, QPixmap)
from PyQt6.QtPdf import QPdfDocument

import render
from rmdoc import Document, LibNode, scan_library

APP_NAME = "Czytnik reMarkable"
HERE = Path(__file__).resolve().parent
PAGE_GAP = 40           # odstęp między stronami (jednostki tabletu)
MAX_CACHE_PIXELS = 36_000_000   # większy podgląd rysujemy wektorowo
CACHE_PAGES = 14


# ======================================================================
# Wspólne: dokument + PDF + plany stron
# ======================================================================

class LoadedDoc:
    """Dokument z wczytanym PDF-em (jeśli jest) i planami stron."""

    def __init__(self, path):
        self.path = Path(path)
        self.doc = Document.open(path)
        self.pdf = None
        self._pdf_buf = None
        if self.doc.pdf_bytes:
            self._pdf_buf = QBuffer()
            self._pdf_buf.setData(QByteArray(self.doc.pdf_bytes))
            self._pdf_buf.open(QIODevice.OpenModeFlag.ReadOnly)
            self.pdf = QPdfDocument(None)
            self.pdf.load(self._pdf_buf)
            if self.pdf.status() != QPdfDocument.Status.Ready:
                self.pdf = None
            else:
                self.doc.ensure_pdf_pages(self.pdf.pageCount())
        self._plans = {}

    def __len__(self):
        return len(self.doc.pages)

    def dpi(self):
        return 229.0 if self.doc.page_size[0] >= 1600 else 226.0

    def has_plan(self, i):
        return i in self._plans

    def estimate_rect(self, i) -> QRectF:
        """Szybkie przybliżenie rozmiaru strony (zanim zostanie wczytana)."""
        W, H = self.doc.page_size
        r = QRectF(-W / 2, 0, W, H)
        ref = self.doc.pages[i]
        if ref.pdf_index is not None and self.pdf and ref.pdf_index < self.pdf.pageCount():
            sz = self.pdf.pagePointSize(ref.pdf_index)
            if sz.width() > 0 and sz.height() > 0:
                s = min(W / sz.width(), H / sz.height())
                r = r.united(QRectF(-sz.width() * s / 2, 0, sz.width() * s, sz.height() * s))
        return r

    def plan(self, i) -> render.PagePlan:
        pl = self._plans.get(i)
        if pl is None:
            pl = render.build_plan(self.doc, i)
            if pl.pdf_index is not None and self.pdf and pl.pdf_index < self.pdf.pageCount():
                sz = self.pdf.pagePointSize(pl.pdf_index)
                if sz.width() > 0 and sz.height() > 0:
                    pl.pdf_rect = render.pdf_target_rect(pl, sz.width(), sz.height())
                    pl.rect = pl.rect.united(pl.pdf_rect)
            self._plans[i] = pl
        return pl

    def pdf_image(self, plan: render.PagePlan, scale: float, max_side=6000):
        if plan.pdf_rect is None or self.pdf is None:
            return None
        w = plan.pdf_rect.width() * scale
        h = plan.pdf_rect.height() * scale
        k = min(1.0, max_side / max(w, h))
        img = self.pdf.render(plan.pdf_index, QSize(max(1, int(w * k)), max(1, int(h * k))))
        return None if img.isNull() else img


def page_dpi(plan: render.PagePlan):
    return 229.0 if plan.paper.width() >= 1600 else 226.0


def export_pdf(ld: LoadedDoc, out_path, progress=None):
    writer = QPdfWriter(str(out_path))
    writer.setTitle(ld.doc.name)
    writer.setCreator(APP_NAME)
    res = 200
    writer.setResolution(res)
    painter = None
    for i in range(len(ld)):
        plan = ld.plan(i)
        dpi = page_dpi(plan)
        size_pt = QSizeF(plan.rect.width() / dpi * 72, plan.rect.height() / dpi * 72)
        writer.setPageSize(QPageSize(size_pt, QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch))
        writer.setPageMargins(render_margins_zero())
        if painter is None:
            painter = QPainter(writer)
        else:
            writer.newPage()
        scale = res / dpi
        painter.save()
        painter.scale(scale, scale)
        painter.translate(-plan.rect.left(), -plan.rect.top())
        img = ld.pdf_image(plan, scale * 1.5, max_side=4000)
        render.paint_page(painter, plan, img, allow_multiply=False)
        painter.restore()
        if progress and not progress(i + 1, len(ld)):
            break
    if painter is not None:
        painter.end()


def render_margins_zero():
    from PyQt6.QtCore import QMarginsF
    return QMarginsF(0, 0, 0, 0)


def page_image(ld: LoadedDoc, i, scale=1.0) -> QImage:
    plan = ld.plan(i)
    return render.render_image(plan, scale, ld.pdf_image(plan, scale))


# ======================================================================
# Tryb wiersza poleceń
# ======================================================================

def cli_export(args):
    from PyQt6.QtGui import QGuiApplication
    # na Linuksie bez ekranu użyj trybu „offscreen”; na Windows/macOS zostaw domyślny
    # (offscreen na Windows nie widzi czcionek systemowych)
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") \
            and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv[:1])  # noqa: F841
    if args.export_pdf:
        src, out = args.export_pdf
        t = time.time()
        ld = LoadedDoc(src)
        export_pdf(ld, out)
        print(f"Zapisano {out} ({len(ld)} stron, {time.time() - t:.1f} s)")
    if args.export_png:
        src, out = args.export_png
        ld = LoadedDoc(src)
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        pages = range(len(ld))
        if args.pages:
            pages = [int(x) - 1 for x in args.pages.split(",")]
        for i in pages:
            t = time.time()
            img = page_image(ld, i, args.scale)
            fn = out / f"{ld.doc.name}_{i + 1:03d}.png"
            img.save(str(fn))
            print(f"{fn}  {img.width()}x{img.height()}  {time.time() - t:.2f} s")


# ======================================================================
# Interfejs graficzny
# ======================================================================

def run_gui(initial=None, screenshot=None, actions=()):
    from PyQt6.QtWidgets import (QApplication, QFileDialog, QGraphicsItem,
                                 QGraphicsScene, QGraphicsView, QHBoxLayout,
                                 QLabel, QLineEdit, QMainWindow, QMessageBox,
                                 QProgressDialog, QPushButton, QSpinBox,
                                 QSplitter, QStyle, QToolBar, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout, QWidget)

    # ------------------------------------------------------------------
    class PageItem(QGraphicsItem):
        def __init__(self, view, ld: LoadedDoc, index: int):
            super().__init__()
            self.view = view
            self.ld = ld
            self.index = index
            self.plan = ld.plan(index) if ld.has_plan(index) else None
            self.rect = self.plan.rect if self.plan else ld.estimate_rect(index)
            self.image = None
            self.image_scale = 0.0
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

        def boundingRect(self):
            return self.rect

        def set_plan(self, plan):
            changed = plan.rect != self.rect
            if changed:
                self.prepareGeometryChange()
                self.rect = QRectF(plan.rect)
            self.plan = plan
            self.update()
            return changed

        def paint(self, painter: QPainter, option, widget=None):
            r = self.rect
            lod = option.levelOfDetailFromTransform(painter.worldTransform())
            dpr = widget.devicePixelRatioF() if widget else 1.0
            want = lod * dpr
            # cień pod kartką
            painter.fillRect(r.translated(6 / lod, 6 / lod), QColor(0, 0, 0, 40))
            if self.plan is None:
                # strona jeszcze się wczytuje
                painter.fillRect(r, QColor(255, 255, 255))
                painter.setPen(QColor(150, 150, 150))
                f = painter.font()
                f.setPixelSize(40)
                painter.setFont(f)
                painter.drawText(r, Qt.AlignmentFlag.AlignCenter, "Wczytywanie…")
                self.view.request_plan(self.index)
                return
            pixels = r.width() * r.height() * want * want
            if pixels > MAX_CACHE_PIXELS:
                # bardzo duże powiększenie: rysuj wektorowo tylko widoczny fragment
                painter.save()
                painter.setClipRect(option.exposedRect)
                img = self.ld.pdf_image(self.plan, want, max_side=5000)
                render.paint_page(painter, self.plan, img)
                painter.restore()
                return
            if self.image is None or abs(self.image_scale - want) / want > 0.02:
                if self.image is not None and not self.view.rendering_allowed():
                    # w trakcie zoomu: pokaż stary podgląd przeskalowany, odśwież za chwilę
                    painter.drawImage(r, self.image)
                    self.view.schedule_refresh()
                    return
                self.image = page_image(self.ld, self.index, want)
                self.image_scale = want
                self.view.touch_cache(self)
            painter.drawImage(r, self.image)

        def drop_cache(self):
            self.image = None
            self.image_scale = 0.0

    # ------------------------------------------------------------------
    class Viewer(QGraphicsView):
        pageChanged = pyqtSignal(int)
        zoomChanged = pyqtSignal(float)
        loadedAll = pyqtSignal()

        def __init__(self):
            super().__init__()
            self.setScene(QGraphicsScene(self))
            self.setBackgroundBrush(QColor(110, 112, 118))
            self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.items_ = []
            self.ld = None
            self.fit_mode = "width"
            self._cache = OrderedDict()
            self._last_zoom = 0.0
            self._refresh = QTimer(self, singleShot=True, interval=160)
            self._refresh.timeout.connect(self.viewport().update)
            self.verticalScrollBar().valueChanged.connect(self._emit_page)
            self._current = -1
            # wczytywanie stron w tle (po kawałku, żeby okno nie „zamarzało”)
            self._queue = []
            self._priority = []
            self._loader = QTimer(self, interval=0)
            self._loader.timeout.connect(self._load_some)

        # --- pamięć podręczna obrazów stron
        def touch_cache(self, item):
            self._cache[item.index] = item
            self._cache.move_to_end(item.index)
            while len(self._cache) > CACHE_PAGES:
                _, old = self._cache.popitem(last=False)
                old.drop_cache()

        def rendering_allowed(self):
            return time.monotonic() - self._last_zoom > 0.15

        def schedule_refresh(self):
            self._refresh.start()

        # --- dokument
        def set_document(self, ld: LoadedDoc, start_page=0):
            self._loader.stop()
            sc = self.scene()
            sc.clear()
            self._cache.clear()
            self.items_ = []
            self.ld = ld
            start_page = max(0, min(start_page, len(ld) - 1))
            # najpierw wczytaj stronę startową i sąsiednie – reszta w tle
            for i in range(start_page, min(len(ld), start_page + 2)):
                ld.plan(i)
            for i in range(len(ld)):
                it = PageItem(self, ld, i)
                sc.addItem(it)
                self.items_.append(it)
            self._layout()
            self._current = -1
            self.apply_fit()
            self.go_to(start_page)
            n = len(ld)
            self._priority = []
            self._queue = [i for i in list(range(start_page, n)) + list(range(start_page - 1, -1, -1))
                           if self.items_[i].plan is None]
            if self._queue:
                self._loader.start()

        def _layout(self):
            y = 0.0
            maxw = 0.0
            for it in self.items_:
                r = it.rect
                it.setPos(0, y - r.top())
                y += r.height() + PAGE_GAP
                maxw = max(maxw, r.width())
            self.scene().setSceneRect(QRectF(-maxw / 2 - PAGE_GAP, -PAGE_GAP,
                                             maxw + 2 * PAGE_GAP, y + PAGE_GAP))

        def request_plan(self, i):
            if i not in self._priority:
                self._priority.append(i)
            if not self._loader.isActive():
                self._loader.start()

        def _load_some(self):
            if self.ld is None:
                self._loader.stop()
                return
            t0 = time.monotonic()
            changed = False
            while time.monotonic() - t0 < 0.03:
                if self._priority:
                    i = self._priority.pop(0)
                elif self._queue:
                    i = self._queue.pop(0)
                else:
                    break
                it = self.items_[i]
                if it.plan is None:
                    changed |= it.set_plan(self.ld.plan(i))
            if changed:
                # zachowaj położenie widoku względem bieżącej strony
                cur = self.current_page()
                before = self.items_[cur].sceneBoundingRect().top()
                self._layout()
                after = self.items_[cur].sceneBoundingRect().top()
                if after != before:
                    sb = self.verticalScrollBar()
                    sb.setValue(sb.value() + round((after - before) * self.zoom()))
            if not self._priority and not self._queue:
                self._loader.stop()
                self.loadedAll.emit()

        def clear(self):
            self._loader.stop()
            self.scene().clear()
            self.items_ = []
            self.ld = None
            self._cache.clear()

        # --- nawigacja
        def page_count(self):
            return len(self.items_)

        def current_page(self):
            if not self.items_:
                return 0
            c = self.mapToScene(self.viewport().rect().center()).y()
            top = self.mapToScene(0, 0).y()
            # strona, która zajmuje górną część ekranu (jak w czytnikach PDF)
            probe = top + (c - top) * 0.35
            for it in self.items_:
                r = it.sceneBoundingRect()
                if probe < r.bottom() + PAGE_GAP / 2:
                    return it.index
            return len(self.items_) - 1

        def _emit_page(self, *_):
            p = self.current_page()
            if p != self._current:
                self._current = p
                self.pageChanged.emit(p)

        def go_to(self, i):
            if not self.items_:
                return
            i = max(0, min(i, len(self.items_) - 1))
            r = self.items_[i].sceneBoundingRect()
            vh = self.viewport().height() / self.transform().m11()
            self.centerOn(QPointF(0, r.top() - PAGE_GAP / 2 + vh / 2))
            self._emit_page()

        # --- zoom
        def zoom(self):
            return self.transform().m11()

        def set_zoom(self, z, anchor_mouse=False):
            z = max(0.03, min(z, 8.0))
            old = self.transformationAnchor()
            if not anchor_mouse:
                self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
            k = z / self.zoom()
            self.scale(k, k)
            self.setTransformationAnchor(old)
            self._last_zoom = time.monotonic()
            self.schedule_refresh()
            self.zoomChanged.emit(z)

        def zoom_by(self, f, anchor_mouse=False):
            self.fit_mode = None
            self.set_zoom(self.zoom() * f, anchor_mouse)

        def apply_fit(self):
            if not self.items_ or not self.fit_mode:
                return
            page = self.items_[max(0, self.current_page())]
            r = page.rect
            paper_h = self.ld.doc.page_size[1] if self.ld else r.height()
            vw = self.viewport().width() - 24
            vh = self.viewport().height() - 24
            if self.verticalScrollBar().isVisible():
                pass  # szerokość widoku już bez paska
            else:
                vw -= self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent)
            sw = self.scene().sceneRect().width()
            if self.fit_mode == "width":
                z = vw / sw
            else:
                z = min(vw / sw, vh / (min(r.height(), paper_h) + PAGE_GAP))
            cur = self.current_page()
            self.set_zoom(z)
            self.go_to(cur)

        def fit(self, mode):
            self.fit_mode = mode
            self.apply_fit()

        # --- zdarzenia
        def wheelEvent(self, e):
            if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
                d = e.angleDelta().y()
                if d:
                    self.zoom_by(1.15 if d > 0 else 1 / 1.15, anchor_mouse=True)
                e.accept()
                return
            super().wheelEvent(e)

        def resizeEvent(self, e):
            super().resizeEvent(e)
            if self.fit_mode:
                QTimer.singleShot(0, self.apply_fit)

        def keyPressEvent(self, e):
            k = e.key()
            if k in (Qt.Key.Key_PageDown, Qt.Key.Key_Right, Qt.Key.Key_Space) and not (
                    e.modifiers() & Qt.KeyboardModifier.ShiftModifier and k == Qt.Key.Key_Space):
                self.go_to(self.current_page() + 1)
            elif k in (Qt.Key.Key_PageUp, Qt.Key.Key_Left, Qt.Key.Key_Space):
                self.go_to(self.current_page() - 1)
            elif k == Qt.Key.Key_Home:
                self.go_to(0)
            elif k == Qt.Key.Key_End:
                self.go_to(self.page_count() - 1)
            else:
                super().keyPressEvent(e)

    # ------------------------------------------------------------------
    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.settings = QSettings("remarkable_reader", "CzytnikReMarkable")
            self.ld = None
            self.root_dir = None
            self.setAcceptDrops(True)

            # --- panel biblioteki
            left = QWidget()
            lv = QVBoxLayout(left)
            lv.setContentsMargins(6, 6, 0, 6)
            row = QHBoxLayout()
            self.folder_btn = QPushButton("Wybierz folder…")
            self.folder_btn.clicked.connect(self.choose_folder)
            self.refresh_btn = QPushButton()
            self.refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            self.refresh_btn.setToolTip("Odśwież bibliotekę")
            self.refresh_btn.clicked.connect(lambda: self.load_library(self.root_dir))
            row.addWidget(self.folder_btn, 1)
            row.addWidget(self.refresh_btn)
            lv.addLayout(row)
            self.filter = QLineEdit()
            self.filter.setPlaceholderText("Szukaj dokumentu…")
            self.filter.setClearButtonEnabled(True)
            self.filter.textChanged.connect(self.apply_filter)
            lv.addWidget(self.filter)
            self.tree = QTreeWidget()
            self.tree.setHeaderHidden(True)
            self.tree.itemActivated.connect(self.tree_open)
            self.tree.itemClicked.connect(self.tree_open)
            lv.addWidget(self.tree, 1)
            self.lib_label = QLabel()
            self.lib_label.setWordWrap(True)
            self.lib_label.setStyleSheet("color: gray; font-size: 11px")
            lv.addWidget(self.lib_label)

            # --- podgląd
            self.viewer = Viewer()
            self.viewer.pageChanged.connect(self.on_page_changed)
            self.viewer.zoomChanged.connect(self.on_zoom_changed)
            self.viewer.loadedAll.connect(self.on_loaded_all)
            self.base_msg = ""

            self.split = QSplitter()
            self.split.addWidget(left)
            self.split.addWidget(self.viewer)
            self.split.setStretchFactor(1, 1)
            self.split.setSizes([280, 900])
            self.setCentralWidget(self.split)

            # --- pasek narzędzi
            st = self.style()
            tb = QToolBar("Narzędzia")
            tb.setMovable(False)
            tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.addToolBar(tb)

            def act(text, icon, slot, shortcut=None, tip=None):
                a = QAction(st.standardIcon(icon) if icon is not None else QIcon(), text, self)
                a.triggered.connect(slot)
                if shortcut:
                    a.setShortcuts(shortcut if isinstance(shortcut, list) else [shortcut])
                a.setToolTip(tip or text)
                tb.addAction(a)
                return a

            act("Otwórz", QStyle.StandardPixmap.SP_DialogOpenButton, self.open_file_dialog,
                QKeySequence.StandardKey.Open, "Otwórz plik .rmdoc / .rm (Ctrl+O)")
            tb.addSeparator()
            self.a_prev = act("", QStyle.StandardPixmap.SP_ArrowUp,
                              lambda: self.viewer.go_to(self.viewer.current_page() - 1),
                              None, "Poprzednia strona (PgUp)")
            self.page_spin = QSpinBox()
            self.page_spin.setMinimum(1)
            self.page_spin.setKeyboardTracking(False)
            self.page_spin.valueChanged.connect(lambda v: self.viewer.go_to(v - 1))
            tb.addWidget(self.page_spin)
            self.page_total = QLabel(" / 0 ")
            tb.addWidget(self.page_total)
            self.a_next = act("", QStyle.StandardPixmap.SP_ArrowDown,
                              lambda: self.viewer.go_to(self.viewer.current_page() + 1),
                              None, "Następna strona (PgDn)")
            tb.addSeparator()
            act("−", None, lambda: self.viewer.zoom_by(1 / 1.2), QKeySequence.StandardKey.ZoomOut,
                "Pomniejsz (Ctrl+-, Ctrl+kółko)")
            self.zoom_label = QLabel(" 100% ")
            self.zoom_label.setMinimumWidth(48)
            self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tb.addWidget(self.zoom_label)
            act("+", None, lambda: self.viewer.zoom_by(1.2), [QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")],
                "Powiększ (Ctrl++, Ctrl+kółko)")
            act("Szerokość", None, lambda: self.viewer.fit("width"), QKeySequence("Ctrl+1"),
                "Dopasuj do szerokości (Ctrl+1)")
            act("Cała strona", None, lambda: self.viewer.fit("page"), QKeySequence("Ctrl+2"),
                "Pokaż całą stronę (Ctrl+2)")
            tb.addSeparator()
            act("Eksport PDF", QStyle.StandardPixmap.SP_DialogSaveButton, self.export_pdf_dialog,
                QKeySequence("Ctrl+E"), "Zapisz cały dokument jako PDF (Ctrl+E)")
            act("Strona PNG", None, self.export_png_dialog, QKeySequence("Ctrl+Shift+E"),
                "Zapisz bieżącą stronę jako obraz PNG (Ctrl+Shift+E)")
            full = QAction("Pełny ekran", self)
            full.setShortcut(QKeySequence("F11"))
            full.triggered.connect(self.toggle_full)
            self.addAction(full)
            lib = QAction("Biblioteka", self)
            lib.setShortcut(QKeySequence("Ctrl+B"))
            lib.triggered.connect(lambda: left.setVisible(not left.isVisible()))
            self.addAction(lib)

            self.status = self.statusBar()
            self.update_nav()

            # --- przywróć stan
            geo = self.settings.value("geometry")
            if geo is not None:
                self.restoreGeometry(geo)
            else:
                self.resize(1280, 900)
            sp = self.settings.value("splitter")
            if sp is not None:
                self.split.restoreState(sp)

            folder = self.settings.value("folder")
            if not folder or not Path(folder).is_dir():
                for cand in (HERE.parent / "remarkable_backup", HERE / "remarkable_backup",
                             Path.cwd() / "remarkable_backup"):
                    if cand.is_dir():
                        folder = str(cand)
                        break
            if folder and Path(folder).is_dir():
                self.load_library(folder)
            else:
                self.lib_label.setText("Wybierz folder z plikami .rmdoc (np. kopię z remarkable_backup.py) "
                                       "albo przeciągnij plik do okna.")

        # --- biblioteka
        def choose_folder(self):
            d = QFileDialog.getExistingDirectory(self, "Folder z dokumentami reMarkable",
                                                 self.root_dir or str(Path.home()))
            if d:
                self.load_library(d)

        def load_library(self, folder):
            if not folder:
                return
            self.root_dir = str(folder)
            self.settings.setValue("folder", self.root_dir)
            self.tree.clear()
            root = scan_library(Path(folder))
            count = [0]
            folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
            file_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

            def add(parent_item, node: LibNode):
                for ch in node.children:
                    it = QTreeWidgetItem([ch.name])
                    if ch.is_folder:
                        it.setIcon(0, folder_icon)
                        add(it, ch)
                    else:
                        it.setIcon(0, file_icon)
                        it.setData(0, Qt.ItemDataRole.UserRole, str(ch.path))
                        it.setToolTip(0, str(ch.path))
                        count[0] += 1
                    if parent_item is None:
                        self.tree.addTopLevelItem(it)
                    else:
                        parent_item.addChild(it)
            add(None, root)
            self.tree.expandToDepth(0)
            self.folder_btn.setText(Path(folder).name or str(folder))
            self.folder_btn.setToolTip(str(folder))
            self.lib_label.setText(f"{count[0]} dokumentów w {folder}")
            self.apply_filter(self.filter.text())

        def apply_filter(self, text):
            text = text.strip().lower()

            def rec(it):
                any_vis = False
                for i in range(it.childCount()):
                    c = it.child(i)
                    vis = rec(c) if c.childCount() else (not text or text in c.text(0).lower())
                    if c.childCount() and text and text in c.text(0).lower():
                        vis = True
                        for j in range(c.childCount()):
                            _show_all(c.child(j))
                    c.setHidden(not vis)
                    any_vis |= vis
                if text and any_vis:
                    it.setExpanded(True)
                return any_vis

            def _show_all(it):
                it.setHidden(False)
                for j in range(it.childCount()):
                    _show_all(it.child(j))
            rec(self.tree.invisibleRootItem())

        def tree_open(self, item, _col=0):
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if path and (self.ld is None or str(self.ld.path) != path):
                self.open_path(path)

        # --- otwieranie
        def open_file_dialog(self):
            fn, _ = QFileDialog.getOpenFileName(
                self, "Otwórz dokument reMarkable", self.root_dir or str(Path.home()),
                "Dokumenty reMarkable (*.rmdoc *.rm *.metadata *.zip);;Wszystkie pliki (*)")
            if fn:
                self.open_path(fn)

        def open_path(self, path):
            path = Path(path)
            if path.is_dir() and not (path.parent / f"{path.name}.metadata").exists():
                self.load_library(path)
                return
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                t = time.time()
                ld = LoadedDoc(path)
                start = ld.doc.last_opened_page if 0 <= ld.doc.last_opened_page < len(ld) else 0
                self.ld = ld
                self.viewer.set_document(ld, start)
                kind = {"notebook": "notatnik", "pdf": "PDF", "epub": "EPUB"}.get(ld.doc.file_type, ld.doc.file_type)
                msg = f"{ld.doc.name} — {kind}, {len(ld)} stron"
                if ld.doc.file_type == "epub" and ld.pdf is None:
                    msg += " (brak PDF-a w pliku – widać tylko notatki)"
                self.base_msg = msg
                self.status.showMessage(msg)
                self.setWindowTitle(f"{ld.doc.name} — {APP_NAME}")
                if not len(ld):
                    self.status.showMessage(f"{ld.doc.name}: dokument nie ma stron do wyświetlenia")
            except Exception as e:  # noqa: BLE001
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(self, APP_NAME, f"Nie udało się otworzyć pliku:\n{path}\n\n{e}")
                return
            QApplication.restoreOverrideCursor()
            self.update_nav()
            self.select_in_tree(str(path))
            self.viewer.setFocus()

        def select_in_tree(self, path):
            it = self.tree.invisibleRootItem()
            stack = [it]
            while stack:
                cur = stack.pop()
                for i in range(cur.childCount()):
                    c = cur.child(i)
                    if c.data(0, Qt.ItemDataRole.UserRole) == path:
                        self.tree.setCurrentItem(c)
                        self.tree.scrollToItem(c)
                        return
                    stack.append(c)

        # --- nawigacja / zoom
        def update_nav(self):
            n = self.viewer.page_count()
            self.page_spin.blockSignals(True)
            self.page_spin.setMaximum(max(1, n))
            self.page_spin.setValue(self.viewer.current_page() + 1 if n else 1)
            self.page_spin.blockSignals(False)
            self.page_total.setText(f" / {n} ")

        def on_page_changed(self, p):
            self.page_spin.blockSignals(True)
            self.page_spin.setValue(p + 1)
            self.page_spin.blockSignals(False)

        def on_zoom_changed(self, z):
            if self.ld and len(self.ld):
                # procent względem rzeczywistego rozmiaru kartki (przybliżenie 96 dpi ekranu)
                self.zoom_label.setText(f" {round(z * self.ld.dpi() / 96 * 100)}% ")

        def on_loaded_all(self):
            if not self.ld:
                return
            warn = sum(len(self.ld.plan(i).warnings) for i in range(len(self.ld)))
            if warn:
                self.status.showMessage(f"{self.base_msg} — ostrzeżenia: {warn} "
                                        "(część danych mogła zostać pominięta)")

        def toggle_full(self):
            self.showNormal() if self.isFullScreen() else self.showFullScreen()

        # --- eksport
        def export_pdf_dialog(self):
            if not self.ld:
                return
            default = str(Path(self.root_dir or Path.home()) / f"{self.ld.doc.name}.pdf")
            fn, _ = QFileDialog.getSaveFileName(self, "Zapisz jako PDF", default, "PDF (*.pdf)")
            if not fn:
                return
            dlg = QProgressDialog("Eksport do PDF…", "Anuluj", 0, len(self.ld), self)
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
            dlg.setMinimumDuration(300)

            def prog(i, n):
                dlg.setValue(i)
                QApplication.processEvents()
                return not dlg.wasCanceled()
            try:
                export_pdf(self.ld, fn, prog)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Błąd eksportu:\n{e}")
                return
            dlg.close()
            self.status.showMessage(f"Zapisano {fn}", 8000)

        def export_png_dialog(self):
            if not self.ld:
                return
            p = self.viewer.current_page()
            default = str(Path(self.root_dir or Path.home()) / f"{self.ld.doc.name} - strona {p + 1}.png")
            fn, _ = QFileDialog.getSaveFileName(self, "Zapisz stronę jako PNG", default, "Obraz PNG (*.png)")
            if not fn:
                return
            img = page_image(self.ld, p, 1.5)
            img.save(fn)
            self.status.showMessage(f"Zapisano {fn}", 8000)

        # --- przeciągnij i upuść
        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            urls = e.mimeData().urls()
            if urls:
                self.open_path(urls[0].toLocalFile())

        def closeEvent(self, e):
            self.settings.setValue("geometry", self.saveGeometry())
            self.settings.setValue("splitter", self.split.saveState())
            super().closeEvent(e)

    # ------------------------------------------------------------------
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    w.show()
    if initial:
        QTimer.singleShot(0, lambda: w.open_path(initial))
    if screenshot:
        # tryb diagnostyczny: wykonaj akcje, zapisz zrzut okna i zakończ
        def shoot():
            for a in actions:
                if a.startswith("page="):
                    w.viewer.go_to(int(a[5:]) - 1)
                elif a.startswith("zoom="):
                    w.viewer.fit_mode = None
                    w.viewer.set_zoom(float(a[5:]))
                elif a == "fitpage":
                    w.viewer.fit("page")
                elif a.startswith("filter="):
                    w.filter.setText(a[7:])
                QApplication.processEvents()
            QTimer.singleShot(1500, lambda: (w.grab().save(screenshot), app.quit()))
        QTimer.singleShot(1500, shoot)
    return app.exec()


def main():
    ap = argparse.ArgumentParser(description="Czytnik plików reMarkable (.rmdoc, .rm).")
    ap.add_argument("file", nargs="?", help="plik .rmdoc / .rm albo folder do otwarcia")
    ap.add_argument("--export-pdf", nargs=2, metavar=("WEJŚCIE", "WYJŚCIE.pdf"),
                    help="zapisz dokument jako PDF bez otwierania okna")
    ap.add_argument("--export-png", nargs=2, metavar=("WEJŚCIE", "FOLDER"),
                    help="zapisz strony jako obrazy PNG bez otwierania okna")
    ap.add_argument("--scale", type=float, default=1.0, help="skala obrazów PNG (domyślnie 1.0)")
    ap.add_argument("--pages", help="numery stron do eksportu PNG, np. 1,3,5")
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    ap.add_argument("--do", action="append", default=[], help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.export_pdf or args.export_png:
        cli_export(args)
        return 0
    return run_gui(args.file, args.screenshot, args.do)


if __name__ == "__main__":
    sys.exit(main())
