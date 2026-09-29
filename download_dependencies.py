"""
download_dependencies.py
========================
Fetches every model / database the project needs, idempotently.

Run this BEFORE ``index_documents.py`` or ``search.py``:

    python download_dependencies.py

What it does
------------
* Always:  ensures the spaCy base model (en_core_web_sm) is installed —
           both NER engines build on a base English pipeline.
* If NER_ENGINE == "medspacy" (fully-UMLS mode):
    - verifies the medspacy / quickumls packages are importable, and
    - checks for a QuickUMLS database under data/index/quickumls/.

The QuickUMLS database has TWO tiers:

  DEMO   — small sample UMLS bundled with medspaCy. No license needed.
           Good for POC / testing. (Selected automatically if no db present.)

  FULL   — the complete UMLS metathesaurus. Requires a free NLM UMLS
           license + multi-GB source files, then a one-time build:
               python -m quickumls.install <umls_installation_path> <dest>
           Pass --umls-path to have this script run that build for you.

The default "spacy" engine needs NONE of the UMLS machinery — only the base
model step above.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure src/ is importable when run from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import NER_ENGINE, SPACY_MODEL, QUICKUMLS_DB_DIR  # noqa: E402


def _run(cmd: list[str]) -> int:
    """Run a subprocess, streaming its output; return the exit code."""
    print(f"\n  $ {' '.join(cmd)}")
    return subprocess.call(cmd)


def ensure_spacy_model() -> bool:
    """Install the spaCy base model if it is not already present."""
    try:
        import spacy
        spacy.load(SPACY_MODEL)
        print(f"  [ok] spaCy model '{SPACY_MODEL}' already installed.")
        return True
    except ImportError:
        print("  [!!] spaCy is not installed. Run: pip install -r requirements.txt")
        return False
    except OSError:
        print(f"  [--] spaCy model '{SPACY_MODEL}' missing — downloading...")
        rc = _run([sys.executable, "-m", "spacy", "download", SPACY_MODEL])
        return rc == 0


def medspacy_importable() -> bool:
    """Check that the medspacy + quickumls stack can be imported."""
    missing = []
    for mod in ("medspacy", "quickumls"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"  [!!] Missing packages: {', '.join(missing)}")
        print("       Fix: pip install -r requirements.txt   (adds medspacy)")
        return False
    print("  [ok] medspacy + quickumls are importable.")
    return True


def quickumls_db_present() -> bool:
    """A QuickUMLS db is considered present if its directory is non-empty."""
    QUICKUMLS_DB_DIR.mkdir(parents=True, exist_ok=True)
    return any(QUICKUMLS_DB_DIR.iterdir())


def ensure_quickumls(umls_path: str | None) -> bool:
    """Ensure a QuickUMLS database exists (demo or full)."""
    if quickumls_db_present():
        print(f"  [ok] QuickUMLS db already present at {QUICKUMLS_DB_DIR}")
        return True

    # Full-UMLS build, if the user supplied a UMLS installation path.
    if umls_path:
        up = Path(umls_path)
        if not (up / "MRCONSO.RRF").exists():
            print(f"  [!!] {up} does not contain MRCONSO.RRF — is this a UMLS install?")
            return False
        print("  [--] Building QuickUMLS db from your UMLS installation "
              "(5-30 min)...")
        rc = _run([sys.executable, "-m", "quickumls.install", str(up),
                   str(QUICKUMLS_DB_DIR)])
        return rc == 0

    # Otherwise: point the user at the two options.
    print("\n  No QuickUMLS database found. Pick one:\n")
    print("  [A] DEMO (no license, POC-grade):")
    print("      medspaCy ships a small sample UMLS. Point the pipeline at it via:")
    print("          from medspacy.util import get_quickumls_demo_dir")
    print("          demo = get_quickumls_demo_dir()   # use as the QuickUMLS db_path")
    print("\n  [B] FULL (production, requires a free NLM UMLS license):")
    print("      1. Get a UMLS license:  https://uts.nlm.nih.gov/license.html")
    print("      2. Download UMLS files (MRCONSO.RRF, MRSTY.RRF, ...) and install")
    print("         with MetamorphoSys into a folder, e.g. C:\\umls\\2024AA")
    print("      3. Re-run this script pointing at that folder:")
    print(f"          python download_dependencies.py --umls-path C:\\umls\\2024AA")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Download project models/databases.")
    parser.add_argument(
        "--umls-path", default=None,
        help="Path to a UMLS installation (contains MRCONSO.RRF) to build the "
             "full QuickUMLS db from. Only used in medspacy mode.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Clinical Document Search POC — Dependency Downloader")
    print("=" * 60)
    print(f"\n  NER engine : {NER_ENGINE}")

    # Step 1: base model (both engines).
    print("\n[1/2] spaCy base model")
    if not ensure_spacy_model():
        return 1

    # Step 2: UMLS db (medspacy mode only).
    if NER_ENGINE == "medspacy":
        print("\n[2/2] QuickUMLS database (medspacy mode)")
        if not medspacy_importable():
            return 1
        if not ensure_quickumls(args.umls_path):
            print("\n  UMLS db not ready — medspacy mode will fall back to the")
            print("  demo/sample UMLS or surface-form matching until one is built.")
            return 1
    else:
        print('\n[2/2] QuickUMLS database — skipped (NER_ENGINE="spacy").')

    print("\n" + "=" * 60)
    print("  Done. Next:  python index_documents.py")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
