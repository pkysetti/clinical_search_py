"""
index_documents.py
==================
Ingests all .txt files from data/documents/ into the search index.

Usage:
  python index_documents.py

Run this once before running search.py.
Re-run any time you add new documents to data/documents/.

NOTE (Windows sqlite3 issue):
  If you see "RuntimeError: Your system has an unsupported version of sqlite3",
  install pysqlite3-binary and uncomment the three lines below.
"""

# ── Windows sqlite3 fix (uncomment if needed) ────────────────────────────────
# import sys, pysqlite3
# sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
# ─────────────────────────────────────────────────────────────────────────────

import sys
from pathlib import Path

# Ensure src/ is on the path when running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import DOCUMENTS_DIR, CHROMA_DIR, BM25_DIR
from src.indexing import DocumentIndexer


def main() -> None:
    print("=" * 60)
    print("  Clinical Document Search POC — Indexer")
    print("=" * 60)
    print(f"\n  Documents folder : {DOCUMENTS_DIR}")
    print(f"  Vector index     : {CHROMA_DIR}")
    print(f"  BM25 index       : {BM25_DIR}")

    # Sanity check
    docs = list(DOCUMENTS_DIR.glob("*.txt"))
    if not docs:
        print(f"\n  ERROR: No .txt files found in {DOCUMENTS_DIR}")
        print("  Add documents and re-run.")
        sys.exit(1)

    print(f"\n  Found {len(docs)} document(s) to index.\n")
    print("  Note: First run downloads the embedding model (~90 MB).")
    print("  This may take a few minutes on slow connections.\n")

    indexer = DocumentIndexer()
    indexer.index_all(DOCUMENTS_DIR)

    print("\n  Indexing complete. Run search.py to query the index.")
    print("=" * 60)


if __name__ == "__main__":
    main()
