"""
Wczytywanie dokumentów reMarkable:
  * .rmdoc  – archiwum ZIP z interfejsu USB / aplikacji reMarkable (FW 3.9+)
  * folder xochitl – surowa kopia z tabletu (<uuid>.metadata, <uuid>.content, <uuid>/*.rm)
  * pojedynczy plik .rm
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import rmlines

DEFAULT_SIZE = (1404, 1872)   # reMarkable 1/2; Paper Pro ma 1620 x 2160 (odczytywane z plików)


@dataclass
class PageRef:
    page_id: str
    rm_name: Optional[str]       # nazwa pliku .rm w źródle (None = strona bez notatek)
    template: str = "Blank"
    pdf_index: Optional[int] = None   # numer strony w PDF-ie (None = wstawiona pusta strona)


class _ZipSource:
    def __init__(self, path: Path):
        self.path = path
        self.zf = zipfile.ZipFile(path)
        self.names = set(self.zf.namelist())
        uid = None
        for n in self.names:
            if n.endswith(".content") and "/" not in n:
                uid = n[:-len(".content")]
                break
        if uid is None:
            for n in self.names:
                if n.endswith(".metadata") and "/" not in n:
                    uid = n[:-len(".metadata")]
                    break
        if uid is None:
            raise ValueError("To archiwum nie wygląda na dokument reMarkable (.rmdoc)")
        self.uid = uid

    def read(self, name) -> Optional[bytes]:
        return self.zf.read(name) if name in self.names else None

    def list_dir(self, prefix):
        return sorted(n for n in self.names if n.startswith(prefix))


class _DirSource:
    def __init__(self, root: Path, uid: str):
        self.root = root
        self.uid = uid
        self.path = root / f"{uid}.metadata"

    def read(self, name) -> Optional[bytes]:
        p = self.root / name
        return p.read_bytes() if p.is_file() else None

    def list_dir(self, prefix):
        d = self.root / prefix.rstrip("/")
        if not d.is_dir():
            return []
        return sorted(f"{prefix}{p.name}" for p in d.iterdir())


class Document:
    def __init__(self, source, display_name: Optional[str] = None):
        self.src = source
        uid = source.uid
        self.metadata = _json(source.read(f"{uid}.metadata"))
        self.content = _json(source.read(f"{uid}.content"))
        self.name = display_name or self.metadata.get("visibleName") or uid
        self.file_type = self.content.get("fileType") or "notebook"
        self.pdf_bytes = source.read(f"{uid}.pdf")
        self._scenes = {}
        self.pages = self._build_pages()
        self.last_opened_page = int(self.metadata.get("lastOpenedPage") or 0)
        self.page_size = self._guess_size()

    # ------------------------------------------------------------------
    @classmethod
    def open(cls, path) -> "Document":
        path = Path(path)
        if path.suffix.lower() == ".rm":
            return SingleRmDocument(path)
        if path.suffix.lower() in (".metadata", ".content"):
            return cls(_DirSource(path.parent, path.stem))
        if path.is_dir():
            # folder <uuid> obok <uuid>.metadata
            if (path.parent / f"{path.name}.metadata").exists():
                return cls(_DirSource(path.parent, path.name))
            raise ValueError("To nie jest folder dokumentu reMarkable")
        if zipfile.is_zipfile(path):
            return cls(_ZipSource(path), None)
        raise ValueError(f"Nieznany format pliku: {path.name}")

    # ------------------------------------------------------------------
    def _build_pages(self):
        uid = self.src.uid
        c = self.content
        rm_files = {n.rsplit("/", 1)[-1][:-3]: n
                    for n in self.src.list_dir(f"{uid}/") if n.endswith(".rm")}
        pages = []

        cpages = (c.get("cPages") or {}).get("pages")
        if cpages:
            live = [p for p in cpages if not (p.get("deleted") or {}).get("value")]
            live.sort(key=lambda p: ((p.get("idx") or {}).get("value", ""), p.get("id", "")))
            for p in live:
                pid = p.get("id")
                redir = (p.get("redir") or {}).get("value")
                tmpl = (p.get("template") or {}).get("value") or "Blank"
                pages.append(PageRef(pid, rm_files.get(pid), tmpl,
                                     redir if isinstance(redir, int) and redir >= 0 else None))
        elif c.get("pages"):
            redirect = c.get("redirectionPageMap") or []
            templates = (self.src.read(f"{uid}.pagedata") or b"").decode("utf-8", "replace").splitlines()
            for i, pid in enumerate(c["pages"]):
                pdf_i = redirect[i] if i < len(redirect) else (i if self.pdf_bytes else None)
                if pdf_i is not None and pdf_i < 0:
                    pdf_i = None
                tmpl = templates[i].strip() if i < len(templates) and templates[i].strip() else "Blank"
                pages.append(PageRef(pid, rm_files.get(pid), tmpl, pdf_i if self.pdf_bytes else None))
        elif rm_files:
            for pid, n in sorted(rm_files.items()):
                pages.append(PageRef(pid, n))

        if not pages and self.pdf_bytes:
            # PDF bez listy stron – liczbę stron poda widok po wczytaniu PDF-a
            count = int(c.get("pageCount") or c.get("originalPageCount") or 0)
            pages = [PageRef(f"pdf-{i}", None, "Blank", i) for i in range(count)]
        return pages

    def _guess_size(self):
        for p in self.pages:
            if p.rm_name:
                try:
                    s = self.scene(self.pages.index(p))
                except Exception:
                    continue
                if s is not None and s.paper_size:
                    return s.paper_size
                return DEFAULT_SIZE
        return DEFAULT_SIZE

    def scene(self, index) -> Optional[rmlines.Scene]:
        """Wczytana scena strony (z pamięcią podręczną). None = strona bez notatek."""
        if index in self._scenes:
            return self._scenes[index]
        ref = self.pages[index]
        scene = None
        if ref.rm_name:
            data = self.src.read(ref.rm_name)
            if data:
                scene = rmlines.read_scene(data)
        self._scenes[index] = scene
        return scene

    def ensure_pdf_pages(self, pdf_count: int):
        """Dla PDF-ów bez listy stron w .content – uzupełnia strony wg PDF-a."""
        if not self.pages and pdf_count:
            self.pages = [PageRef(f"pdf-{i}", None, "Blank", i) for i in range(pdf_count)]


class SingleRmDocument(Document):
    def __init__(self, path: Path):
        self.src = None
        self.path = path
        self.metadata, self.content = {}, {}
        self.name = path.stem
        self.file_type = "notebook"
        self.pdf_bytes = None
        self._data = path.read_bytes()
        self._scenes = {}
        self.pages = [PageRef(path.stem, path.name)]
        self.last_opened_page = 0
        self.page_size = self._guess_size()

    def scene(self, index):
        if index not in self._scenes:
            self._scenes[index] = rmlines.read_scene(self._data)
        return self._scenes[index]


def _json(b: Optional[bytes]) -> dict:
    if not b:
        return {}
    try:
        return json.loads(b.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


# ----------------------------------------------------------------------
# Biblioteka: drzewo folderów / dokumentów
# ----------------------------------------------------------------------

@dataclass
class LibNode:
    name: str
    path: Optional[Path] = None      # ścieżka do otwarcia (None dla folderu)
    children: list = None
    modified: int = 0                # ms od epoki (z metadanych, jeśli są)

    @property
    def is_folder(self):
        return self.children is not None


def scan_library(root: Path) -> LibNode:
    """Buduje drzewo z folderu. Rozpoznaje .rmdoc/.rm oraz surowe dane xochitl."""
    root = Path(root)
    if any(root.glob("*.metadata")):
        return _scan_xochitl(root)
    return _scan_fs(root, root.name or str(root))


def _scan_fs(d: Path, name: str) -> LibNode:
    node = LibNode(name, None, [])
    try:
        entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return node
    for p in entries:
        if p.name.startswith("."):
            continue
        if p.is_dir():
            if any(p.glob("*.metadata")):
                sub = _scan_xochitl(p)
            else:
                sub = _scan_fs(p, p.name)
            if sub.children:
                node.children.append(sub)
        elif p.suffix.lower() in (".rmdoc", ".rm"):
            nm = p.stem
            if nm.lower().endswith(".pdf") or nm.lower().endswith(".epub"):
                nm = nm.rsplit(".", 1)[0]
            node.children.append(LibNode(nm, p, None, int(p.stat().st_mtime * 1000)))
    return node


def _scan_xochitl(d: Path) -> LibNode:
    items = {}
    for m in d.glob("*.metadata"):
        meta = _json(m.read_bytes())
        if meta.get("deleted"):
            continue
        items[m.stem] = meta
    nodes = {}
    for uid, meta in items.items():
        is_folder = meta.get("type") == "CollectionType"
        nodes[uid] = LibNode(meta.get("visibleName") or uid,
                             None if is_folder else d / f"{uid}.metadata",
                             [] if is_folder else None,
                             int(meta.get("lastModified") or 0))
    root = LibNode(d.name, None, [])
    trash = LibNode("Kosz", None, [])
    for uid, meta in items.items():
        parent = meta.get("parent") or ""
        if parent == "trash":
            trash.children.append(nodes[uid])
        elif parent in nodes and nodes[parent].is_folder:
            nodes[parent].children.append(nodes[uid])
        else:
            root.children.append(nodes[uid])

    def sort(n):
        n.children.sort(key=lambda c: (not c.is_folder, c.name.lower()))
        for c in n.children:
            if c.is_folder:
                sort(c)
    if trash.children:
        root.children.append(trash)
    sort(root)
    return root
