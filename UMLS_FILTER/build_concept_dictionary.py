#!/usr/bin/env python3
"""
build_concept_dictionary.py
===========================

Distil the UMLS Metathesaurus down to a compact JSON synonym dictionary for
medical semantic search, of the form:

    {
      "concepts": [
        {
          "cui": "C0027051",
          "canonical": "myocardial infarction",
          "synonyms": ["heart attack", "cardiac infarction", ...],
          "abbreviations": ["MI", "AMI"],
          "icd10": "I21",
          "category": "cardiovascular",
          "kind": "disorder",
          "specificity": "high",
          "sources": ["SNOMEDCT_US", "MSH", "ICD10CM"]
        },
        ...
      ]
    }

Reads the Rich Release Format (RRF) files straight off disk, streaming, so a
2.2 GB MRCONSO.RRF runs in a few hundred MB of RAM. No pandas required.

WHICH UMLS DOWNLOAD
-------------------
  * MRCONSO.RRF alone (492 MB zip)  -> synonyms + ICD-10 codes only.
      Gets you `canonical`, `synonyms`, `abbreviations`, `icd10`. No `kind`,
      weak `category`, no `specificity`.
  * UMLS Metathesaurus Level 0 Subset (1.9 GB zip)  -> MRCONSO + MRSTY + MRHIER.
      Everything except ICD-10-CM (license category 4, excluded from Level 0).
      Supply --icd10-map with NLM's free SNOMED CT -> ICD-10-CM map to fill in
      the `icd10` field.
  * UMLS Metathesaurus Full Subset (5.4 GB zip)  -> all of the above with
      ICD10CM present in MRCONSO. Simplest path; check your license terms
      before shipping anything derived from category 1-4 sources.

USAGE
-----
    # Full/Level 0 subset, whole clinical vocabulary, capped at 2000 concepts
    python build_concept_dictionary.py --meta-dir ./2026AA/META \
        --out concepts.json --max-concepts 2000

    # Scope to your own seed list (one term or CUI per line), pull descendants too
    python build_concept_dictionary.py --meta-dir ./2026AA/META \
        --seed-file ems_terms.txt --expand-descendants --out concepts.json

    # MRCONSO.RRF only
    python build_concept_dictionary.py --mrconso ./MRCONSO.RRF --out concepts.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict

# --------------------------------------------------------------------------
# RRF column layouts (2026AA). Order matters; RRF has no header row.
# --------------------------------------------------------------------------
MRCONSO_COLS = ("CUI LAT TS LUI STT SUI ISPREF AUI SAUI SCUI SDUI "
                "SAB TTY CODE STR SRL SUPPRESS CVF").split()
MRSTY_COLS = "CUI TUI STN STY ATUI CVF".split()
MRHIER_COLS = "CUI AUI CXN PAUI SAB RELA PTR HCD CVF".split()

C = {name: i for i, name in enumerate(MRCONSO_COLS)}
S = {name: i for i, name in enumerate(MRSTY_COLS)}
H = {name: i for i, name in enumerate(MRHIER_COLS)}

# --------------------------------------------------------------------------
# Source vocabularies to trust, in canonical-name preference order.
# Trim this list to shrink the output and raise precision.
# --------------------------------------------------------------------------
SAB_PREFERENCE = [
    "MTH",           # UMLS Metathesaurus-preferred name
    "SNOMEDCT_US",   # richest clinical synonymy
    "MSH",           # MeSH: clean canonical names, good entry terms
    "NCI",
    "RXNORM",        # drugs
    "LNC",           # labs
    "ICD10CM",
    "ICD9CM",
    "HPO",           # phenotype
    "MEDLINEPLUS",   # lay/consumer wording - valuable for user-facing search
    "MTHSPL",
    "CHV",           # consumer health vocabulary: "heart attack" lives here
    "OMIM",
]
SAB_RANK = {sab: i for i, sab in enumerate(SAB_PREFERENCE)}

# Term types that are real names. Everything else is dropped.
GOOD_TTY = {
    "PT", "PN", "FN", "SY", "MH", "EN", "ET", "MTH_PT", "MTH_SY", "MTH_PN",
    "PEP", "SCN", "IN", "BN", "CD", "HT", "LLT", "PTGB", "SB", "DN",
}
# Term types that are abbreviations/acronyms - kept, but segregated.
ABBREV_TTY = {"AB", "ACR", "AA", "MTH_AB", "AS", "AC"}

# Billing/claims classifications: mined for codes, not for synonym text.
CLASSIFICATION_SABS = {"ICD10CM", "ICD10", "ICD9CM", "ICD10PCS", "CPT", "HCPCS"}

# --------------------------------------------------------------------------
# Drugs need their own rules. RxNorm models a drug at several levels, and
# only the top two are useful as search synonyms:
#     IN   ingredient            "morphine sulfate"        <- want
#     PIN  precise ingredient    "morphine sulfate anhydrous"
#     MIN  multiple ingredients  "amoxicillin / clavulanate"
#     BN   brand name            "MS Contin"               <- want
#     SCD  clinical drug         "morphine sulfate 15 MG oral tablet"  <- no
#     SBD  branded drug          "MS Contin 15 MG oral tablet"         <- no
# Dose-level strings are products, not names: they wreck a synonym index
# because every strength becomes a separate "synonym" of the ingredient.
# --------------------------------------------------------------------------
DRUG_GOOD_TTY = {"IN", "PIN", "MIN", "BN", "PT", "SY", "PN", "MTH_PT",
                 "MTH_SY", "SCN", "FN", "ET"}
DRUG_SKIP_TTY = {"SCD", "SBD", "SCDC", "SBDC", "SCDF", "SBDF", "SCDG",
                 "SBDG", "BPCK", "GPCK", "DF", "DFG", "PSN", "TMSY"}
DRUG_KINDS = {"drug", "substance"}

# --------------------------------------------------------------------------
# Partition scheme for --split-by group: kind -> output file stem.
# --------------------------------------------------------------------------
PARTITION_GROUP = {
    "drug": "drugs",
    "substance": "drugs",
    "disorder": "conditions",
    "injury": "conditions",
    "symptom": "findings",
    "finding": "findings",
    "procedure": "procedures",
    "anatomy": "anatomy",
}

# --------------------------------------------------------------------------
# Semantic types -> (kind, fallback category). Only these CUIs are kept.
# Add or remove rows here to change the clinical scope of the dictionary.
# --------------------------------------------------------------------------
STY_MAP = {
    "Disease or Syndrome":                      ("disorder", "condition"),
    "Mental or Behavioral Dysfunction":         ("disorder", "behavioral_health"),
    "Neoplastic Process":                       ("disorder", "neoplasm"),
    "Congenital Abnormality":                   ("disorder", "congenital"),
    "Acquired Abnormality":                     ("disorder", "condition"),
    "Anatomical Abnormality":                   ("disorder", "condition"),
    "Pathologic Function":                      ("disorder", "condition"),
    "Cell or Molecular Dysfunction":            ("disorder", "condition"),
    "Experimental Model of Disease":            ("disorder", "condition"),
    "Injury or Poisoning":                      ("injury", "injury_poisoning"),
    "Sign or Symptom":                          ("symptom", "symptom"),
    "Finding":                                  ("finding", "finding"),
    "Clinical Attribute":                       ("finding", "finding"),
    "Laboratory or Test Result":                ("finding", "finding"),
    "Therapeutic or Preventive Procedure":      ("procedure", "procedure"),
    "Diagnostic Procedure":                     ("procedure", "procedure"),
    "Laboratory Procedure":                     ("procedure", "laboratory"),
    "Health Care Activity":                     ("procedure", "procedure"),
    "Pharmacologic Substance":                  ("drug", "medication"),
    "Clinical Drug":                            ("drug", "medication"),
    "Antibiotic":                               ("drug", "medication"),
    "Organic Chemical":                         ("substance", "substance"),
    "Body Part, Organ, or Organ Component":     ("anatomy", "anatomy"),
    "Body Location or Region":                  ("anatomy", "anatomy"),
}

# --------------------------------------------------------------------------
# ICD-10-CM chapter ranges -> body-system category label.
# This is what produces "cardiovascular" for I21/I10 and "symptom" for R07.
# --------------------------------------------------------------------------
ICD10_CHAPTERS = [
    ("A00", "B99", "infectious"),
    ("C00", "D49", "neoplasm"),
    ("D50", "D89", "hematologic_immune"),
    ("E00", "E89", "endocrine_metabolic"),
    ("F01", "F99", "behavioral_health"),
    ("G00", "G99", "neurologic"),
    ("H00", "H59", "ophthalmic"),
    ("H60", "H95", "otologic"),
    ("I00", "I99", "cardiovascular"),
    ("J00", "J99", "respiratory"),
    ("K00", "K95", "gastrointestinal"),
    ("L00", "L99", "dermatologic"),
    ("M00", "M99", "musculoskeletal"),
    ("N00", "N99", "genitourinary"),
    ("O00", "O9A", "obstetric"),
    ("P00", "P96", "perinatal"),
    ("Q00", "Q99", "congenital"),
    ("R00", "R99", "symptom"),
    ("S00", "T88", "injury_poisoning"),
    ("U00", "U85", "special_purpose"),
    ("V00", "Y99", "external_cause"),
    ("Z00", "Z99", "health_status"),
]

# SNOMED FSN semantic tags, e.g. "Myocardial infarction (disorder)"
SEMANTIC_TAG_RE = re.compile(
    r"\s*\((disorder|finding|procedure|situation|event|observable entity|"
    r"body structure|qualifier value|substance|product|organism|morphologic "
    r"abnormality|regime/therapy|physical object|cell structure|attribute|"
    r"navigational concept|specimen|record artifact|environment|occupation|"
    r"person|assessment scale|tumor staging|clinical drug|medicinal product"
    r"[^)]*)\)\s*$",
    re.IGNORECASE,
)
# Trailing source cruft: "Headache [Disease/Finding]", "aspirin 81 MG [Bayer]"
BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
WS_RE = re.compile(r"\s+")


# ==========================================================================
# helpers
# ==========================================================================
def opener(path):
    """Open plain or gzipped RRF."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def rrf_rows(path, expect):
    """Stream an RRF file as lists of fields. Skips malformed short lines."""
    with opener(path) as fh:
        for lineno, line in enumerate(fh, 1):
            row = line.rstrip("\n").split("|")
            if len(row) < expect:
                continue
            yield row


def clean_string(s: str) -> str:
    s = SEMANTIC_TAG_RE.sub("", s)
    s = BRACKET_RE.sub("", s)
    s = WS_RE.sub(" ", s).strip(" ,;:.-")
    return s


def is_abbrev_string(s: str) -> bool:
    """Uppercase-ish token(s), <= 6 chars, e.g. MI, AMI, STEMI, HTN."""
    return len(s) <= 6 and s.isupper() and s.isalpha()


def icd10_category(code: str) -> str | None:
    if not code:
        return None
    head = code[:3].upper()
    for lo, hi, label in ICD10_CHAPTERS:
        if lo <= head <= hi:
            return label
    return None


def depth_bucket(depth: int | None, low_max: int, med_max: int) -> str:
    """Hierarchy depth -> specificity label. None means unknown."""
    if depth is None:
        return "unknown"
    if depth <= low_max:
        return "low"
    if depth <= med_max:
        return "medium"
    return "high"


# ==========================================================================
# stage 1: MRSTY -> semantic types for the CUIs we care about
# ==========================================================================
def load_semantic_types(path, allowed_stys):
    """Return {cui: (kind, fallback_category, [sty, ...])} for in-scope CUIs."""
    out = {}
    kept = 0
    for row in rrf_rows(path, len(MRSTY_COLS)):
        sty = row[S["STY"]]
        if sty not in allowed_stys:
            continue
        cui = row[S["CUI"]]
        kind, cat = STY_MAP[sty]
        if cui in out:
            out[cui][2].append(sty)
        else:
            out[cui] = (kind, cat, [sty])
            kept += 1
    print(f"[MRSTY]   {kept:,} CUIs in scope", file=sys.stderr)
    return out


# ==========================================================================
# stage 2: optional seed matching -> restrict to a hand-picked slice
# ==========================================================================
def load_seeds(path):
    cuis, terms = set(), set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            t = line.strip()
            if not t or t.startswith("#"):
                continue
            if re.fullmatch(r"C\d{7}", t):
                cuis.add(t)
            else:
                terms.add(t.lower())
    return cuis, terms


def match_seed_cuis(mrconso, seed_terms, seed_cuis, in_scope):
    """One pass over MRCONSO to turn seed strings into CUIs."""
    hits = set(seed_cuis)
    for row in rrf_rows(mrconso, len(MRCONSO_COLS)):
        if row[C["LAT"]] != "ENG":
            continue
        cui = row[C["CUI"]]
        if in_scope and cui not in in_scope:
            continue
        if clean_string(row[C["STR"]]).lower() in seed_terms:
            hits.add(cui)
    print(f"[seeds]   {len(hits):,} CUIs matched", file=sys.stderr)
    return hits


# ==========================================================================
# stage 3: MRHIER -> hierarchy depth (drives `specificity`)
# ==========================================================================
def load_depths(path, targets, sabs=("SNOMEDCT_US", "MSH")):
    """Shallowest path-to-root length per CUI. PTR is a dot-joined AUI path."""
    depths = {}
    for row in rrf_rows(path, len(MRHIER_COLS)):
        cui = row[H["CUI"]]
        if cui not in targets:
            continue
        if sabs and row[H["SAB"]] not in sabs:
            continue
        ptr = row[H["PTR"]]
        d = ptr.count(".") + 1 if ptr else 0
        prev = depths.get(cui)
        if prev is None or d < prev:
            depths[cui] = d
    print(f"[MRHIER]  depth for {len(depths):,} CUIs", file=sys.stderr)
    return depths


# ==========================================================================
# stage 4: MRCONSO -> the actual synonym harvest
# ==========================================================================
def harvest(mrconso, targets, sab_filter, min_len, max_len,
            keep_abbrev, abbrev_ambiguity_max,
            icd_synonyms=False, canonical_mode="preferred", kind_of=None):
    """
    Returns:
      syns    {cui: {clean_string: best_sab_rank}}
      abbrevs {cui: set(str)}
      brands  {cui: set(str)}   -> drug brand names, kept apart
      icd     {cui: Counter(3-char ICD-10-CM category)}
      rxcui   {cui: RxNorm ingredient code}
      pref    {cui: (rank_tuple, string)}   -> canonical
      sabs    {cui: set(SAB)}
    """
    kind_of = kind_of or {}
    syns = defaultdict(dict)
    abbrevs = defaultdict(set)
    brands = defaultdict(set)
    icd = defaultdict(Counter)
    rxcui = {}
    pref = {}
    sabs = defaultdict(set)
    abbrev_cuis = defaultdict(set)   # ambiguity guard, measured globally
    drug_skipped = 0

    n = 0
    for row in rrf_rows(mrconso, len(MRCONSO_COLS)):
        n += 1
        if n % 2_000_000 == 0:
            print(f"[MRCONSO] {n:,} rows", file=sys.stderr)

        if row[C["LAT"]] != "ENG" or row[C["SUPPRESS"]] != "N":
            continue

        cui, sab, tty = row[C["CUI"]], row[C["SAB"]], row[C["TTY"]]
        raw = row[C["STR"]]

        # global abbreviation ambiguity census (cheap, runs on every row)
        if len(raw) <= 6 and raw.isupper() and raw.isalpha():
            abbrev_cuis[raw].add(cui)

        if targets is not None and cui not in targets:
            continue

        # ICD-10-CM code: capture before the SAB whitelist so it survives
        # even when ICD10CM is not in your preferred-source list.
        if sab in ("ICD10CM", "ICD10"):
            code = row[C["CODE"]]
            if code and code[0].isalpha():
                icd[cui][code[:3]] += 1

        if sab_filter and sab not in sab_filter:
            continue

        # Claims-classification rubrics ("Acute myocardial infarction,
        # unspecified") are code labels, not clinical synonyms. Excluded by
        # default; --icd-synonyms puts them back if you want billing wording.
        if not icd_synonyms and sab in CLASSIFICATION_SABS:
            continue

        is_abbr = tty in ABBREV_TTY
        is_drug = kind_of.get(cui) in DRUG_KINDS

        if is_drug:
            # RxNorm ingredient code is the drug's equivalent of an ICD code
            if sab == "RXNORM" and tty in ("IN", "PIN", "MIN") and cui not in rxcui:
                rxcui[cui] = row[C["CODE"]]
            # dose-level product strings are not synonyms of the ingredient
            if tty in DRUG_SKIP_TTY:
                drug_skipped += 1
                continue
            if tty not in DRUG_GOOD_TTY and not is_abbr:
                continue
        elif tty not in GOOD_TTY and not is_abbr:
            continue

        s = clean_string(raw)
        if not s or not (min_len <= len(s) <= max_len):
            continue

        sabs[cui].add(sab)
        rank = SAB_RANK.get(sab, len(SAB_PREFERENCE))

        if is_abbr or is_abbrev_string(s):
            if keep_abbrev:
                abbrevs[cui].add(s)
            continue

        # A brand name matches the ingredient but is not a synonym of it:
        # "Amoxil" should retrieve amoxicillin documents, yet displaying it
        # as an alternative name for the molecule is wrong.
        if is_drug and tty == "BN":
            brands[cui].add(s)
            continue

        low = s.lower()
        if low not in syns[cui] or rank < syns[cui][low]:
            syns[cui][low] = rank

        # canonical: MRCONSO's own preferred flags, tie-broken by source rank
        is_pref = (row[C["TS"]] == "P" and row[C["STT"]] == "PF"
                   and row[C["ISPREF"]] == "Y")
        if canonical_mode == "shortest":
            # favour the plainest label ("hypertension" over "hypertensive
            # disorder, systemic arterial") - usually better for display
            key = (len(s), 0 if is_pref else 1, rank)
        else:
            key = (0 if is_pref else 1, rank, len(s))
        if cui not in pref or key < pref[cui][0]:
            pref[cui] = (key, low)

    # drop over-ambiguous abbreviations ("MS" -> dozens of CUIs)
    if keep_abbrev and abbrev_ambiguity_max:
        dropped = 0
        for cui, group in abbrevs.items():
            for a in list(group):
                if len(abbrev_cuis.get(a, ())) > abbrev_ambiguity_max:
                    group.discard(a)
                    dropped += 1
        print(f"[abbrev]  dropped {dropped:,} ambiguous abbreviations",
              file=sys.stderr)

    print(f"[MRCONSO] {n:,} rows read, {len(syns):,} CUIs with synonyms",
          file=sys.stderr)
    if drug_skipped:
        print(f"[drugs]   skipped {drug_skipped:,} dose-level product strings",
              file=sys.stderr)
    return syns, abbrevs, brands, icd, rxcui, pref, sabs


# ==========================================================================
# stage 5: assemble
# ==========================================================================
def build(args):
    meta = args.meta_dir
    mrconso = args.mrconso or (os.path.join(meta, "MRCONSO.RRF") if meta else None)
    mrsty = args.mrsty or (os.path.join(meta, "MRSTY.RRF") if meta else None)
    mrhier = args.mrhier or (os.path.join(meta, "MRHIER.RRF") if meta else None)

    if not mrconso or not os.path.exists(mrconso):
        sys.exit("MRCONSO.RRF not found - pass --meta-dir or --mrconso")

    allowed_stys = set(STY_MAP)
    if args.kinds:
        wanted = set(args.kinds.split(","))
        allowed_stys = {s for s, (k, _) in STY_MAP.items() if k in wanted}

    sty_info = {}
    if mrsty and os.path.exists(mrsty):
        sty_info = load_semantic_types(mrsty, allowed_stys)
    else:
        print("[MRSTY]   not found - no semantic filtering, no `kind` field",
              file=sys.stderr)

    in_scope = set(sty_info) if sty_info else None

    targets = in_scope
    if args.seed_file:
        seed_cuis, seed_terms = load_seeds(args.seed_file)
        targets = match_seed_cuis(mrconso, seed_terms, seed_cuis, in_scope)
        if args.expand_descendants and mrhier and os.path.exists(mrhier):
            targets = add_descendants(mrhier, targets, mrconso)

    syns, abbrevs, brands, icd, rxcui, pref, sabs = harvest(
        mrconso, targets,
        sab_filter=set(args.sabs.split(",")) if args.sabs else None,
        min_len=args.min_len, max_len=args.max_len,
        keep_abbrev=not args.no_abbrev,
        abbrev_ambiguity_max=args.abbrev_ambiguity_max,
        icd_synonyms=args.icd_synonyms,
        canonical_mode=args.canonical,
        kind_of={cui: info[0] for cui, info in sty_info.items()},
    )

    external_icd = load_external_icd(args.icd10_map) if args.icd10_map else {}

    depths = {}
    if mrhier and os.path.exists(mrhier):
        depths = load_depths(mrhier, set(syns))
    else:
        print("[MRHIER]  not found - `specificity` will be 'unknown'",
              file=sys.stderr)

    concepts = []
    for cui, syn_map in syns.items():
        canonical = pref.get(cui, (None, None))[1]
        if not canonical:
            continue
        synonyms = sorted(s for s in syn_map if s != canonical)
        abbr = sorted(abbrevs.get(cui, ()))

        brand_list = sorted(brands.get(cui, ()))
        if len(synonyms) + len(abbr) + len(brand_list) < args.min_synonyms:
            continue

        code = None
        if icd.get(cui):
            code = icd[cui].most_common(1)[0][0]
        elif cui in external_icd:
            code = external_icd[cui]

        kind, fallback_cat, stys = sty_info.get(cui, (None, None, []))
        category = icd10_category(code) or fallback_cat or "unclassified"

        concepts.append({
            "cui": cui,
            "canonical": canonical,
            "synonyms": synonyms,
            "abbreviations": abbr,
            "brands": sorted(brands.get(cui, ())),
            "icd10": code,
            "rxcui": rxcui.get(cui),
            "category": category,
            "kind": kind,
            "specificity": depth_bucket(depths.get(cui),
                                        args.low_depth, args.medium_depth),
            "semantic_types": sorted(set(stys)),
            "sources": sorted(sabs.get(cui, ())),
        })

    # rank by synonym richness so --max-concepts keeps the useful ones
    concepts.sort(key=lambda c: (-(len(c["synonyms"]) + len(c["abbreviations"])),
                                 c["canonical"]))
    if args.max_concepts:
        concepts = concepts[:args.max_concepts]
    concepts.sort(key=lambda c: (c["category"], c["canonical"]))

    def meta_for(rows, partition=None):
        m = {
            "source": "UMLS Metathesaurus",
            "release": args.release,
            "concept_count": len(rows),
            "synonym_count": sum(len(c["synonyms"]) + len(c["abbreviations"])
                                 for c in rows),
            "source_vocabularies": args.sabs or ",".join(SAB_PREFERENCE),
            "specificity_thresholds": {"low": f"<={args.low_depth}",
                                       "medium": f"<={args.medium_depth}",
                                       "high": f">{args.medium_depth}"},
        }
        if partition:
            m["partition"] = partition
            m["kinds"] = sorted({c["kind"] for c in rows if c["kind"]})
        return m

    def write_json(path, rows, partition=None):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"meta": meta_for(rows, partition), "concepts": rows},
                      fh, indent=2, ensure_ascii=False)
        surf = sum(len(c["synonyms"]) + len(c["abbreviations"]) for c in rows)
        print(f"wrote {path}: {len(rows):,} concepts, {surf:,} surface forms",
              file=sys.stderr)

    print("", file=sys.stderr)

    if args.split_by == "none":
        write_json(args.out, concepts)
    else:
        out_dir = args.out_dir or os.path.dirname(args.out) or "."
        os.makedirs(out_dir, exist_ok=True)
        buckets = defaultdict(list)
        for c in concepts:
            if args.split_by == "group":
                key = PARTITION_GROUP.get(c["kind"], "other")
            elif args.split_by == "kind":
                key = c["kind"] or "unclassified"
            else:                                   # category
                key = c["category"] or "unclassified"
            buckets[key].append(c)

        index = {}
        for key in sorted(buckets):
            path = os.path.join(out_dir, f"{key}.json")
            write_json(path, buckets[key], partition=key)
            index[key] = {
                "file": f"{key}.json",
                "concepts": len(buckets[key]),
                "surface_forms": sum(len(c["synonyms"]) + len(c["abbreviations"])
                                     for c in buckets[key]),
            }
        with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh:
            json.dump({"release": args.release, "split_by": args.split_by,
                       "partitions": index}, fh, indent=2)
        print(f"wrote {os.path.join(out_dir, 'index.json')}: "
              f"{len(index)} partitions", file=sys.stderr)

    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as fh:
            for c in concepts:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"wrote {args.jsonl}", file=sys.stderr)


def add_descendants(mrhier, targets, mrconso):
    """Pull in everything beneath the seed CUIs (one MRHIER + one MRCONSO pass)."""
    aui2cui = {}
    seed_auis = set()
    for row in rrf_rows(mrconso, len(MRCONSO_COLS)):
        aui2cui[row[C["AUI"]]] = row[C["CUI"]]
        if row[C["CUI"]] in targets:
            seed_auis.add(row[C["AUI"]])
    grown = set(targets)
    for row in rrf_rows(mrhier, len(MRHIER_COLS)):
        ptr = row[H["PTR"]]
        if not ptr:
            continue
        if seed_auis.intersection(ptr.split(".")):
            grown.add(row[H["CUI"]])
    print(f"[descend] {len(targets):,} seeds -> {len(grown):,} CUIs",
          file=sys.stderr)
    return grown


def load_external_icd(path):
    """
    NLM's free SNOMED CT -> ICD-10-CM map, keyed here by CUI is not possible
    directly (the map is SNOMED-code keyed), so this expects a simple
    two-column TSV you produce once: <CUI>\t<ICD10 code>.
    Use it when your download is the Level 0 subset (no ICD10CM in MRCONSO).
    """
    out = {}
    with opener(path) as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and re.fullmatch(r"C\d{7}", parts[0]):
                out[parts[0]] = parts[1][:3]
    print(f"[icd10]   {len(out):,} external CUI->ICD-10 rows", file=sys.stderr)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--meta-dir", help="directory holding MRCONSO.RRF etc.")
    p.add_argument("--mrconso"); p.add_argument("--mrsty"); p.add_argument("--mrhier")
    p.add_argument("--out", default="concepts.json",
                   help="output file when --split-by none (the default)")
    p.add_argument("--split-by", choices=("none", "group", "kind", "category"),
                   default="none",
                   help="write one JSON per partition instead of one file: "
                        "'group' = drugs/conditions/findings/procedures/anatomy, "
                        "'kind' = one per semantic kind, "
                        "'category' = one per body system")
    p.add_argument("--out-dir", help="directory for --split-by output")
    p.add_argument("--jsonl", help="also write one concept per line")
    p.add_argument("--release", default="2026AA")
    p.add_argument("--sabs", default=",".join(SAB_PREFERENCE),
                   help="comma-separated source vocabularies to keep")
    p.add_argument("--kinds", help="restrict semantic scope, e.g. "
                   "'disorder,symptom,injury,procedure'")
    p.add_argument("--seed-file", help="one term or CUI per line")
    p.add_argument("--expand-descendants", action="store_true",
                   help="with --seed-file, also keep hierarchical descendants")
    p.add_argument("--icd10-map", help="TSV of CUI<TAB>ICD10 for Level 0 builds")
    p.add_argument("--min-synonyms", type=int, default=2)
    p.add_argument("--max-concepts", type=int, default=0)
    p.add_argument("--min-len", type=int, default=2)
    p.add_argument("--max-len", type=int, default=60)
    p.add_argument("--no-abbrev", action="store_true")
    p.add_argument("--icd-synonyms", action="store_true",
                   help="also treat ICD/CPT rubric text as synonyms")
    p.add_argument("--canonical", choices=("preferred", "shortest"),
                   default="preferred",
                   help="'preferred' uses UMLS preferred-name flags; "
                        "'shortest' favours the plainest label")
    p.add_argument("--abbrev-ambiguity-max", type=int, default=3,
                   help="drop an abbreviation mapping to more CUIs than this")
    p.add_argument("--low-depth", type=int, default=3)
    p.add_argument("--medium-depth", type=int, default=7)
    build(p.parse_args())


if __name__ == "__main__":
    main()
