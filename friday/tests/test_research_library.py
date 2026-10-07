"""
Phase 5A — research library: ingestion, extraction, search.

Principles under test:
- Books, papers, notes and articles become searchable text, locally, with
  their page/chapter locations kept.
- Nothing is executed (scripts dropped), nothing unsafe is accepted
  (fake/locked/scanned/oversized/zip-bomb files are rejected with a reason),
  and nothing sensitive is indexed.
- The full text lives only in a private folder outside any Git repo.
"""

import io
import json
import os
import stat
import subprocess
import zipfile

import fitz
import pytest
import sys

from friday.research import ExtractionError, LibraryError, ResearchLibrary, chunk_text, extract
from friday.research.__main__ import main as cli

extract_module = sys.modules["friday.research.extract"]

LOREM = ("Prediction markets aggregate dispersed information into prices. "
         "A calibrated forecaster's stated probabilities match observed frequencies. ")


# ---------------------------------------------------------------- sample files

def make_pdf(path, pages, title="Superforecasting Notes", author="Test Author", password=None):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=9)
        else:
            page.draw_rect(fitz.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    doc.set_metadata({"title": title, "author": author})
    kwargs = {}
    if password:
        kwargs = dict(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password)
    doc.save(str(path), **kwargs)
    return path


def make_epub(path, chapters, title="Fooled by Randomness", author="N. Author", extra=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="ch{i}.xhtml"/>' for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        items += extra or ""
        spine += '<itemref idref="evil"/>' if extra else ""
        z.writestr("OEBPS/content.opf",
                   '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f'<metadata><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>'
                   f'<manifest>{items}</manifest><spine>{spine}</spine></package>')
        for i, (heading, body) in enumerate(chapters):
            z.writestr(f"OEBPS/ch{i}.xhtml",
                       f"<html><head><title>{heading}</title><script>steal()</script></head>"
                       f"<body><h1>{heading}</h1><p>{body}</p></body></html>")
    return path


@pytest.fixture
def lib(tmp_path):
    return ResearchLibrary(tmp_path / "library").init()


# ---------------------------------------------------------------- extraction

def test_pdf_pages_title_and_locations(lib, tmp_path):
    pdf = make_pdf(tmp_path / "sf.pdf", [LOREM * 3, "Kelly criterion sizing and bankroll risk. " * 8, LOREM * 2])
    r = lib.add(pdf, kind="book")
    assert r.status == "ADDED" and r.title == "Superforecasting Notes" and r.chunks >= 3
    [hit] = lib.search("kelly criterion", limit=1)
    assert hit["location"] == "p. 2"
    assert "[kelly]" in hit["snippet"].lower() and "[criterion]" in hit["snippet"].lower()
    assert lib.get(r.doc_id)["pages"] == 3 and lib.get(r.doc_id)["author"] == "Test Author"


def test_epub_chapters_in_spine_order_scripts_dropped(lib, tmp_path):
    epub = make_epub(tmp_path / "fbr.epub", [("Chapter One", LOREM * 4),
                                              ("Survivorship Bias", "Survivorship bias hides the failures. " * 10)])
    r = lib.add(epub)
    assert r.status == "ADDED" and r.title == "Fooled by Randomness"
    [hit] = lib.search("survivorship bias", limit=1)
    assert hit["location"] == "ch. 2: Survivorship Bias"
    assert lib.search("steal") == []                       # <script> never indexed


def test_epub_manifest_path_traversal_is_ignored(lib, tmp_path):
    epub = make_epub(tmp_path / "evil.epub", [("One", LOREM * 4)],
                     extra='<item id="evil" href="../../../../etc/passwd"/>')
    assert lib.add(epub).status == "ADDED"
    assert lib.search("root") == []


def test_html_markdown_text(lib, tmp_path):
    html = tmp_path / "article.html"
    html.write_text("<html><head><title>Market Microstructure</title><style>.x{}</style></head>"
                    "<body><nav>Menu Login</nav><article><p>" + "Order book depth and spread. " * 12 +
                    "</p></article><script>execute_trade()</script></body></html>", encoding="utf-8")
    md = tmp_path / "idea.md"
    md.write_text("# My Base Rates Idea\n\n" + "Base rates beat narratives. " * 12, encoding="utf-8")
    txt = tmp_path / "notes.txt"
    txt.write_text("Plain notes about liquidity provision. " * 10, encoding="utf-8")
    assert lib.add(html, kind="article").title == "Market Microstructure"
    assert lib.add(md, kind="note").title == "My Base Rates Idea"
    assert lib.add(txt, kind="note").title == "notes"
    assert lib.search("execute_trade") == [] and lib.search("login menu") == []
    assert lib.search("base rates", kind="note")[0]["title"] == "My Base Rates Idea"
    assert lib.search("base rates", kind="paper") == []


# ---------------------------------------------------------------- rejection

def test_rejections_have_reasons(lib, tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_text("not really a pdf " * 50)
    scanned = make_pdf(tmp_path / "scan.pdf", ["", ""])
    locked = make_pdf(tmp_path / "locked.pdf", [LOREM * 3], password="pw123456")
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ" + b"\0" * 500)
    tiny = tmp_path / "tiny.txt"
    tiny.write_text("too short")
    link = tmp_path / "link.txt"
    os.symlink(tiny, link)
    reasons = {p.name: lib.add(p).reason for p in (fake, scanned, locked, exe, tiny, link)}
    assert "not a PDF" in reasons["fake.pdf"]
    assert "scanned PDF" in reasons["scan.pdf"]
    assert "password-protected" in reasons["locked.pdf"]
    assert "unsupported file type" in reasons["tool.exe"]
    assert "no usable text" in reasons["tiny.txt"]
    assert "not a regular file" in reasons["link.txt"]
    assert lib.status()["documents"] == 0


def test_epub_zip_bomb_limits(lib, tmp_path, monkeypatch):
    epub = make_epub(tmp_path / "big.epub", [("One", LOREM * 4)])
    monkeypatch.setattr(extract_module, "MAX_EPUB_UNCOMPRESSED", 100)
    assert "expands beyond" in lib.add(epub).reason
    monkeypatch.setattr(extract_module, "MAX_EPUB_MEMBERS", 2)
    assert "too many files" in lib.add(epub).reason


def test_invalid_kind(lib, tmp_path):
    md = tmp_path / "x.md"
    md.write_text(LOREM * 5)
    assert "kind must be" in lib.add(md, kind="strategy").reason


# ---------------------------------------------------------------- safety

def test_duplicates_are_detected(lib, tmp_path):
    md = tmp_path / "a.md"
    md.write_text(LOREM * 5)
    first = lib.add(md)
    copy = tmp_path / "copy-of-a.md"
    copy.write_bytes(md.read_bytes())
    second = lib.add(copy)
    assert second.status == "DUPLICATE" and second.doc_id == first.doc_id
    assert lib.status()["documents"] == 1


def test_secret_passages_are_withheld(lib, tmp_path):
    key = "sk-" + "ant-" + "api03-abcdefghijklmnopqrstuv"
    md = tmp_path / "leaky.md"
    md.write_text(LOREM * 12 + "\n\nMy config: " + key + "\n\n" + "Calibration curves matter. " * 12)
    r = lib.add(md, kind="note")
    assert r.status == "ADDED" and r.withheld_chunks >= 1
    assert lib.search("config") == [] and lib.search("abcdefghijklmnopqrstuv") == []
    assert lib.search("calibration curves")


def test_library_inside_git_repo_is_refused(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    with pytest.raises(LibraryError, match="must never be committed"):
        ResearchLibrary(tmp_path / "repo" / "library")


def test_private_permissions_and_read_only_originals(lib, tmp_path):
    md = tmp_path / "p.md"
    md.write_text(LOREM * 5)
    r = lib.add(md)
    assert stat.S_IMODE(os.stat(lib.root).st_mode) == 0o700
    original = lib.root / lib.get(r.doc_id)["original"]
    assert original.read_bytes() == md.read_bytes()
    assert stat.S_IMODE(os.stat(original).st_mode) == stat.S_IRUSR
    assert lib.remove(r.doc_id) and not original.exists()
    assert lib.search("prediction markets") == [] and lib.remove(r.doc_id) is False


# ---------------------------------------------------------------- search

def test_search_all_words_then_any_word(lib, tmp_path):
    md = tmp_path / "s.md"
    md.write_text("Brier score measures forecast accuracy. " * 8 + "\n\n" + "Liquidity is thin. " * 8)
    lib.add(md)
    assert lib.search("brier accuracy")[0]["snippet"]
    assert lib.search("brier nonexistentword")               # falls back to any-word


@pytest.mark.parametrize("query", ['"; DROP TABLE documents; --', "NEAR(a b)", "a* OR", "()\"'", ""])
def test_search_is_injection_safe(lib, tmp_path, query):
    md = tmp_path / "q.md"
    md.write_text(LOREM * 5)
    lib.add(md)
    lib.search(query)                                        # never raises
    assert lib.status()["documents"] == 1


def test_chunking_respects_size():
    text = ("Sentence number one is here. " * 200) + "\n\n" + "x" * 5000
    chunks = chunk_text(text, size=1200)
    assert all(len(c) <= 1200 for c in chunks)
    assert "".join(chunks).replace("\n", "").replace(" ", "").count("x") == 5000


# ---------------------------------------------------------------- inbox + CLI

def test_inbox_ingestion(lib, tmp_path):
    good = lib.inbox / "good.md"
    good.write_text(LOREM * 5)
    bad = lib.inbox / "bad.pdf"
    bad.write_text("fake")
    (lib.inbox / ".partial-upload").write_text("ignored")
    results = {r.name: r.status for r in lib.ingest_inbox(kind="note")}
    assert results == {"bad.pdf": "REJECTED", "good.md": "ADDED"}
    assert not good.exists() and (lib.rejected / "bad.pdf").exists()
    assert "not a PDF" in (lib.rejected / "bad.pdf.reason.txt").read_text()
    assert lib.status()["inbox_waiting"] == 0


def test_cli_end_to_end(tmp_path, capsys):
    root = str(tmp_path / "cli-lib")
    assert cli(["status", "--library", root]) == 2              # not initialised
    assert cli(["init", "--library", root]) == 0
    pdf = make_pdf(tmp_path / "paper.pdf", ["Favourite-longshot bias in betting markets. " * 12])
    assert cli(["add", str(pdf), "--kind", "paper", "--tag", "bias", "--library", root]) == 0
    capsys.readouterr()
    assert cli(["search", "longshot", "bias", "--json", "--library", root]) == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits[0]["kind"] == "paper" and hits[0]["location"] == "p. 1"
    assert cli(["list", "--json", "--library", root]) == 0
    docs = json.loads(capsys.readouterr().out)
    assert docs[0]["tags"] == ["bias"]
    assert cli(["remove", docs[0]["doc_id"], "--library", root]) == 2   # needs --confirm
    assert cli(["remove", docs[0]["doc_id"], "--confirm", "--library", root]) == 0


# ---------------------------------------------------------------- app

@pytest.fixture
def app_lib(tmp_path, monkeypatch):
    import friday.app.db as db_module
    import friday.app.main as main
    from friday.core.health import HealthManager
    from friday.core.lifecycle import LifecycleManager
    from friday.core.state import StateManager
    from friday.tests.test_app_hermes import FakeIntelligence, FakeMarket

    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "friday.db"))
    state = StateManager("PAPER")
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "health_manager", HealthManager(state))
    monkeypatch.setattr(main, "lifecycle", LifecycleManager(state, main.events))
    monkeypatch.setattr(main, "INTELLIGENCE_FACTORY", FakeIntelligence)
    monkeypatch.setattr(main, "MARKET_FACTORY", FakeMarket)
    for name in ("HERMES_HEALTH_INTERVAL_SECONDS", "HERMES_MARKET_SCAN_INTERVAL_SECONDS"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("FRIDAY_KNOWLEDGE_ENABLED", "false")
    library = ResearchLibrary(tmp_path / "library").init()
    monkeypatch.setenv("FRIDAY_LIBRARY_PATH", str(library.root))
    return main, library


def test_app_upload_and_search(app_lib, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main, library = app_lib
    pdf = make_pdf(tmp_path / "Longshot Paper.pdf", ["Favourite-longshot bias in betting markets. " * 12])
    with TestClient(main.app) as client:
        assert client.get("/health").json()["components"]["research"] == "READY"
        up = client.post("/api/books?kind=paper",
                         files={"file": ("../../etc/Longshot Paper.pdf", pdf.read_bytes(), "application/pdf")}).json()
        assert up["status"] == "ADDED" and up["name"] == "Longshot Paper.pdf"
        again = client.post("/api/books?kind=paper", files={"file": ("x.pdf", pdf.read_bytes())}).json()
        assert again["status"] == "DUPLICATE"
        found = client.get("/api/research/search", params={"q": "longshot bias"}).json()
        assert found["trust"] == "UNTRUSTED_DATA" and found["hits"][0]["kind"] == "paper"
        assert client.get("/api/research/search", params={"q": "x", "kind": "strategy"}).status_code == 400
        assert client.post("/api/books?kind=strategy", files={"file": ("a.md", b"x")}).status_code == 400
        monkeypatch.setattr(main, "MAX_FILE_BYTES", 10)
        assert client.post("/api/books", files={"file": ("big.md", b"y" * 100)}).status_code == 413
        status = client.get("/api/research/status").json()
    assert status["documents"] == 1 and status["by_kind"] == {"paper": 1}
    leftovers = [p for p in library.inbox.rglob("*") if p.is_file() or p.name.startswith(".")]
    assert leftovers == []                                       # no staging left behind
    assert not (main.BASE_DIR / "books").exists() or not any((main.BASE_DIR / "books").iterdir())


def test_app_without_library_is_healthy(app_lib, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    main, _ = app_lib
    monkeypatch.setenv("FRIDAY_LIBRARY_PATH", str(tmp_path / "none"))
    with TestClient(main.app) as client:
        health = client.get("/health").json()
        assert health["status"] == "HEALTHY" and health["components"]["research"] == "DISABLED"
        assert "python -m friday.research init" in client.get("/api/research/status").json()["reason"]
        assert client.get("/api/research/search", params={"q": "x"}).status_code == 503
        assert client.post("/api/books", files={"file": ("a.md", b"x")}).status_code == 503
