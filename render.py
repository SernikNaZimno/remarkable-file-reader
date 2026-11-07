"""
Renderowanie stron reMarkable przez QPainter (ekran, PNG, PDF).

Układ współrzędnych strony = współrzędne tabletu: x = 0 na środku strony,
y = 0 u góry. Jednostka = piksel ekranu tabletu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QPainter, QPen,
                         QPolygonF)

import rmlines
from rmdoc import Document

# kolory palety (id koloru z pliku .rm)
PALETTE = {
    0: (0, 0, 0),          # czarny
    1: (144, 144, 144),    # szary
    2: (255, 255, 255),    # biały
    3: (251, 247, 25),     # żółty
    4: (0, 180, 0),        # zielony
    5: (255, 160, 190),    # różowy
    6: (40, 90, 210),      # niebieski
    7: (200, 40, 40),      # czerwony
    8: (125, 125, 125),    # szary (nakładanie)
    9: (255, 237, 117),    # zakreślacz (domyślnie żółty)
    10: (110, 200, 80),    # zielony 2
    11: (60, 190, 230),    # cyjan
    12: (200, 80, 200),    # magenta
    13: (247, 232, 81),    # żółty 2
}

# id narzędzi
BRUSH = (0, 12)
PENCIL = (1, 14)
BALLPOINT = (2, 15)
MARKER = (3, 16)
FINELINER = (4, 17)
HIGHLIGHTER = (5, 18)
ERASERS = (6, 8)
MECH_PENCIL = (7, 13)
CALLIGRAPHY = (21,)
SHADER = (23,)

TEXT_TOP_Y = -88
LINE_HEIGHTS = {0: 70, 1: 70, 2: 150, 3: 70, 4: 35, 5: 35, 6: 35, 7: 35}


@dataclass
class _Op:
    kind: str                     # "line" | "rect" | "text"
    poly: Optional[QPolygonF] = None
    pen: Optional[QPen] = None
    color: Optional[QColor] = None
    rect: Optional[QRectF] = None
    multiply: bool = False
    text: str = ""
    font: Optional[QFont] = None
    pos: Optional[QPointF] = None


@dataclass
class PagePlan:
    """Gotowa do narysowania strona (zbudowana raz, rysowana wiele razy)."""
    rect: QRectF                          # obszar strony we współrzędnych tabletu
    paper: QRectF                         # właściwa kartka (bez dorysowanego „wydłużenia”)
    ops: list = field(default_factory=list)
    template: str = "Blank"
    pdf_index: Optional[int] = None
    pdf_rect: Optional[QRectF] = None     # gdzie narysować stronę PDF
    warnings: list = field(default_factory=list)


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _line_color(line: rmlines.Line):
    if line.rgba is not None:
        r, g, b, a = line.rgba
        return QColor(r, g, b, a)
    r, g, b = PALETTE.get(line.color, (0, 0, 0))
    return QColor(r, g, b)


def _mix_white(c: QColor, alpha: float) -> QColor:
    """Kolor „rozjaśniony” zamiast przezroczystego – bez ciemnych kropek na łączeniach."""
    a = _clamp(alpha)
    return QColor(int(255 - (255 - c.red()) * a),
                  int(255 - (255 - c.green()) * a),
                  int(255 - (255 - c.blue()) * a))


def _make_pen(color: QColor, width: float, cap=Qt.PenCapStyle.RoundCap) -> QPen:
    p = QPen(color)
    p.setWidthF(max(width, 0.5))
    p.setCapStyle(cap)
    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return p


def _stroke_ops(line: rmlines.Line, dx: float, dy: float, out: list):
    pts = line.points
    if not pts or line.tool in ERASERS:
        return
    tool = line.tool
    base = _line_color(line)

    def poly(seq):
        return QPolygonF([QPointF(p[0] + dx, p[1] + dy) for p in seq])

    if len(pts) == 1:
        pts = [pts[0], (pts[0][0] + 0.01,) + tuple(pts[0][1:])]

    # --- narzędzia półprzezroczyste: jedna ścieżka, mieszanie „mnożenie”
    if tool in HIGHLIGHTER or tool in SHADER:
        w = max(p[3] for p in pts) / 4 or 30
        c = QColor(base)
        if tool in HIGHLIGHTER:
            c.setAlpha(110)
            cap = Qt.PenCapStyle.FlatCap
        else:
            if line.rgba is None:
                c.setAlpha(64)
            cap = Qt.PenCapStyle.RoundCap
        out.append(_Op("line", poly(pts), _make_pen(c, w, cap), multiply=True))
        return

    # --- stała grubość
    if tool in FINELINER or tool in MECH_PENCIL:
        w = pts[0][3] / 4
        c = base if tool in FINELINER else _mix_white(base, 0.8)
        out.append(_Op("line", poly(pts), _make_pen(c, w)))
        return

    # --- grubość / nasycenie zależne od nacisku: dzielimy kreskę na odcinki
    def style(p):
        w = p[3] / 4
        pr = p[5] / 255
        if tool in PENCIL:
            return round(w * 0.9 * 2) / 2, round(_clamp(0.3 + 0.75 * pr) * 10) / 10
        if tool in BRUSH:
            return round(w * 2) / 2, round(_clamp(0.35 + 0.8 * pr) * 10) / 10
        if tool in BALLPOINT:
            return round(w * 2) / 2, round(_clamp(0.7 + 0.4 * pr) * 10) / 10
        # marker, kaligrafia, inne
        return round(w * 2) / 2, 1.0

    run = [pts[0]]
    cur = style(pts[0])
    for p in pts[1:]:
        s = style(p)
        run.append(p)
        if s != cur:
            out.append(_Op("line", poly(run), _make_pen(_mix_white(base, cur[1]), cur[0])))
            run = [p]
            cur = s
    if len(run) > 1:
        out.append(_Op("line", poly(run), _make_pen(_mix_white(base, cur[1]), cur[0])))


def _text_ops(text: rmlines.Text, out: list, width_hint: float):
    """Tekst z klawiatury. Zwraca słownik pozycji kotwic (id znaku -> y)."""
    anchors = dict(rmlines.SPECIAL_ANCHORS)
    y = text.pos_y + TEXT_TOP_Y
    box_w = text.width if text.width > 0 else width_hint
    for para in text.paragraphs:
        style = para.style
        lh = LINE_HEIGHTS.get(style, 70)
        size = {2: 60}.get(style, 34)
        font = QFont("Segoe UI")
        font.setPixelSize(size)
        if style in (2, 3):
            font.setBold(True)
        prefix = {4: "•  ", 5: "    ◦  ", 6: "☐  ", 7: "☑  "}.get(style, "")
        fm = QFontMetricsF(font)

        anchors[para.start_id] = y
        # zawijanie słów do szerokości pola tekstowego; token = (znak, id znaku)
        tokens = [(c, None) for c in prefix] + list(zip(para.text, para.char_ids))
        lines, cur = [], []
        for tok in tokens:
            cur.append(tok)
            if len(cur) > 1 and fm.horizontalAdvance("".join(t[0] for t in cur)) > box_w:
                sp = max((i for i, t in enumerate(cur[:-1]) if t[0] == " "), default=-1)
                if sp >= 0:
                    lines.append(cur[:sp + 1])
                    cur = cur[sp + 1:]
                else:
                    lines.append(cur[:-1])
                    cur = [cur[-1]]
        lines.append(cur)

        line_start_y = y
        for k, toks in enumerate(lines):
            txt = "".join(t[0] for t in toks)
            for _, cid in toks:
                if cid is not None:
                    anchors[cid] = line_start_y
            if txt.strip():
                f = QFont(font)
                out.append(_Op("text", text=txt.rstrip(), font=f,
                               pos=QPointF(text.pos_x, line_start_y + lh),
                               color=QColor(0, 0, 0)))
            if k < len(lines) - 1:
                line_start_y += lh
        y = line_start_y + lh
    return anchors


def build_plan(doc: Document, index: int) -> PagePlan:
    W, H = doc.page_size
    ref = doc.pages[index]
    paper = QRectF(-W / 2, 0, W, H)
    plan = PagePlan(rect=QRectF(paper), paper=paper, template=ref.template,
                    pdf_index=ref.pdf_index)
    try:
        scene = doc.scene(index)
    except Exception as e:  # uszkodzona strona – pokaż pustą z ostrzeżeniem
        plan.warnings.append(str(e))
        scene = None

    if scene is not None:
        plan.warnings += scene.warnings
        anchors = dict(rmlines.SPECIAL_ANCHORS)
        if scene.text is not None:
            anchors = _text_ops(scene.text, plan.ops, W * 0.8)

        def rec(g: rmlines.Group, dx, dy):
            if not g.visible:
                return
            for ch in g.children():
                if isinstance(ch, rmlines.Group):
                    ax, ay = 0.0, 0.0
                    if ch.anchor_id is not None:
                        ax = ch.anchor_origin_x or 0.0
                        ay = anchors.get(ch.anchor_id, 0.0)
                    rec(ch, dx + ax, dy + ay)
                elif isinstance(ch, rmlines.Line):
                    _stroke_ops(ch, dx, dy, plan.ops)
                elif isinstance(ch, rmlines.Highlight):
                    c = QColor(*ch.rgba) if ch.rgba else QColor(*PALETTE.get(ch.color, PALETTE[9]))
                    c.setAlpha(110)
                    for x, y, w, h in ch.rects:
                        plan.ops.append(_Op("rect", rect=QRectF(x + dx, y + dy, w, h),
                                            color=c, multiply=True))
        rec(scene.root, 0.0, 0.0)

    # rozszerz stronę, jeśli notatki wychodzą poza kartkę (przewijane notatniki)
    r = QRectF(paper)
    for op in plan.ops:
        if op.kind == "line":
            br = op.poly.boundingRect()
            m = op.pen.widthF() / 2 + 20
            r = r.united(br.adjusted(-m, -m, m, m))
        elif op.kind == "rect":
            r = r.united(op.rect)
    # wydłużamy tylko w dół i na boki symetrycznie (strona dalej „wygląda” jak kartka)
    left = min(r.left(), paper.left())
    right = max(r.right(), paper.right())
    half = max(-left, right)
    top = min(r.top(), 0)
    bottom = max(r.bottom(), H)
    if bottom > H:
        # zaokrąglij w górę do pełnych „ekranów” jak na tablecie (co 1/4 strony)
        step = H / 4
        bottom = H + step * int((bottom - H + step - 1) // step)
    plan.rect = QRectF(-half, top, 2 * half, bottom - top)
    return plan


def pdf_target_rect(plan: PagePlan, pdf_w: float, pdf_h: float) -> QRectF:
    """Gdzie na kartce leży strona PDF (dopasowana do kartki, wyśrodkowana w poziomie)."""
    p = plan.paper
    s = min(p.width() / pdf_w, p.height() / pdf_h)
    w, h = pdf_w * s, pdf_h * s
    return QRectF(-w / 2, 0, w, h)


# ----------------------------------------------------------------------
# Szablony (przybliżone – oryginalne pliki szablonów są na tablecie)
# ----------------------------------------------------------------------

def _draw_template(p: QPainter, plan: PagePlan):
    name = (plan.template or "").lower()
    if not name or name == "blank" or plan.pdf_index is not None:
        return
    W = plan.paper.width()
    k = W / 1404
    if "small" in name or " s" in name[-2:]:
        step = 46 * k
    elif "large" in name or " l" in name[-2:]:
        step = 92 * k
    else:
        step = 66 * k
    r = plan.rect
    p.save()
    c = QColor(205, 205, 205)
    pen = QPen(c)
    pen.setWidthF(1.2)
    p.setPen(pen)
    top = 140 * k
    if "grid" in name or "square" in name or "kratk" in name:
        y = top
        while y < r.bottom():
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            y += step
        x = 0.0
        while x < r.right():
            p.drawLine(QPointF(x, top), QPointF(x, r.bottom()))
            p.drawLine(QPointF(-x, top), QPointF(-x, r.bottom()))
            x += step
    elif "dot" in name:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(170, 170, 170))
        y = top
        while y < r.bottom():
            x = 0.0
            while x < r.right():
                p.drawEllipse(QPointF(x, y), 2.2, 2.2)
                if x:
                    p.drawEllipse(QPointF(-x, y), 2.2, 2.2)
                x += step
            y += step
    elif "line" in name or "lined" in name or "checklist" in name or "todo" in name:
        y = top
        while y < r.bottom():
            p.drawLine(QPointF(r.left() + 60 * k, y), QPointF(r.right() - 60 * k, y))
            y += step
    p.restore()


# ----------------------------------------------------------------------
# Rysowanie
# ----------------------------------------------------------------------

def paint_page(p: QPainter, plan: PagePlan, pdf_image: Optional[QImage] = None,
               allow_multiply=True, background=True):
    """Rysuje stronę; painter ma już ustawioną transformację na współrzędne tabletu."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if background:
        p.fillRect(plan.rect, QColor(255, 255, 255))
    if pdf_image is not None and plan.pdf_rect is not None:
        p.drawImage(plan.pdf_rect, pdf_image)
    _draw_template(p, plan)

    normal = QPainter.CompositionMode.CompositionMode_SourceOver
    multiply = QPainter.CompositionMode.CompositionMode_Multiply
    p.setBrush(Qt.BrushStyle.NoBrush)
    for op in plan.ops:
        if op.kind == "line":
            if op.multiply and allow_multiply:
                p.setCompositionMode(multiply)
            p.setPen(op.pen)
            p.drawPolyline(op.poly)
            if op.multiply and allow_multiply:
                p.setCompositionMode(normal)
        elif op.kind == "rect":
            if allow_multiply:
                p.setCompositionMode(multiply)
            p.fillRect(op.rect, op.color)
            p.setCompositionMode(normal)
        elif op.kind == "text":
            p.setFont(op.font)
            p.setPen(op.color)
            p.drawText(op.pos, op.text)


def render_image(plan: PagePlan, scale: float, pdf_image: Optional[QImage] = None) -> QImage:
    w = max(1, int(round(plan.rect.width() * scale)))
    h = max(1, int(round(plan.rect.height() * scale)))
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    p.scale(scale, scale)
    p.translate(-plan.rect.left(), -plan.rect.top())
    paint_page(p, plan, pdf_image)
    p.end()
    return img
