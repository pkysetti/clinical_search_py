#!/usr/bin/env python3
"""
extract_heart_attack_graph.py
=============================

Extract everything in UMLS 2026AA related to heart-attack (myocardial
infarction) care — diagnosis, findings, medications, procedures, allergies —
plus the relationships between those concepts, into a single JSON graph:

    {
      "meta": {...},
      "concepts": [ {cui, canonical, synonyms, abbreviations, icd10,
                     semantic_types, category, sources} ],
      "edges":    [ {type: "hierarchy"|"rel", from, to, rel, rela} ]
    }

Data sources (all under C:\\Inference\\umls-2026AA-full\\2026AA-full):
  * META/MRCONSO.RRF          — concepts + names (2.3 GB, streamed)
  * META/MRSTY.RRF            — semantic types (73 MB, loaded)
  * 2026aa-1-meta.nlm (zip)   — MRHIER.RRF.{aa..ae}.gz (streamed from zip)
  * 2026aa-2-meta.nlm (zip)   — MRREL.RRF.{aa..ae}.gz  (streamed from zip)

Column layouts verified against the real 2026AA files on 2026-09-29:
  MRCONSO: CUI LAT TS LUI STT SUI ISPREF AUI SAUI SCUI SDUI SAB TTY CODE STR SRL SUPPRESS CVF
  MRSTY:   CUI TUI STN STY ATUI CVF
  MRHIER:  CUI AUI CXN PAUI SAB RELA PTR HCD CVF
  MRREL:   CUI1 AUI1 SCUI1 REL CUI2 AUI2 SCUI2 RELA ...

Every pass is checkpointed to heart_attack_state.json, so an interrupted run
resumes where it left off. Standard library only.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

BASE = Path(r"C:\Inference\umls-2026AA-full\2026AA-full")
META = BASE / "2026AA" / "META"
MRCONSO = META / "MRCONSO.RRF"
MRSTY = META / "MRSTY.RRF"
HIER_ZIP = BASE / "2026aa-1-meta.nlm"
HIER_PARTS = [f"2026AA/META/MRHIER.RRF.{p}.gz" for p in ("aa", "ab", "ac", "ad", "ae")]
REL_ZIP = BASE / "2026aa-2-meta.nlm"
REL_PARTS = [f"2026AA/META/MRREL.RRF.{p}.gz" for p in ("aa", "ab", "ac", "ad", "ae")]

OUT_DIR = Path(r"C:\github\clinical_search_py\data\filter")
OUT_JSON = OUT_DIR / "heart_attack_graph.json"
STATE = OUT_DIR / "heart_attack_state.json"

CORE_CUI = "C0027051"  # myocardial infarction (verified in 2026AA MRCONSO)

# ── Seed terms, exact-match against MRCONSO STR (lowercase) ─────────────────
SEEDS = {
    "diagnosis": [
        "acute coronary syndrome", "chest pain", "st elevation", "troponin",
        "coronary artery disease", "atherosclerosis", "cardiac arrest",
        "myocardial ischemia",
    ],
    "medication": [
        "aspirin", "heparin", "enoxaparin", "clopidogrel", "ticagrelor",
        "prasugrel", "atorvastatin", "rosuvastatin", "metoprolol",
        "lisinopril", "nitroglycerin", "morphine", "alteplase",
    ],
    "procedure": [
        "percutaneous coronary intervention", "coronary angiography",
        "coronary artery bypass grafting", "coronary artery bypass",
        "coronary artery bypass graft", "thrombolysis",
        "cardiopulmonary resuscitation", "echocardiography",
    ],
    "allergy": [
        "drug hypersensitivity", "hypersensitivity",
        "heparin-induced thrombocytopenia", "drug allergy",
    ],
}

# Semantic types considered clinically relevant for 1-hop MRREL expansion.
CLINICAL_STY = {
    "Disease or Syndrome", "Sign or Symptom", "Finding", "Injury or Poisoning",
    "Pharmacologic Substance", "Biologic Function", "Pathologic Function",
    "Organ or Tissue Function", "Therapeutic or Preventive Procedure",
    "Diagnostic Procedure", "Anatomical Structure",
    "Body Part, Organ, or Organ Component", "Body Substance", "Cell",
    "Molecular Sequence", "Chemical View", "Neoplastic Process",
    "Congenital Abnormality", "Mental or Behavioral Dysfunction",
    "Medical Device", "Idea or Concept", "Organic Chemical",
    "Amino Acid, Peptide, or Protein", "Biologically Active Substance",
}

# TUI code -> semantic type name.  Verified against the real 2026AA MRSTY on
# 2026-09-29 (this release ships only codes, names are empty) using known
# concept anchors (MI=T047 disease, aspirin/heparin/...=T121 drug, chest
# pain=T184 symptom, CPR/PCI=T061 therapeutic procedure, echo/angiography=
# T060 diagnostic procedure, troponin/alteplase=T116 protein) plus the
# official list shipped in quickumls.constants.
TUI_NAMES = {
    "T033": "Finding",
    "T042": "Organ or Tissue Function",
    "T046": "Pathologic Function",
    "T047": "Disease or Syndrome",
    "T060": "Diagnostic Procedure",
    "T061": "Therapeutic or Preventive Procedure",
    "T109": "Organic Chemical",
    "T114": "Nucleic Acid, Nucleoside, or Nucleotide",
    "T116": "Amino Acid, Peptide, or Protein",
    "T121": "Pharmacologic Substance",
    "T123": "Biologically Active Substance",
    "T184": "Sign or Symptom",
}

MAX_CONCEPTS = 220  # hard cap on total concepts in the graph

SAB_RANK = {"MTH": 0, "SNOMEDCT_US": 1, "MSH": 2}


# ── helpers ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict:
    if STATE.exists():
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def iter_bytes(zip_path: Path, members: list[str]):
    """Yield raw bytes lines from gzipped RRF parts inside a zip archive."""
    z = zipfile.ZipFile(zip_path)
    for m in members:
        with z.open(m) as raw:
            g = gzip.GzipFile(fileobj=raw)
            yield from g


# ── pass 1: MRCONSO — core cluster + seed candidates ────────────────────────

def pass1_mrconso_seeds(state: dict) -> None:
    if state.get("pass1_done"):
        log("pass1: skipped (checkpoint)")
        return
    log(f"pass1: scanning {MRCONSO.stat().st_size/1e9:.2f} GB MRCONSO for core + seeds...")
    t0 = time.time()

    seed_terms: set[str] = set()
    for terms in SEEDS.values():
        seed_terms.update(t.lower() for t in terms)

    pat = re.compile(
        b"|" .join(re.escape(t.encode("utf-8")) for t in sorted(seed_terms, key=len, reverse=True))
    )
    core_b = CORE_CUI.encode()

    core_rows: list[list[str]] = []
    candidates: dict[str, list[dict]] = {}
    n = 0
    with open(MRCONSO, "rb") as f:
        for raw in f:
            n += 1
            if n % 2_000_000 == 0:
                log(f"pass1: {n/1e6:.0f}M rows ({time.time()-t0:.0f}s)")
            if raw[:8] == core_b:
                core_rows.append(raw.decode("utf-8", "replace").rstrip("\n").split("|"))
                continue
            if pat.search(raw):
                cols = raw.decode("utf-8", "replace").rstrip("\n").split("|")
                if len(cols) < 15:
                    continue
                str_field = cols[14].lower()
                for term in seed_terms:
                    if str_field == term:
                        bucket = candidates.setdefault(term, [])
                        if len(bucket) < 30:
                            bucket.append({
                                "cui": cols[0], "str": cols[14], "sab": cols[11],
                                "tty": cols[12], "ts": cols[2], "stt": cols[4],
                                "ispref": cols[6], "code": cols[13],
                            })
    log(f"pass1: {n/1e6:.1f}M rows in {time.time()-t0:.0f}s; core rows={len(core_rows)}, "
        f"seed terms hit={len(candidates)}/{len(seed_terms)}")

    resolved: dict[str, list[str]] = {}
    for term, rows in candidates.items():
        def rank(r):
            return (r["ts"] == "P", r["stt"] == "PF", r["ispref"] == "Y",
                    SAB_RANK.get(r["sab"], 9))
        rows.sort(key=rank, reverse=True)
        cuis = []
        for r in rows:
            if r["cui"] not in cuis:
                cuis.append(r["cui"])
            if len(cuis) >= 2:
                break
        resolved[term] = cuis

    state["core_rows"] = core_rows
    state["seed_resolved"] = resolved
    state["pass1_done"] = True
    save_state(state)
    missing = [t for t in sorted(seed_terms) if t not in candidates]
    log(f"pass1: resolved {len(resolved)} terms; no exact STR match for: {missing}")


# ── record collection (used by pass 2 and pass 6) ───────────────────────────

def _collect_records(cuis: set[str]) -> dict[str, dict]:
    """One MRCONSO pass collecting full records + AUIs for `cuis`.

    CUI is always field 0 of MRCONSO, so a raw[:8] set lookup replaces the
    (slow) large-alternation regex — O(1) per row.
    """
    t0 = time.time()
    cuis_b = {c.encode() for c in cuis}
    recs: dict[str, dict] = {
        c: {"synonyms": set(), "abbreviations": set(), "icd10": None,
            "sources": set(), "auis": [], "preferred": []}
        for c in cuis
    }
    n_match = 0
    with open(MRCONSO, "rb") as f:
        for raw in f:
            if raw[:8] not in cuis_b:
                continue
            cols = raw.decode("utf-8", "replace").rstrip("\n").split("|")
            if len(cols) < 15 or cols[0] not in recs:
                continue
            n_match += 1
            r = recs[cols[0]]
            s = cols[14].strip()
            tty, sab = cols[12], cols[11]
            if cols[1] != "ENG" or (len(cols) > 16 and cols[16] not in ("N", "")):
                continue
            if not s or len(s) < 2 or len(s) > 80:
                continue
            if tty in ("AB", "ACR", "AA", "AS", "MTH_AB"):
                r["abbreviations"].add(s)
            elif tty in ("SCD", "SBD", "PSN", "TMSY", "BPCK", "GPCK"):
                pass  # dose-level product strings: never names
            elif sab in ("ICD10CM", "ICD9CM", "CPT", "HCPCS"):
                if cols[13] and not r["icd10"]:
                    r["icd10"] = cols[13][:3]
            else:
                if tty == "BN" and sab == "RXNORM":
                    pass  # brands kept out of synonyms for search quality
                else:
                    r["synonyms"].add(s)
                    if cols[2] == "P" and cols[4] == "PF" and cols[6] == "Y":
                        r["preferred"].append((SAB_RANK.get(sab, 9), s))
            r["sources"].add(sab)
            if cols[7]:
                r["auis"].append(cols[7])
    log(f"_collect_records: {len(recs)} CUIs, {n_match} rows in {time.time()-t0:.0f}s")
    return recs


def pass2_records(state: dict) -> None:
    if state.get("pass2_done"):
        log("pass2: skipped (checkpoint)")
        return
    cuis = {CORE_CUI}
    for cl in state["seed_resolved"].values():
        if cl:
            cuis.add(cl[0])
    log(f"pass2: collecting full records for {len(cuis)} seed CUIs...")
    recs = _collect_records(cuis)
    state["records"] = {
        c: {k: (sorted(v) if isinstance(v, set) else v) for k, v in r.items()}
        for c, r in recs.items()
    }
    state["pass2_done"] = True
    save_state(state)


# ── pass 3: MRSTY — semantic types ──────────────────────────────────────────

def pass3_mrsty(state: dict) -> None:
    if state.get("pass3_done"):
        log("pass3: skipped (checkpoint)")
        return
    t0 = time.time()
    sty: dict[str, list[str]] = {}
    n = 0
    with open(MRSTY, "rb") as f:
        for raw in f:
            cols = raw.rstrip(b"\n").split(b"|")
            if len(cols) < 2:
                continue
            n += 1
            cui = cols[0].decode("ascii", "replace")
            tui = cols[1].decode("ascii", "replace")
            name = TUI_NAMES.get(tui, tui)  # unknown codes kept as raw code
            if name not in sty.setdefault(cui, []):
                sty[cui].append(name)
    log(f"pass3: MRSTY {n/1e6:.1f}M rows -> {len(sty)} CUIs in {time.time()-t0:.0f}s")
    state["sty"] = sty
    state["pass3_done"] = True
    save_state(state)


# ── pass 4: MRHIER — ancestors, descendants, depth ──────────────────────────

def pass4_mrhier(state: dict) -> None:
    if state.get("pass4_done"):
        log("pass4: skipped (checkpoint)")
        return
    t0 = time.time()
    core_auis = set(state["records"].get(CORE_CUI, {}).get("auis", []))

    aui_pat = None
    if core_auis:
        alts = b"|".join(sorted((a.encode() for a in core_auis), key=len, reverse=True))
        aui_pat = re.compile(b"(?:^|[|\\.])(" + alts + b")(?:[|\\.]|$)")

    core_b = CORE_CUI.encode()
    mi_chain: list[str] = []          # longest root->MI AUI path, in order
    ancestor_auis: set[str] = set()
    descendant_cuis: dict[str, str] = {}
    n = 0
    for raw in iter_bytes(HIER_ZIP, HIER_PARTS):
        n += 1
        if n % 5_000_000 == 0:
            log(f"pass4: {n/1e6:.0f}M rows ({time.time()-t0:.0f}s)")
        cui_b = raw[:8]
        if cui_b == core_b:
            cols = raw.rstrip(b"\n").split(b"|")
            if len(cols) > 6 and cols[6]:
                chain = [a.decode() for a in cols[6].split(b".") if a]
                if len(chain) > len(mi_chain):
                    mi_chain = chain
                ancestor_auis.update(chain)
            continue
        if aui_pat and aui_pat.search(raw):
            cui = cui_b.decode()
            if cui != CORE_CUI:
                descendant_cuis[cui] = CORE_CUI

    log(f"pass4: {n/1e6:.0f}M rows in {time.time()-t0:.0f}s; "
        f"descendants={len(descendant_cuis)}, ancestor auis={len(ancestor_auis)}")

    # Second light pass: map ancestor AUIs -> CUI (row's own atom = that AUI).
    aui_to_cui: dict[str, str] = {}
    if ancestor_auis:
        apat = re.compile(
            b"\\|(" + b"|".join(sorted((a.encode() for a in ancestor_auis), key=len, reverse=True)) + b")\\|"
        )
        t1 = time.time()
        for raw in iter_bytes(HIER_ZIP, HIER_PARTS):
            mm = apat.search(raw)
            if not mm:
                continue
            aui = mm.group(1).decode()
            # only accept when the AUI is this row's own atom (field 1)
            if raw.split(b"|", 2)[1].rstrip(b"\n") == aui.encode():
                aui_to_cui.setdefault(aui, raw[:8].decode())
        log(f"pass4b: ancestor AUI resolution in {time.time()-t1:.0f}s ({len(aui_to_cui)} mapped)")

    state["hierarchy"] = {
        "descendants": descendant_cuis,
        "mi_chain": mi_chain,
        "ancestor_auis": sorted(ancestor_auis),
        "aui_to_cui": aui_to_cui,
    }
    state["pass4_done"] = True
    save_state(state)


# ── pass 5: MRREL — one-hop relationships ───────────────────────────────────

def pass5_mrrel(state: dict) -> None:
    if state.get("pass5_done"):
        log("pass5: skipped (checkpoint)")
        return
    t0 = time.time()
    known = set(state["records"].keys())
    desc = set(state["hierarchy"]["descendants"].keys())
    sty = state["sty"]

    def clinical(cui: str) -> bool:
        return any(t in CLINICAL_STY for t in sty.get(cui, []))

    pat = re.compile(
        b"(" + b"|".join(sorted((c.encode() for c in known), key=len, reverse=True)) + b")\\|"
    )
    edges: list[dict] = []
    new_cuis: set[str] = set()
    n = 0
    for raw in iter_bytes(REL_ZIP, REL_PARTS):
        n += 1
        if n % 5_000_000 == 0:
            log(f"pass5: {n/1e6:.0f}M rows ({time.time()-t0:.0f}s)")
        if not pat.search(raw):
            continue
        cols = raw.rstrip(b"\n").split(b"|")
        if len(cols) < 8:
            continue
        cui1, rel, cui2, rela = (cols[0].decode(), cols[3].decode(),
                                 cols[4].decode(), cols[7].decode())
        if cui1 not in known and cui2 not in known:
            continue
        other = cui2 if cui1 in known else cui1
        if other not in known and other not in desc:
            if clinical(other) and len(known) + len(new_cuis) < MAX_CONCEPTS:
                new_cuis.add(other)
        edges.append({"type": "rel", "from": cui1, "to": cui2, "rel": rel, "rela": rela})

    log(f"pass5: {n/1e6:.0f}M rows in {time.time()-t0:.0f}s; "
        f"edges={len(edges)}, new CUIs via MRREL={len(new_cuis)}")
    state["rel_edges"] = edges
    state["mrrel_new"] = sorted(new_cuis)
    state["pass5_done"] = True
    save_state(state)


# ── pass 6: records for MRREL-discovered CUIs + assemble ────────────────────

CATEGORY_BY_STY = [
    (("Pharmacologic Substance", "Organic Chemical", "Biologically Active Substance",
      "Amino Acid, Peptide, or Protein", "Chemical View"), "medication"),
    (("Therapeutic or Preventive Procedure", "Diagnostic Procedure"), "procedure"),
    (("Anatomical Structure", "Body Part, Organ, or Organ Component",
      "Body Substance", "Cell"), "anatomy"),
    (("Sign or Symptom", "Finding"), "finding"),
    (("Disease or Syndrome", "Neoplastic Process", "Congenital Abnormality",
      "Mental or Behavioral Dysfunction", "Injury or Poisoning",
      "Pathologic Function", "Organ or Tissue Function"), "diagnosis"),
]


def categorize(cui: str, state: dict) -> str:
    types = set(state["sty"].get(cui, []))
    rec = state["records"].get(cui, {})
    text = " ".join([rec.get("canonical", "")] + list(rec.get("synonyms", [])[:20])).lower()
    if "allerg" in text or "hypersensitiv" in text:
        return "allergy"
    for sty_set, cat in CATEGORY_BY_STY:
        if types & set(sty_set):
            return cat
    return "other"


def canonical_for(cui: str, state: dict) -> str:
    rec = state["records"].get(cui, {})
    pref = rec.get("preferred") or []
    if pref:
        pref.sort()
        return pref[0][1]
    syns = rec.get("synonyms", [])
    if syns:
        return min(syns, key=len)
    return cui


def pass6_assemble(state: dict) -> None:
    if state.get("pass6_done"):
        log("pass6: skipped (checkpoint)")
        return

    h = state["hierarchy"]
    a2c = h["aui_to_cui"]

    # 1) Ancestor spine CUIs (root -> ... -> MI) become concepts.
    chain_cuis = {a2c[a] for a in h.get("mi_chain", []) if a in a2c} - {CORE_CUI}

    # 2) Top MI subtypes from the descendant pool: prefer names that mention
    #    infarction/myocardial, then synonym richness.
    desc = h["descendants"]
    mrrel_new = [c for c in state["mrrel_new"] if c not in state["records"]]
    log(f"pass6: collecting records for {len(desc)} descendants + "
        f"{len(chain_cuis)} spine + {len(mrrel_new)} MRREL CUIs...")
    pool = set(desc.keys()) | chain_cuis | set(mrrel_new)
    recs = _collect_records(pool)

    def desc_score(c: str) -> tuple:
        r = recs.get(c, {})
        canon = ""
        pref = r.get("preferred") or []
        if pref:
            canon = sorted(pref)[0][1].lower()
        elif r.get("synonyms"):
            canon = min(r["synonyms"], key=len).lower()
        name_hit = 2 if ("infarct" in canon or "myocardial" in canon) else 0
        return (name_hit, len(r.get("synonyms", [])))

    top_desc = sorted((c for c in desc if c in recs and recs[c]["synonyms"]),
                      key=desc_score, reverse=True)[:15]

    extra = ([c for c in state["mrrel_new"] if c not in state["records"]]
             + [c for c in chain_cuis if c in recs]
             + top_desc)
    for c in extra:
        if c in recs and c not in state["records"]:
            state["records"][c] = {k: (sorted(v) if isinstance(v, set) else v)
                                   for k, v in recs[c].items()}
    log(f"pass6: concept set now {len(state['records'])} "
        f"(+{len(chain_cuis & set(recs))} spine, +{len(top_desc)} subtypes)")

    # Hierarchy edges: MI ancestry chain (root -> MI, ordered) + descendants.
    hier_edges: list[dict] = []
    h = state["hierarchy"]
    a2c = h["aui_to_cui"]
    have = set(state["records"].keys())

    chain_cuis: list[str] = []
    for a in h.get("mi_chain", []):
        c = a2c.get(a)
        if c and c != CORE_CUI and c not in chain_cuis:
            chain_cuis.append(c)
    prev = None
    for c in chain_cuis:
        if c not in have:
            continue
        if prev is not None:
            hier_edges.append({"type": "hierarchy", "from": prev, "to": c})
        prev = c
    if prev is not None and CORE_CUI in have:
        hier_edges.append({"type": "hierarchy", "from": prev, "to": CORE_CUI})

    for d, anc in h["descendants"].items():
        if d in have:
            hier_edges.append({"type": "hierarchy", "from": anc, "to": d})

    # Rel edges: keep both-endpoint-in-set, dedupe by unordered pair (best
    # rel wins), score for display ranking, cap the total.
    seed_set = {CORE_CUI} | {c for cl in state["seed_resolved"].values() for c in cl[:1]}
    best: dict[frozenset, dict] = {}
    for e in state["rel_edges"]:
        if e["from"] not in have or e["to"] not in have or e["from"] == e["to"]:
            continue
        key = frozenset((e["from"], e["to"]))
        cur = best.get(key)
        if cur is None or (e["rel"] in ("RN", "RB") and cur["rel"] not in ("RN", "RB")):
            best[key] = e

    def edge_score(e: dict) -> int:
        both_seed = e["from"] in seed_set and e["to"] in seed_set
        one_core = CORE_CUI in (e["from"], e["to"])
        if e["rel"] in ("RN", "RB"):
            return 3 if both_seed else (2 if one_core else 1)
        return 1 if both_seed else 0

    rel_edges = sorted(best.values(), key=edge_score, reverse=True)[:1500]
    for i, e in enumerate(rel_edges):
        e["score"] = edge_score(e)

    concepts = []
    for cui, rec in state["records"].items():
        canon = canonical_for(cui, state)
        if canon == cui:  # no usable name found — drop
            continue
        syns = [s for s in rec.get("synonyms", []) if s.lower() != canon.lower()]
        concepts.append({
            "cui": cui,
            "canonical": canon,
            "synonyms": sorted(syns)[:40],
            "abbreviations": sorted(rec.get("abbreviations", []))[:15],
            "icd10": rec.get("icd10"),
            "semantic_types": state["sty"].get(cui, [])[:6],
            "category": categorize(cui, state),
            "sources": sorted(rec.get("sources", []))[:12],
        })
    concepts.sort(key=lambda c: (c["category"], c["canonical"]))
    final_cuis = {c["cui"] for c in concepts}
    hier_edges = [e for e in hier_edges if e["from"] in final_cuis and e["to"] in final_cuis]
    rel_edges = [e for e in rel_edges if e["from"] in final_cuis and e["to"] in final_cuis]

    graph = {
        "meta": {
            "source": "UMLS 2026AA Full Release",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "core_cui": CORE_CUI,
            "concept_count": len(concepts),
            "edge_count": len(hier_edges) + len(rel_edges),
            "seed_terms": {t: state["seed_resolved"].get(t, [])
                           for t in sorted({x for ts in SEEDS.values() for x in ts})},
        },
        "concepts": concepts,
        "edges": hier_edges + rel_edges,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=1)
    log(f"pass6: wrote {OUT_JSON} — {len(concepts)} concepts, "
        f"{len(hier_edges)} hierarchy + {len(rel_edges)} rel edges")
    state["pass6_done"] = True
    save_state(state)


def main() -> int:
    state = load_state()
    log(f"state: {[k for k, v in state.items() if k.endswith('_done')] or 'fresh'}")
    pass1_mrconso_seeds(state)
    pass2_records(state)
    pass3_mrsty(state)
    pass4_mrhier(state)
    pass5_mrrel(state)
    pass6_assemble(state)
    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
