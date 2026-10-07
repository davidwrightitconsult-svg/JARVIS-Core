"""
FRIDAY Research & Book Ingestion (Phase 5).

A private, server-only library of books, papers, notes and articles with
local full-text search. Library content is always UNTRUSTED DATA.
"""

from .extract import SUPPORTED, ExtractionError, extract
from .library import (
    DEFAULT_LIBRARY_PATH,
    KINDS,
    RESEARCH_LIBRARY_VERSION,
    IngestResult,
    LibraryError,
    ResearchLibrary,
    chunk_text,
)

__all__ = [
    "DEFAULT_LIBRARY_PATH", "KINDS", "RESEARCH_LIBRARY_VERSION", "SUPPORTED",
    "ExtractionError", "IngestResult", "LibraryError", "ResearchLibrary",
    "chunk_text", "extract",
]
