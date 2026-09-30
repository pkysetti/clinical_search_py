#!/usr/bin/env python3
"""
similar.py
==========

Given a UMLS CUI (or a surface term that resolves to one via ``synonym-map.csv``),
return **clinically similar concepts** using the two UMLS structure sources:

  * MRHIER  — hierarchy: ancestors (broader / class) + descendants (subtypes)
  * MRREL   — relationships: 1-hop edges, ranked by REL/RELA (RN/RB = narrower/broader)

This is the "similar medications / diagnoses from a patient record" step that sits
on top of the master CSVs produced by ``build_master_csv.py``:

    patient record text
        -> synonym-map.csv  (surface form -> CUI + rxcui/icd10)   [normalise]
        -> similar.py       (CUI -> similar CUIs via MRHIER+MRREL) [expand]

Data sources (real 2026AA Full Release, verified on this machine):
  * META/MRCONSO.RRF          — names (for resolving a term's CUI + enriching)
  * 2026aa-1-meta.nlm (zip)   — MRHIER.RRF.{aa..ae}.gz
  * 2026aa-2-meta.nlm (zip)   — MRREL.RRF.{aa..ae}.gz

Every result is cached to ``data/master/similar/<CUI>.json`` so a repeat query for
the same CUI is instant. Standard library only.

USAGE
-----
    # by CUI
    python UMLS_MASTER\\similar.py --cui C0027051

    # by surface term (resolved through synonym-map.csv)
    python UMLS_MASTER\\similar.py --term "heart attack"

    # more results, force re-scan
    python UMLS_MASTER\\similar.py --cui C0004057 --max 40 --no-cache
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import zipfile
from pathlib import Path

# ── fixed data locations (verified present, 2026-09-30) ─────────────────────
BASE = Path(r"C:\Inference\umls-2026AA-full\2026AA-full")
HIER_ZIP = BASE / "2026aa-1-meta.nlm"
HIER_PARTS = [f"2026AA/META/MRHIER.RRF.{p}.gz" for p in ("aa", "ab", "ac", "ad", "ae")]
REL_ZIP = BASE / "2026aa-2-meta.nlm"
REL_PARTS = [f"2026AA/META/MRREL.RRF.{p}.gz" for p in ("aa", "ab", "ac", "ad", "ae")]

MASTER_DIR = Path(__file__).resolve().parent.parent / "data" / "master"
SYNONYM_MAP = MASTER_DIR / "synonym-map.csv"
CONCEPTS_JSONL = MASTER_DIR / "concepts.jsonl"
CACHE_DIR = MASTER_DIR / "similar"

# MRHIER: CUI|AUI|CXN|PAUI|SAB|RELA|PTR|HCD|CVF  (CUI=0, AUI=1, PTR=6)
# MRREL : CUI1|AUI1|SCUI1|REL|CUI2|AUI2|SCUI2|RELA (CUI1=0, REL=3, CUI2=4, RELA=7)

# Relationship codes that mean "narrower/broader" — strongest similarity signal.
STRONG_REL = {"RN", "RB"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def iter_bytes(zip_path: Path, members: list[str]):
    """Yield raw bytes lines from gzipped RRF parts inside a zip archive."""
    z = zipfile.ZipFile(zip_path)
    for m in members:
        with z.open(m) as raw:
            g = gzip.GzipFile(fileobj=raw)
            yield from g


# ── name resolution / enrichment ─────────────────────────────────────────────
def load_names() -> dict[str, str]:
    """cui -> canonical from the master checkpoint (fast, in-memory)."""
    names: dict[str, str] = {}
    if CONCEPTS_JSONL.exists():
        with open(CONCEPTS_JSONL, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                c = json.loads(line)
                names[c["cui"]] = c["canonical"]
    return names


def resolve_term(term: str) -> tuple[str | None, list[dict]]:
    """Resolve a surface term to CUI(s) via synonym-map.csv.

    Returns (best_cui, all_candidate_rows). Prefers canonical > synonym >
    abbreviation > brand, then the richest concept.
    """
    import csv
    type_rank = {"canonical": 0, "synonym": 1, "abbreviation": 2, "brand": 3}
    key = term.strip().lower()
    best = None
    cands = []
    with open(SYNONYM_MAP, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["term"].lower() == key:
                cands.append(row)
                r = (type_rank.get(row["term_type"], 9), row["cui"])
                if best is None or r < best[0]:
                    best = (r, row)
    if not cands:
        return None, []
    return best[1]["cui"], cands


# ── hierarchy scan (MRHIER) ──────────────────────────────────────────────────
def scan_hierarchy(cui: str) -> dict:
    """Two passes over MRHIER: (1) X's AUIs + ancestor paths, (2) descendants +
    ancestor-AUI->CUI resolution."""
    cui_b = cui.encode()

    # pass 1: rows where CUI == X
    x_auis: set[str] = set()
    ancestor_auis: set[str] = set()
    n = 0
    for raw in iter_bytes(HIER_ZIP, HIER_PARTS):
        n += 1
        if raw[:8] != cui_b:
            continue
        cols = raw.rstrip(b"\n").split(b"|")
        if len(cols) < 7:
            continue
        if cols[1]:
            x_auis.add(cols[1].decode())
        if cols[6]:
            ancestor_auis.update(a.decode() for a in cols[6].split(b".") if a)

    # pass 2: descendants (PTR contains an X-AUI) + resolve ancestor AUIs->CUI
    anc_b = {a.encode() for a in ancestor_auis}
    x_auis_b = {a.encode() for a in x_auis}
    descendant_cuis: dict[str, str] = {}
    aui_to_cui: dict[str, str] = {}
    if x_auis_b:
        # AUI appears as a path component: bounded by start/dot/pipe
        comp_pat = re.compile(
            b"(?:^|[|\\.])(" + b"|".join(sorted(x_auis_b, key=len, reverse=True)) + b")(?:[|\\.]|$)")
    if anc_b:
        own_atom_pat = re.compile(
            b"\\|(" + b"|".join(sorted(anc_b, key=len, reverse=True)) + b")\\|")

    n = 0
    for raw in iter_bytes(HIER_ZIP, HIER_PARTS):
        n += 1
        if x_auis_b and comp_pat.search(raw):
            c = raw[:8].decode()
            if c != cui:
                descendant_cuis[c] = "descendant"
        if anc_b:
            mm = own_atom_pat.search(raw)
            if mm and raw.split(b"|", 2)[1].rstrip(b"\n") == mm.group(1):
                aui_to_cui.setdefault(mm.group(1).decode(), raw[:8].decode())

    ancestors = {aui_to_cui[a]: "ancestor" for a in ancestor_auis if a in aui_to_cui}
    return {"ancestors": ancestors, "descendants": descendant_cuis,
            "x_auis": sorted(x_auis)}


# ── relationship scan (MRREL) ────────────────────────────────────────────────
def scan_rel(cui: str) -> list[dict]:
    """One pass over MRREL: 1-hop edges where X is an endpoint."""
    cui_b = cui.encode()
    pat = re.compile(b"(" + cui_b + b")\\|")
    edges: dict[frozenset, dict] = {}
    n = 0
    for raw in iter_bytes(REL_ZIP, REL_PARTS):
        n += 1
        if not pat.search(raw):
            continue
        cols = raw.rstrip(b"\n").split(b"|")
        if len(cols) < 8:
            continue
        c1, rel, c2, rela = (cols[0].decode(), cols[3].decode(),
                             cols[4].decode(), cols[7].decode())
        if c1 != cui and c2 != cui:
            continue
        other = c2 if c1 == cui else c1
        if not other or other == cui:
            continue
        key = frozenset((cui, other))
        cur = edges.get(key)
        # keep the strongest relationship for a pair (RN/RB preferred)
        if cur is None or (rel in STRONG_REL and cur["rel"] not in STRONG_REL):
            edges[key] = {"cui": other, "rel": rel, "rela": rela}

    return list(edges.values())


# ── assemble + rank ──────────────────────────────────────────────────────────
def build_similar(cui: str, names: dict[str, str], max_results: int) -> dict:
    hier = scan_hierarchy(cui)
    rels = scan_rel(cui)

    def item(c: str, relation: str, rel=None, rela=None) -> dict | None:
        if not c or c == cui:
            return None
        return {"cui": c, "name": names.get(c, ""), "relation": relation,
                "rel": rel, "rela": rela}

    desc = [i for i in (item(c, "descendant") for c in hier["descendants"]) if i]
    anc = [i for i in (item(c, "ancestor") for c in hier["ancestors"]) if i]
    mrrel = [i for i in (item(e["cui"], "mrrel", e["rel"], e.get("rela")) for e in rels) if i]

    # Within each bucket: named (master-set) concepts first, then by name.
    desc.sort(key=lambda x: (0 if x["name"] else 1, x["name"] or x["cui"]))
    anc.sort(key=lambda x: (0 if x["name"] else 1, x["name"] or x["cui"]))
    mrrel.sort(key=lambda x: (0 if x["name"] else 1,
                              0 if x["rel"] in STRONG_REL else 1,
                              x["name"] or x["cui"]))

    # Round-robin interleave (subtypes -> related -> broader) so the list is a
    # balanced, clinically useful mix rather than 20-of-a-kind.
    buckets = [desc, mrrel, anc]
    pos = {id(b): 0 for b in buckets}
    result: list[dict] = []
    seen: set[str] = set()
    while len(result) < max_results and any(pos[id(b)] < len(b) for b in buckets):
        for b in buckets:
            i = pos[id(b)]
            if i < len(b):
                it = b[i]
                pos[id(b)] = i + 1
                if it["cui"] not in seen:
                    seen.add(it["cui"])
                    result.append(it)
                if len(result) >= max_results:
                    break

    return {
        "cui": cui,
        "canonical": names.get(cui, ""),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {"ancestor": len(anc), "descendant": len(desc), "mrrel": len(mrrel)},
        "similar": result[:max_results],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--cui", help="UMLS CUI, e.g. C0027051")
    g.add_argument("--term", help="surface term, resolved via synonym-map.csv")
    p.add_argument("--max", type=int, default=25, help="max similar concepts to return")
    p.add_argument("--no-cache", action="store_true", help="force a fresh scan")
    args = p.parse_args()

    if args.cui:
        cui = args.cui.upper()
        cands = []
    else:
        cui, cands = resolve_term(args.term)
        if not cui:
            log(f"term {args.term!r} not found in synonym-map.csv")
            return 1
        log(f"resolved {args.term!r} -> {cui} ({len(cands)} candidate row(s))")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{cui}.json"
    if cache.exists() and not args.no_cache:
        log(f"cache hit -> {cache}")
        result = json.loads(cache.read_text(encoding="utf-8"))
    else:
        names = load_names()
        t0 = time.time()
        log(f"scanning MRHIER + MRREL for {cui} (first run, ~minutes) ...")
        result = build_similar(cui, names, args.max)
        cache.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        log(f"done in {time.time()-t0:.0f}s; cached -> {cache}")

    # human-readable table to stderr
    print(f"\nSimilar concepts for {result['canonical'] or args.term} ({cui})", file=sys.stderr)
    print(f"  ancestors={result['counts']['ancestor']}  "
          f"descendants={result['counts']['descendant']}  "
          f"mrrel={result['counts']['mrrel']}\n", file=sys.stderr)
    for i, s in enumerate(result["similar"], 1):
        rel = f" {s['rel']}/{s.get('rela','')}" if s["relation"] == "mrrel" else ""
        print(f"  {i:2d}. [{s['relation']:>10}]{rel:<14} {s['name'] or '(not in master set)'}  {s['cui']}",
              file=sys.stderr)

    # machine-readable to stdout
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
