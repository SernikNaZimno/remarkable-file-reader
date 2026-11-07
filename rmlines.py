"""
Parser plików reMarkable .rm (format "lines" w wersji 6, oprogramowanie 3.x).

Bez zewnętrznych zależności. Zwraca drzewo warstw (Group) z kreskami (Line),
zaznaczeniami tekstu PDF (Highlight) i tekstem pisanym z klawiatury (Text).

Opis formatu na podstawie publicznych prac społeczności (ddvk, rmscene).
"""

from __future__ import annotations

import heapq
import struct
from dataclasses import dataclass, field
from typing import Optional

HEADER_V6 = b"reMarkable .lines file, version=6          "
HEADER_PREFIX = b"reMarkable .lines file, version="

# typy tagów
T_ID, T_LEN, T_B8, T_B4, T_B1 = 0xF, 0xC, 0x8, 0x4, 0x1

END = (0, 0)              # znacznik początku/końca sekwencji CRDT
ROOT_ID = (0, 1)          # korzeń drzewa sceny


class FormatError(Exception):
    pass


# --------------------------------------------------------------------------
# Struktury danych
# --------------------------------------------------------------------------

@dataclass
class Line:
    tool: int
    color: int
    thickness: float
    points: list            # lista krotek (x, y, speed, width, direction, pressure)
    rgba: Optional[tuple] = None   # (r, g, b, a) – kolory spoza palety / zakreślacz


@dataclass
class Highlight:
    """Zakreślenie tekstu w PDF/EPUB (narzędzie zakreślacza „przyklejone” do tekstu)."""
    color: int
    text: str
    rects: list             # lista (x, y, w, h)
    rgba: Optional[tuple] = None


@dataclass
class Paragraph:
    text: str
    style: int
    start_id: tuple
    char_ids: list
    bold: list              # flaga pogrubienia dla każdego znaku
    italic: list


@dataclass
class Text:
    paragraphs: list
    pos_x: float
    pos_y: float
    width: float


@dataclass
class Group:
    node_id: tuple
    label: str = ""
    visible: bool = True
    anchor_id: Optional[tuple] = None
    anchor_type: Optional[int] = None
    anchor_threshold: Optional[float] = None
    anchor_origin_x: Optional[float] = None
    # podczas wczytywania: item_id -> (left_id, right_id, wartość)
    _items: dict = field(default_factory=dict)
    # po wczytaniu: dzieci w kolejności rysowania (posortowane wg CRDT)
    kids: list = field(default_factory=list)

    def children(self):
        return self.kids


@dataclass
class Scene:
    root: Group
    text: Optional[Text] = None
    paper_size: Optional[tuple] = None
    warnings: list = field(default_factory=list)

    def walk_lines(self):
        """Wszystkie kreski (z przesunięciem warstwy) – do liczenia obrysu."""
        out = []

        def rec(g, dx, dy):
            for ch in g.children():
                if isinstance(ch, Group):
                    ax, ay = anchor_offset(ch, self.anchor_pos)
                    rec(ch, dx + ax, dy + ay)
                elif isinstance(ch, Line):
                    out.append((ch, dx, dy))
        rec(self.root, 0.0, 0.0)
        return out

    @property
    def anchor_pos(self):
        if not hasattr(self, "_anchor_cache"):
            self._anchor_cache = build_anchor_pos(self.text)
        return self._anchor_cache


# --------------------------------------------------------------------------
# Sortowanie CRDT
# --------------------------------------------------------------------------

def toposort(items: dict):
    """items: id -> (left_id, right_id, ...). Zwraca listę id w kolejności."""
    if not items:
        return []
    START, STOP = "S", "E"

    def side(v, is_left):
        if v == END or v not in items:
            return START if is_left else STOP
        return v

    indeg = {START: 0, STOP: 0}
    deps = {}
    for iid, it in items.items():
        l, r = side(it[0], True), side(it[1], False)
        indeg[iid] = indeg.get(iid, 0) + 1
        deps.setdefault(l, []).append(iid)
        indeg[r] = indeg.get(r, 0) + 1
        deps.setdefault(iid, []).append(r)
        indeg.setdefault(l, 0)

    def key(n):
        if n == START:
            return (0, 0, 0)
        if n == STOP:
            return (2, 0, 0)
        return (1, -n[0], n[1])

    ready = [(key(n), n) for n, d in indeg.items() if d == 0]
    heapq.heapify(ready)
    out = []
    while ready:
        _, n = heapq.heappop(ready)
        if n in items:
            out.append(n)
        if n == STOP:
            break
        for d in deps.get(n, ()):
            indeg[d] -= 1
            if indeg[d] == 0:
                heapq.heappush(ready, (key(d), d))
    if len(out) < len(items):
        # cykl / uszkodzone dane – dołóż brakujące na końcu, nic nie gubimy
        seen = set(out)
        out += [i for i in items if i not in seen]
    return out


# --------------------------------------------------------------------------
# Czytnik strumienia
# --------------------------------------------------------------------------

class Reader:
    __slots__ = ("d", "p", "end")

    def __init__(self, data: bytes, pos=0, end=None):
        self.d = data
        self.p = pos
        self.end = len(data) if end is None else end

    def remaining(self):
        return self.end - self.p

    def bytes(self, n):
        if self.p + n > len(self.d):
            raise FormatError("koniec danych")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def f64(self):
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v

    def varuint(self):
        shift = res = 0
        while True:
            b = self.d[self.p]
            self.p += 1
            res |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return res

    def crdt_id(self):
        a = self.u8()
        return (a, self.varuint())

    # --- tagi ---
    def peek_tag(self):
        if self.p >= self.end:
            return None
        save = self.p
        try:
            x = self.varuint()
        except IndexError:
            self.p = save
            return None
        self.p = save
        return x >> 4, x & 0xF

    def has(self, index, ttype):
        return self.peek_tag() == (index, ttype)

    def tag(self, index, ttype):
        t = self.peek_tag()
        if t != (index, ttype):
            raise FormatError(f"oczekiwano tagu {index}/{ttype:#x}, jest {t} @ {self.p}")
        self.varuint()

    # --- wartości z tagiem ---
    def t_id(self, i):
        self.tag(i, T_ID)
        return self.crdt_id()

    def t_bool(self, i):
        self.tag(i, T_B1)
        return self.u8() != 0

    def t_byte(self, i):
        self.tag(i, T_B1)
        return self.u8()

    def t_int(self, i):
        self.tag(i, T_B4)
        return self.u32()

    def t_float(self, i):
        self.tag(i, T_B4)
        return self.f32()

    def t_double(self, i):
        self.tag(i, T_B8)
        return self.f64()

    def sub(self, i):
        """Otwiera podblok; zwraca pozycję końca."""
        self.tag(i, T_LEN)
        n = self.u32()
        return self.p + n

    def string_body(self, end):
        n = self.varuint()
        self.u8()  # flaga „is_ascii”
        s = self.bytes(n).decode("utf-8", "replace")
        fmt = None
        if self.p < end and self.has(2, T_B4):
            fmt = self.t_int(2)
        self.p = end
        return s, fmt

    def t_string(self, i):
        end = self.sub(i)
        return self.string_body(end)[0]

    def lww(self, i, reader):
        end = self.sub(i)
        ts = self.t_id(1)
        v = reader(2)
        self.p = end
        return ts, v


def _rgba(packed):
    # zapisane jako BGRA w uint32 little-endian
    return ((packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF, (packed >> 24) & 0xFF)


# --------------------------------------------------------------------------
# Bloki
# --------------------------------------------------------------------------

def _read_line(r: Reader, version: int, end: int) -> Line:
    tool = r.t_int(1)
    color = r.t_int(2)
    thickness = r.t_double(3)
    r.t_float(4)  # starting_length
    pend = r.sub(5)
    n = pend - r.p
    if version == 1:
        size = 24
        cnt = n // size
        pts = []
        for x, y, sp, di, wi, pr in struct.iter_unpack("<ffffff", r.d[r.p:r.p + cnt * size]):
            # konwersja do jednostek wersji 2
            pts.append((x, y, sp * 4, wi * 4, di * 255 / 6.283185307, pr * 255))
    else:
        size = 14
        cnt = n // size
        pts = [(x, y, sp, wi, di, pr) for x, y, sp, wi, di, pr in
               struct.iter_unpack("<ffHHBB", r.d[r.p:r.p + cnt * size])]
    r.p = pend
    rgba = None
    # pola opcjonalne: 6 = timestamp, 7 = move_id, 8 = kolor ARGB
    while r.p < end:
        t = r.peek_tag()
        if t is None:
            break
        if t == (6, T_ID) or t == (7, T_ID):
            r.t_id(t[0])
        elif t == (8, T_B4):
            rgba = _rgba(r.t_int(8))
        else:
            break
    return Line(tool, color, thickness, pts, rgba)


def _read_glyph(r: Reader, end: int) -> Highlight:
    if r.has(2, T_B4):
        r.t_int(2)
    if r.has(3, T_B4):
        r.t_int(3)
    color = r.t_int(4)
    text = r.t_string(5)
    send = r.sub(6)
    k = r.varuint()
    rects = [struct.unpack_from("<dddd", r.d, r.p + 32 * j) for j in range(k)]
    r.p = send
    rgba = None
    if r.p < end and r.has(10, T_B4):
        rgba = _rgba(r.t_int(10))
    return Highlight(color, text, rects, rgba)


def _read_text(r: Reader) -> Text:
    r.t_id(1)
    e2 = r.sub(2)
    # elementy tekstu
    e21 = r.sub(1)
    e211 = r.sub(1)
    n = r.varuint()
    items = {}
    for _ in range(n):
        ie = r.sub(0)
        iid = r.t_id(2)
        left = r.t_id(3)
        right = r.t_id(4)
        deleted = r.t_int(5)
        value = ""
        if r.p < ie and r.has(6, T_LEN):
            se = r.sub(6)
            s, fmt = r.string_body(se)
            value = fmt if fmt is not None else s
        r.p = ie
        items[iid] = (left, right, value, deleted)
    r.p = e211
    r.p = e21
    # formatowanie akapitów
    styles = {}
    e22 = r.sub(2)
    e221 = r.sub(1)
    n = r.varuint()
    for _ in range(n):
        cid = r.crdt_id()
        ts = r.t_id(1)
        fe = r.sub(2)
        r.u8()  # 17
        code = r.u8()
        r.p = fe
        styles[cid] = code
    r.p = e221
    r.p = e22
    r.p = e2
    e3 = r.sub(3)
    pos_x = r.f64()
    pos_y = r.f64()
    r.p = e3
    width = r.t_float(4)

    # rozwinięcie na pojedyncze znaki (id kolejnych znaków są niejawne)
    chars = {}
    for iid, (left, right, value, deleted) in items.items():
        if deleted > 0:
            seq = [None] * deleted
        elif isinstance(value, int):
            chars[iid] = (left, right, value)
            continue
        else:
            seq = list(value)
        if not seq:
            continue
        cur, l = iid, left
        for c in seq[:-1]:
            nxt = (cur[0], cur[1] + 1)
            chars[cur] = (l, nxt, c)
            l, cur = cur, nxt
        chars[cur] = (l, right, seq[-1])

    order = toposort(chars)
    paragraphs = []
    bold = italic = False
    cur_text, cur_ids, cur_b, cur_i = [], [], [], []
    start = END
    for cid in order:
        v = chars[cid][2]
        if v is None:
            continue  # usunięty znak
        if isinstance(v, int):
            if v == 1:
                bold = True
            elif v == 2:
                bold = False
            elif v == 3:
                italic = True
            elif v == 4:
                italic = False
            continue
        if v == "\n":
            paragraphs.append(Paragraph("".join(cur_text), styles.get(start, 1), start,
                                        cur_ids, cur_b, cur_i))
            cur_text, cur_ids, cur_b, cur_i = [], [], [], []
            start = cid
            continue
        cur_text.append(v)
        cur_ids.append(cid)
        cur_b.append(bold)
        cur_i.append(italic)
    paragraphs.append(Paragraph("".join(cur_text), styles.get(start, 1), start,
                                cur_ids, cur_b, cur_i))
    return Text(paragraphs, pos_x, pos_y, width)


def _read_item_header(r: Reader):
    parent = r.t_id(1)
    iid = r.t_id(2)
    left = r.t_id(3)
    right = r.t_id(4)
    r.t_int(5)  # deleted_length
    return parent, iid, left, right


def read_scene(data: bytes) -> Scene:
    if not data.startswith(HEADER_PREFIX):
        raise FormatError("To nie jest plik .rm reMarkable")
    if not data.startswith(HEADER_V6):
        ver = data[len(HEADER_PREFIX):len(HEADER_PREFIX) + 2].strip()
        raise FormatError(f"Nieobsługiwana wersja pliku .rm: {ver.decode(errors='replace')} "
                          "(obsługiwana jest wersja 6, oprogramowanie 3.0+)")

    nodes = {ROOT_ID: Group(ROOT_ID)}
    pending_items = []        # (parent, iid, left, right, value) – dodajemy po wczytaniu węzłów
    scene = Scene(nodes[ROOT_ID])
    r = Reader(data, len(HEADER_V6))
    n = len(data)

    while r.p + 8 <= n:
        length = r.u32()
        r.u8()
        r.u8()                 # min_version
        cur_ver = r.u8()
        btype = r.u8()
        start = r.p
        end = start + length
        if end > n:
            scene.warnings.append("obcięty blok na końcu pliku")
            break
        r.end = end
        try:
            if btype == 0x01:      # SceneTreeBlock
                tree_id = r.t_id(1)
                r.t_id(2)
                r.t_bool(3)
                se = r.sub(4)
                parent = r.t_id(1)
                r.p = se
                nodes.setdefault(tree_id, Group(tree_id))
            elif btype == 0x02:    # TreeNodeBlock
                nid = r.t_id(1)
                g = nodes.setdefault(nid, Group(nid))
                g.label = r.lww(2, r.t_string)[1]
                g.visible = r.lww(3, r.t_bool)[1]
                if r.p < end and r.has(7, T_LEN):
                    g.anchor_id = r.lww(7, r.t_id)[1]
                    g.anchor_type = r.lww(8, r.t_byte)[1]
                    g.anchor_threshold = r.lww(9, r.t_float)[1]
                    g.anchor_origin_x = r.lww(10, r.t_float)[1]
            elif btype in (0x03, 0x04, 0x05):  # glyph / group / line
                parent, iid, left, right = _read_item_header(r)
                if r.p < end and r.has(6, T_LEN):
                    ve = r.sub(6)
                    r.u8()  # typ elementu
                    if btype == 0x05:
                        value = _read_line(r, cur_ver, ve)
                    elif btype == 0x04:
                        value = ("node", r.t_id(2))
                    else:
                        value = _read_glyph(r, ve)
                    r.p = ve
                    pending_items.append((parent, iid, left, right, value))
                else:
                    # element usunięty – zostawiamy go jako „pusty”, żeby sortowanie się zgadzało
                    pending_items.append((parent, iid, left, right, None))
            elif btype == 0x07:    # RootTextBlock
                scene.text = _read_text(r)
            elif btype == 0x0D:    # SceneInfo
                r.lww(1, r.t_id)
                if r.p < end and r.has(2, T_LEN):
                    r.lww(2, r.t_bool)
                if r.p < end and r.has(3, T_LEN):
                    r.lww(3, r.t_bool)
                if r.p < end and r.has(5, T_LEN):
                    se = r.sub(5)
                    scene.paper_size = (r.u32(), r.u32())
                    r.p = se
            # pozostałe bloki (autorzy, migracja, info o stronie, nagrobki) – pomijamy
        except (FormatError, IndexError, struct.error) as e:
            scene.warnings.append(f"blok {btype:#x} @ {start}: {e}")
        r.p = end
        r.end = n

    for parent, iid, left, right, value in pending_items:
        g = nodes.get(parent)
        if g is None:
            g = nodes.setdefault(parent, Group(parent))
        if isinstance(value, tuple) and value and value[0] == "node":
            child = nodes.get(value[1])
            if child is None:
                child = nodes.setdefault(value[1], Group(value[1]))
            value = child
        g._items[iid] = (left, right, value)

    # posortuj raz; usunięte elementy (None) odrzucamy dopiero po sortowaniu,
    # bo nadal wyznaczają kolejność sąsiadów
    for g in nodes.values():
        its = g._items
        g.kids = [its[i][2] for i in toposort(its) if its[i][2] is not None]
    return scene


# --------------------------------------------------------------------------
# Tekst: pozycje kotwic (warstwy przyklejone do linii tekstu)
# --------------------------------------------------------------------------

TEXT_TOP_Y = -88
LINE_HEIGHTS = {0: 70, 1: 70, 2: 150, 3: 70, 4: 35, 5: 35, 6: 35, 7: 35}
SPECIAL_ANCHORS = {(0, 281474976710654): 100.0, (0, 281474976710655): 100.0}


def build_anchor_pos(text: Optional[Text]) -> dict:
    pos = dict(SPECIAL_ANCHORS)
    if text is None:
        return pos
    y = text.pos_y + TEXT_TOP_Y
    for p in text.paragraphs:
        pos[p.start_id] = y
        for cid in p.char_ids:
            pos[cid] = y
        y += LINE_HEIGHTS.get(p.style, 70)
    return pos


def anchor_offset(g: Group, anchor_pos: dict):
    if g.anchor_id is None:
        return 0.0, 0.0
    ax = g.anchor_origin_x or 0.0
    ay = anchor_pos.get(g.anchor_id, 0.0)
    return ax, ay
