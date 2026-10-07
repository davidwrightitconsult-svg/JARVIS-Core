"""
FRIDAY research text extraction.

Phase 5A:
- PDF   via PyMuPDF (already a FRIDAY dependency), one section per page
- EPUB  via the standard library (zip + OPF spine order), one section per
        chapter; zip-bomb and member-count limits
- HTML  via the standard library parser (scripts/styles/nav dropped)
- Markdown / plain text decoded as UTF-8
- Nothing is executed or rendered: PDF JavaScript, EPUB scripts and HTML
  scripts are never run; only text is read
- Scanned PDFs (images, no text layer) are reported, not guessed at —
  OCR is not part of 5A

Every extractor returns (metadata, [Section(location, text)]).
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

SUPPORTED = {
    ".pdf": "pdf", ".epub": "epub",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".md": "markdown", ".markdown": "markdown", ".txt": "text",
}
MAX_PDF_PAGES = 5000
MAX_EPUB_MEMBERS = 5000
MAX_EPUB_UNCOMPRESSED = 300 * 1024 * 1024
MAX_TEXT_BYTES = 50 * 1024 * 1024


class ExtractionError(ValueError):
    """The file cannot be turned into text (reason is safe to show)."""


@dataclass
class Section:
    location: str          # "p. 12", "ch. 3: Title", "" for single-section docs
    text: str


@dataclass
class Extracted:
    format: str
    title: str | None = None
    author: str | None = None
    pages: int | None = None
    sections: list[Section] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(s.text) for s in self.sections)


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n[ ]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- HTML

class _TextParser(HTMLParser):
    _SKIP = {"script", "style", "noscript", "nav", "header", "footer", "svg", "form", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul", "ol", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.focus: list[str] = []      # text inside <article>/<main>
        self.focus_depth = 0
        self.skip = 0
        self.title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        if tag in ("article", "main"):
            self.focus_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self._emit("\n\n" if tag not in ("br", "li") else "\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self.skip:
            self.skip -= 1
        if tag in ("article", "main") and self.focus_depth:
            self.focus_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self._emit("\n\n" if tag not in ("br", "li") else "\n")

    def _emit(self, text):
        self.parts.append(text)
        if self.focus_depth:
            self.focus.append(text)

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = " ".join(data.split())[:200] or None
        if not self.skip:
            self._emit(data)


def html_to_text(html: str) -> tuple[str | None, str]:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    # Prefer the page's <article>/<main> content when it has a real body;
    # otherwise fall back to all visible text.
    focused = _clean(unescape("".join(parser.focus)))
    if len(focused) >= 200:
        return parser.title, focused
    return parser.title, _clean(unescape("".join(parser.parts)))


# ---------------------------------------------------------------- formats

def _pdf(path: Path) -> Extracted:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("PDF support needs PyMuPDF (pip install PyMuPDF)") from exc
    try:
        doc = fitz.open(str(path))
    except Exception:
        raise ExtractionError("not a readable PDF") from None
    with doc:
        if doc.needs_pass:
            raise ExtractionError("PDF is password-protected")
        if doc.page_count > MAX_PDF_PAGES:
            raise ExtractionError(f"PDF has {doc.page_count} pages (limit {MAX_PDF_PAGES})")
        meta = doc.metadata or {}
        sections = []
        for number, page in enumerate(doc, start=1):
            text = _clean(page.get_text("text") or "")
            if text:
                sections.append(Section(f"p. {number}", text))
        return Extracted("pdf", title=(meta.get("title") or "").strip() or None,
                         author=(meta.get("author") or "").strip() or None,
                         pages=doc.page_count, sections=sections)


def _epub(path: Path) -> Extracted:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ExtractionError("not a readable EPUB (bad zip)") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_EPUB_MEMBERS:
            raise ExtractionError("EPUB has too many files")
        if sum(i.file_size for i in infos) > MAX_EPUB_UNCOMPRESSED:
            raise ExtractionError("EPUB expands beyond the size limit")
        names = {i.filename for i in infos}
        try:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
            opf = ElementTree.fromstring(archive.read(rootfile))
        except Exception:
            raise ExtractionError("EPUB has no readable package document") from None
        base = posixpath.dirname(rootfile)

        def first(tag):
            for e in opf.iter():
                if e.tag.endswith("}" + tag) and (e.text or "").strip():
                    return " ".join(e.text.split())[:200]
            return None

        manifest = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("item")}
        spine = [e.get("idref") for e in opf.iter() if e.tag.endswith("itemref")]
        sections = []
        for index, idref in enumerate(spine, start=1):
            href = manifest.get(idref)
            if not href:
                continue
            member = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
            if member not in names:
                continue
            chapter_title, text = html_to_text(archive.read(member).decode("utf-8", "replace"))
            if text:
                label = f"ch. {index}" + (f": {chapter_title}" if chapter_title else "")
                sections.append(Section(label[:120], text))
        return Extracted("epub", title=first("title"), author=first("creator"), sections=sections)


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ExtractionError("text file too large")
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _html(path: Path) -> Extracted:
    title, text = html_to_text(_read_text(path))
    return Extracted("html", title=title, sections=[Section("", text)] if text else [])


def _markdown(path: Path) -> Extracted:
    text = _clean(_read_text(path))
    match = re.search(r"^#\s+(.+)$", text, re.M)
    return Extracted("markdown", title=match.group(1).strip()[:200] if match else None,
                     sections=[Section("", text)] if text else [])


def _text(path: Path) -> Extracted:
    text = _clean(_read_text(path))
    return Extracted("text", sections=[Section("", text)] if text else [])


_EXTRACTORS = {"pdf": _pdf, "epub": _epub, "html": _html, "markdown": _markdown, "text": _text}


def detect_format(path: Path) -> str:
    fmt = SUPPORTED.get(path.suffix.lower())
    if fmt is None:
        raise ExtractionError(f"unsupported file type {path.suffix or '(none)'}; "
                              f"supported: {', '.join(sorted(SUPPORTED))}")
    with path.open("rb") as handle:
        head = handle.read(8)
    if fmt == "pdf" and not head.startswith(b"%PDF"):
        raise ExtractionError("file ends in .pdf but is not a PDF")
    if fmt == "epub" and not head.startswith(b"PK"):
        raise ExtractionError("file ends in .epub but is not an EPUB")
    return fmt


def extract(path: str | Path) -> Extracted:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ExtractionError("not a regular file")
    result = _EXTRACTORS[detect_format(path)](path)
    if result.chars < 200:
        hint = " (scanned PDF with no text layer? OCR is not supported yet)" if result.format == "pdf" else ""
        raise ExtractionError(f"no usable text extracted{hint}")
    return result
