"""
search.py
=========
Interactive CLI for querying the clinical document search index.

Usage:
  # Single query
  python search.py "type 2 diabetes with peripheral neuropathy"

  # Interactive mode (no argument given)
  python search.py

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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import CHROMA_DIR, BM25_DIR
from src.nlp import SynonymIndex, load_nlp
from src.indexing import Embedder, VectorStore, BM25Index
from src.retrieval import QueryProcessor, HybridRetriever
from src.explainer import build_explanation, format_zero_result


def load_search_engine():
    """Initialise all components. Fails fast with clear message if index missing."""
    bm25_meta = BM25_DIR / "bm25_meta.json"
    if not bm25_meta.exists():
        print("\n  ERROR: Index not found.")
        print("  Run   python index_documents.py   first.")
        sys.exit(1)

    print("  Loading NLP pipeline...", end=" ", flush=True)
    synonym_index = SynonymIndex()
    nlp = load_nlp(synonym_index)
    print("done")

    print("  Loading embedding model...", end=" ", flush=True)
    embedder = Embedder()
    print("done")

    print("  Loading vector store...", end=" ", flush=True)
    vector_store = VectorStore()
    print(f"done  ({vector_store.count()} sections)")

    print("  Loading BM25 index...", end=" ", flush=True)
    bm25_index = BM25Index()
    bm25_index.load()
    print("done")

    query_processor = QueryProcessor(nlp, synonym_index, embedder)
    retriever = HybridRetriever(vector_store, bm25_index, synonym_index, embedder)

    return query_processor, retriever, synonym_index


def run_search(query_text: str, query_processor, retriever, synonym_index) -> None:
    """Run a single search and print results."""
    print(f"\n{'=' * 60}")
    print(f"  Query: \"{query_text}\"")
    print(f"{'=' * 60}")

    # Process query
    pq = query_processor.process(query_text)

    # Show what the system detected
    active = [e for e in pq["entities"] if not e["negated"]]
    negated = [e for e in pq["entities"] if e["negated"]]

    print(f"\n  Query tier    : {pq['tier'].upper()}")
    print(f"  Min threshold : {pq['threshold']}")

    if active:
        print(f"  Recognised    : {[e['canonical'] for e in active]}")
    if negated:
        print(f"  Negated (skipped): {[e['canonical'] for e in negated]}")
    if not pq["entities"]:
        print("  No clinical entities recognised.")

    # Retrieve
    results = retriever.retrieve(pq)

    if not results:
        print(format_zero_result(pq))
        return

    semantic = [r for r in results if not r["exact_match_override"]]
    exact    = [r for r in results if r["exact_match_override"]]

    summary_parts = []
    if semantic:
        summary_parts.append(f"{len(semantic)} semantic match(es)")
    if exact:
        summary_parts.append(f"{len(exact)} exact term match(es)")
    print(f"\n  {', '.join(summary_parts)}:\n")

    rank = 1
    if semantic:
        print(f"  ── Semantic Matches (composite score ≥ {pq['threshold']}) ──")
        for result in semantic:
            explanation = build_explanation(result, pq, synonym_index)
            print(explanation.format(rank))
            rank += 1

    if exact:
        print(f"\n  ── Exact Term Matches (query word found verbatim) ──")
        for result in exact:
            explanation = build_explanation(result, pq, synonym_index)
            print(explanation.format(rank))
            rank += 1

    print(f"\n{'=' * 60}\n")


def main() -> None:
    print("=" * 60)
    print("  Clinical Document Search POC")
    print("=" * 60)
    print()

    query_processor, retriever, synonym_index = load_search_engine()

    # Single query from command line
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        run_search(query, query_processor, retriever, synonym_index)
        return

    # Interactive loop
    print("\n  Ready. Type a clinical query and press Enter.")
    print("  Type 'exit' or press Ctrl+C to quit.\n")

    example_queries = [
        "type 2 diabetes with peripheral neuropathy",
        "heart failure and atrial fibrillation",
        "COPD exacerbation with pneumonia",
        "sepsis urinary tract infection",
        "stroke hypertension",
        "deep vein thrombosis post surgery",
        "quarterly budget forecast",   # non-clinical — should return nothing
        "MI",                           # abbreviation expansion test
    ]
    print("  Example queries to try:")
    for q in example_queries:
        print(f"    > {q}")
    print()

    while True:
        try:
            query = input("  Search > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye.")
            break

        if not query:
            continue
        if query.lower() in {"exit", "quit", "q"}:
            print("  Goodbye.")
            break

        run_search(query, query_processor, retriever, synonym_index)


if __name__ == "__main__":
    main()
