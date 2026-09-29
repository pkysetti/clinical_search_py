"""
setup_check.py
==============
Verifies that all dependencies are installed and the environment is
ready to run the Clinical Document Search POC.

Usage:
  python setup_check.py

Works on macOS and Windows.
"""

import sys
import importlib
from pathlib import Path

REQUIRED = [
    ("spacy",               "spacy"),
    ("sentence_transformers","sentence-transformers"),
    ("chromadb",            "chromadb"),
    ("rank_bm25",           "rank-bm25"),
    ("tqdm",                "tqdm"),
]

SPACY_MODEL = "en_core_web_sm"

print("=" * 60)
print("  Clinical Document Search POC — Environment Check")
print("=" * 60)
print(f"\n  Python : {sys.version}")
print(f"  Platform: {sys.platform}\n")

all_ok = True

# ── Check packages ────────────────────────────────────────────────────────────
print("  [1/3] Checking Python packages...")
for module_name, pip_name in REQUIRED:
    try:
        mod = importlib.import_module(module_name)
        version = getattr(mod, "__version__", "?")
        print(f"        ✓  {pip_name:<30} {version}")
    except ImportError:
        print(f"        ✗  {pip_name:<30} NOT FOUND")
        print(f"             Fix: pip install {pip_name}")
        all_ok = False

# ── Check spaCy model ─────────────────────────────────────────────────────────
print(f"\n  [2/3] Checking spaCy model ({SPACY_MODEL})...")
try:
    import spacy
    nlp = spacy.load(SPACY_MODEL)
    print(f"        ✓  {SPACY_MODEL} loaded OK")
except OSError:
    print(f"        ✗  {SPACY_MODEL} NOT found")
    print(f"             Fix: python -m spacy download {SPACY_MODEL}")
    all_ok = False

# ── Check data files ──────────────────────────────────────────────────────────
print("\n  [3/3] Checking data files...")
base = Path(__file__).resolve().parent
checks = {
    "data/synonyms.json":  base / "data" / "synonyms.json",
    "data/documents/":     base / "data" / "documents",
}
for label, path in checks.items():
    if path.exists():
        if path.is_dir():
            n = len(list(path.glob("*.txt")))
            print(f"        ✓  {label} ({n} .txt files)")
        else:
            print(f"        ✓  {label}")
    else:
        print(f"        ✗  {label}  MISSING")
        all_ok = False

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if all_ok:
    print("  ✅  All checks passed. You are ready to run:")
    print()
    print("       python index_documents.py   # build the search index")
    print("       python search.py            # start interactive search")
    print()
else:
    print("  ❌  Some checks failed. Fix the issues above, then re-run this script.")
    sys.exit(1)

print("=" * 60)
