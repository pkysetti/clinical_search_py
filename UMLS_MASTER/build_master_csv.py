#!/usr/bin/env python3
"""
build_master_csv.py
===================

Emit flat, queryable **master term files** (CSV) from the UMLS Metathesaurus,
reusing the verified streaming core in ``../UMLS_FILTER/build_concept_dictionary.py``.
No parser logic is duplicated here — we import ``harvest`` / ``load_semantic_types``
and only add the CSV assembly + emission layer.

Outputs (default dir: ``data/master/``):

  drug-master.csv      one row per drug CUI, real RxNorm ID in ``rxcui``
  problem-terms.csv    one row per diagnosis/problem CUI (disorder/injury/symptom/finding)
  procedure-terms.csv  one row per procedure CUI
  anatomy-terms.csv    one row per anatomy CUI (only when anatomy is in scope)
  synonym-map.csv      denormalized: one row per surface form -> concept + IDs
  concepts.jsonl       durable checkpoint (one concept per line) — the resume source
  manifest.json        release stamp, per-file row counts, chunk lists

Design notes
------------
* Single streaming pass over MRCONSO.RRF (a few minutes for ~2.2 GB). The
  expensive step is ``harvest``; everything after it (assembly + CSV write) is
  fast and regenerable from ``concepts.jsonl`` via ``--csv-only``.
* Rows are written with the stdlib ``csv`` module (proper quoting). List-valued
  cells (synonyms, brands, semantic_types, sources) are joined with ``"; "``.
* ``--chunk-size N`` splits any oversized partition into ``<stem>-0001.csv``,
  ``-0002.csv`` ... each re-carrying the header. Default 0 = one file per
  partition (clinical scope is bounded, so this is what you want).

USAGE
-----
    # Clinical + labs/anatomy scope (the default), single pass, real release
    python UMLS_MASTER\\build_master_csv.py ^
        --meta-dir C:\\Inference\\umls-2026AA-full\\2026AA-full\\2026AA\\META

    # Re-emit CSVs only from the existing checkpoint (no MRCONSO re-scan)
    python UMLS_MASTER\\build_master_csv.py --csv-only

    # Cap breadth / split huge partitions into 50k-row chunks
    python UMLS_MASTER\\build_master_csv.py --meta-dir <META> --max-concepts 200000 --chunk-size 50000
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# ── import the verified core from the sibling UMLS_FILTER package ────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "UMLS_FILTER"))
import build_concept_dictionary as bcd  # noqa: E402

# Default real release (verified present on this machine, 2026-09-30).
DEFAULT_META = r"C:\Inference\umls-2026AA-full\2026AA-full\2026AA\META"

# ── TUI code -> semantic-type name ───────────────────────────────────────────
# Some releases (incl. 2026AA on this machine) ship MRSTY with the *name*
# columns empty and only the TUI code populated, e.g. "C0027051|T047||||".
# build_concept_dictionary.load_semantic_types reads the name column, so it
# matches nothing on such releases. We therefore classify by TUI code using
# this map (validated 2026-09-30 against known CUIs: MI=T047 disorder,
# chest pain=T184 symptom, morphine sulfate=T121 drug). Codes are stable
# across UMLS releases.
TUI_TO_NAME = {
    "T047": "Disease or Syndrome",
    "T185": "Mental or Behavioral Dysfunction",
    "T192": "Neoplastic Process",
    "T272": "Congenital Abnormality",
    "T273": "Acquired Abnormality",
    "T274": "Anatomical Abnormality",
    "T046": "Pathologic Function",
    "T204": "Cell or Molecular Dysfunction",
    "T218": "Experimental Model of Disease",
    "T291": "Injury or Poisoning",
    "T184": "Sign or Symptom",
    "T033": "Finding",
    "T371": "Clinical Attribute",
    "T404": "Laboratory or Test Result",
    "T061": "Therapeutic or Preventive Procedure",
    "T060": "Diagnostic Procedure",
    "T062": "Laboratory Procedure",
    "T059": "Health Care Activity",
    "T121": "Pharmacologic Substance",
    "T122": "Clinical Drug",
    "T119": "Antibiotic",
    "T109": "Organic Chemical",
    "T128": "Body Part, Organ, or Organ Component",
    "T129": "Body Location or Region",
}


def _mrsty_has_names(path) -> bool:
    """True if the STY name column is populated (older releases); False if only
    TUI codes are present (e.g. 2026AA)."""
    for row in bcd.rrf_rows(path, len(bcd.MRSTY_COLS)):
        return bool(row[bcd.S["STY"]].strip())
    return False


def load_semantic_types_by_code(path, allowed_stys):
    """Classify CUIs by TUI code (for releases whose MRSTY name columns are empty).

    Returns the same shape as bcd.load_semantic_types:
        {cui: (kind, fallback_category, [sty_name, ...])}

    A concept often carries several semantic types (e.g. aspirin = Organic
    Chemical + Pharmacologic Substance). We prefer a *drug* classification over
    *substance* so clinical drugs read as "medication", not "substance".
    """
    names_by_cui: dict[str, list[str]] = {}
    for row in bcd.rrf_rows(path, len(bcd.MRSTY_COLS)):
        name = TUI_TO_NAME.get(row[bcd.S["TUI"]])
        if name is None or name not in allowed_stys:
            continue
        lst = names_by_cui.setdefault(row[bcd.S["CUI"]], [])
        if name not in lst:
            lst.append(name)

    out = {}
    for cui, names in names_by_cui.items():
        ordered = sorted(names, key=lambda n: 0 if bcd.STY_MAP[n][0] == "drug" else 1)
        kind, cat = bcd.STY_MAP[ordered[0]]
        out[cui] = (kind, cat, names)
    print(f"[MRSTY-code] {len(out):,} CUIs in scope", file=sys.stderr)
    return out

# ── kind -> output file stem ─────────────────────────────────────────────────
FILE_BY_KIND = {
    "drug":      "drug-master",
    "substance": "drug-master",
    "disorder":  "problem-terms",
    "injury":    "problem-terms",
    "symptom":   "problem-terms",
    "finding":   "problem-terms",
    "procedure": "procedure-terms",
    "anatomy":   "anatomy-terms",
}

# ── column layouts per file ──────────────────────────────────────────────────
DRUG_COLS = ["cui", "canonical", "rxcui", "category", "synonyms",
             "abbreviations", "brands", "semantic_types", "sources"]
PROBLEM_COLS = ["cui", "canonical", "icd10", "category", "synonyms",
                "abbreviations", "semantic_types", "sources"]
PROC_COLS = ["cui", "canonical", "category", "synonyms",
             "abbreviations", "semantic_types", "sources"]
ANAT_COLS = PROC_COLS  # anatomy shares the procedure layout

COLS_BY_STEM = {
    "drug-master": DRUG_COLS,
    "problem-terms": PROBLEM_COLS,
    "procedure-terms": PROC_COLS,
    "anatomy-terms": ANAT_COLS,
}
SYNONYM_MAP_COLS = ["term", "term_type", "cui", "canonical", "category",
                    "kind", "rxcui", "icd10"]

SEP = "; "  # in-cell list separator (avoids CSV comma ambiguity)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ==========================================================================
# assembly — mirrors build_concept_dictionary.build() but keeps plain dicts
# ==========================================================================
def assemble(sty_info, syns, abbrevs, brands, icd, rxcui, pref, sabs,
             min_synonyms: int) -> list[dict]:
    concepts = []
    for cui, syn_map in syns.items():
        canonical = pref.get(cui, (None, None))[1]
        if not canonical:
            continue
        synonyms = sorted(s for s in syn_map if s != canonical)
        abbr = sorted(abbrevs.get(cui, ()))
        brand_list = sorted(brands.get(cui, ()))
        if len(synonyms) + len(abbr) + len(brand_list) < min_synonyms:
            continue

        code = icd[cui].most_common(1)[0][0] if icd.get(cui) else None
        kind, fallback_cat, stys = sty_info.get(cui, (None, None, []))
        category = bcd.icd10_category(code) or fallback_cat or "unclassified"

        # A concept carrying a real RxNorm ID is, by definition, a medication —
        # normalise the label so drugs don't read as "substance".
        if rxcui.get(cui) and kind in ("drug", "substance"):
            category = "medication"

        concepts.append({
            "cui": cui,
            "canonical": canonical,
            "synonyms": synonyms,
            "abbreviations": abbr,
            "brands": brand_list,
            "icd10": code,
            "rxcui": rxcui.get(cui),
            "category": category,
            "kind": kind,
            "semantic_types": sorted(set(stys)),
            "sources": sorted(sabs.get(cui, ())),
        })

    # breadth-first ordering: richest concepts first, then stable by name
    concepts.sort(key=lambda c: (-(len(c["synonyms"]) + len(c["abbreviations"])),
                                 c["canonical"]))
    return concepts


def run_extraction(args) -> list[dict]:
    meta = args.meta_dir
    mrconso = os.path.join(meta, "MRCONSO.RRF")
    mrsty = os.path.join(meta, "MRSTY.RRF")
    if not os.path.exists(mrconso):
        sys.exit(f"MRCONSO.RRF not found under {meta}")

    # Clinical + labs/anatomy == every kind in STY_MAP (no further restriction).
    allowed_stys = set(bcd.STY_MAP)
    if os.path.exists(mrsty):
        if _mrsty_has_names(mrsty):
            sty_info = bcd.load_semantic_types(mrsty, allowed_stys)
        else:
            sty_info = load_semantic_types_by_code(mrsty, allowed_stys)
    else:
        sty_info = {}
    targets = set(sty_info) if sty_info else None

    syns, abbrevs, brands, icd, rxcui, pref, sabs = bcd.harvest(
        mrconso, targets,
        sab_filter=set(args.sabs.split(",")) if args.sabs else None,
        min_len=args.min_len, max_len=args.max_len,
        keep_abbrev=not args.no_abbrev,
        abbrev_ambiguity_max=args.abbrev_ambiguity_max,
        icd_synonyms=False,
        canonical_mode="preferred",
        kind_of={cui: info[0] for cui, info in sty_info.items()},
    )

    concepts = assemble(sty_info, syns, abbrevs, brands, icd, rxcui, pref, sabs,
                        args.min_synonyms)
    if args.max_concepts:
        concepts = concepts[:args.max_concepts]
    return concepts


# ==========================================================================
# CSV emission
# ==========================================================================
def _write_one(path: Path, cols: list[str], rows: list[dict]) -> int:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        n = 0
        for r in rows:
            w.writerow([_cell(r, c) for c in cols])
            n += 1
            if n % 5000 == 0:
                f.flush()
    return n


def _cell(row: dict, col: str) -> str:
    v = row.get(col, "")
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        return SEP.join(str(x) for x in v)
    return v


def write_partition(out_dir: Path, stem: str, rows: list[dict],
                    chunk_size: int) -> list[Path]:
    cols = COLS_BY_STEM[stem]
    if chunk_size and len(rows) > chunk_size:
        files = []
        for i in range(0, len(rows), chunk_size):
            idx = i // chunk_size + 1
            p = out_dir / f"{stem}-{idx:04d}.csv"
            _write_one(p, cols, rows[i:i + chunk_size])
            files.append(p)
        return files
    p = out_dir / f"{stem}.csv"
    _write_one(p, cols, rows)
    return [p]


def write_synonym_map(out_dir: Path, concepts: list[dict],
                      chunk_size: int) -> list[Path]:
    """Denormalize every surface form into one row (the matching workhorse)."""
    rows = []
    for c in concepts:
        base = {
            "cui": c["cui"], "canonical": c["canonical"],
            "category": c["category"], "kind": c.get("kind") or "",
            "rxcui": c.get("rxcui") or "", "icd10": c.get("icd10") or "",
        }
        rows.append({**base, "term": c["canonical"], "term_type": "canonical"})
        for s in c.get("synonyms", ()):
            rows.append({**base, "term": s, "term_type": "synonym"})
        for a in c.get("abbreviations", ()):
            rows.append({**base, "term": a, "term_type": "abbreviation"})
        for b in c.get("brands", ()):
            rows.append({**base, "term": b, "term_type": "brand"})

    if chunk_size and len(rows) > chunk_size:
        files = []
        for i in range(0, len(rows), chunk_size):
            idx = i // chunk_size + 1
            p = out_dir / f"synonym-map-{idx:04d}.csv"
            _write_one(p, SYNONYM_MAP_COLS, rows[i:i + chunk_size])
            files.append(p)
        return files
    p = out_dir / "synonym-map.csv"
    _write_one(p, SYNONYM_MAP_COLS, rows)
    return [p]


def write_checkpoint(out_dir: Path, concepts: list[dict]) -> Path:
    p = out_dir / "concepts.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for c in concepts:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return p


def write_manifest(out_dir: Path, args, files_by_stem: dict, n_concepts: int,
                   n_synmap_rows: int) -> None:
    manifest = {
        "source": "UMLS Metathesaurus",
        "release": args.release,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "meta_dir": args.meta_dir,
        "concept_count": n_concepts,
        "synonym_map_rows": n_synmap_rows,
        "min_synonyms": args.min_synonyms,
        "max_concepts": args.max_concepts or None,
        "chunk_size": args.chunk_size or None,
        "files": {},
    }
    for stem in sorted(files_by_stem):
        paths = files_by_stem[stem]
        manifest["files"][stem] = {
            "columns": COLS_BY_STEM.get(stem, SYNONYM_MAP_COLS),
            "chunks": [p.name for p in paths],
            "rows": sum(1 for _ in open(paths[0], encoding="utf-8")) - 1
                    if len(paths) == 1 else None,  # exact only for single file
        }
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


# ==========================================================================
def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--meta-dir", default=DEFAULT_META,
                   help="directory holding MRCONSO.RRF / MRSTY.RRF")
    p.add_argument("--out-dir", default=str(_HERE.parent / "data" / "master"))
    p.add_argument("--release", default="2026AA")
    p.add_argument("--csv-only", action="store_true",
                   help="skip the MRCONSO scan; re-emit CSVs from concepts.jsonl")
    p.add_argument("--sabs", default=",".join(bcd.SAB_PREFERENCE),
                   help="comma-separated source vocabularies to keep")
    p.add_argument("--min-synonyms", type=int, default=1,
                   help="min surface forms (syn+abbr+brand) per concept; 1 = max breadth")
    p.add_argument("--max-concepts", type=int, default=0,
                   help="cap total concepts (0 = no cap)")
    p.add_argument("--chunk-size", type=int, default=0,
                   help="split partitions larger than this into N-row chunks (0 = off)")
    p.add_argument("--min-len", type=int, default=2)
    p.add_argument("--max-len", type=int, default=60)
    p.add_argument("--no-abbrev", action="store_true")
    p.add_argument("--abbrev-ambiguity-max", type=int, default=3)
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt = out_dir / "concepts.jsonl"

    if args.csv_only:
        if not ckpt.exists():
            sys.exit(f"--csv-only needs {ckpt} (run a full pass first)")
        log(f"loading checkpoint {ckpt}")
        concepts = [json.loads(line) for line in open(ckpt, encoding="utf-8") if line.strip()]
    else:
        t0 = time.time()
        log(f"extracting from {args.meta_dir} ...")
        concepts = run_extraction(args)
        log(f"assembled {len(concepts):,} concepts in {time.time()-t0:.0f}s")
        write_checkpoint(out_dir, concepts)
        log(f"checkpoint -> {ckpt}")

    # ── partition by kind and emit master CSVs ───────────────────────────────
    buckets: dict[str, list[dict]] = defaultdict(list)
    for c in concepts:
        stem = FILE_BY_KIND.get(c.get("kind") or "", None)
        if stem is None:
            continue  # out-of-scope kind (shouldn't happen with STY_MAP scope)
        buckets[stem].append(c)

    files_by_stem: dict[str, list[Path]] = {}
    for stem in sorted(buckets):
        rows = sorted(buckets[stem], key=lambda c: (c["category"], c["canonical"]))
        paths = write_partition(out_dir, stem, rows, args.chunk_size)
        files_by_stem[stem] = paths
        log(f"{paths[0].name}: {len(rows):,} concepts"
            + (f" ({len(paths)} chunks)" if len(paths) > 1 else ""))

    # ── synonym map (all surface forms) ──────────────────────────────────────
    sm_paths = write_synonym_map(out_dir, concepts, args.chunk_size)
    n_synmap_rows = sum(len(c.get("synonyms", ())) + len(c.get("abbreviations", ()))
                        + len(c.get("brands", ())) + 1 for c in concepts)
    log(f"{sm_paths[0].name}: {n_synmap_rows:,} surface forms"
        + (f" ({len(sm_paths)} chunks)" if len(sm_paths) > 1 else ""))

    write_manifest(out_dir, args, files_by_stem, len(concepts), n_synmap_rows)
    log(f"manifest -> {out_dir / 'manifest.json'}")
    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
