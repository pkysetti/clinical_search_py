# UMLS_MASTER — flat master term files + similarity, offline

Turns the UMLS Metathesaurus into **flat, queryable CSV master files** (real RxNorm
IDs for drugs, CUIs + ICD-10 for problems/procedures) plus a **synonym map**, and a
**similarity tool** that finds related concepts via UMLS hierarchy + relationships.

Everything runs **offline** against the local UMLS release — no network, no server.
The heavy lifting (streaming RRF parsing, drug rules, RxNorm capture) is **reused**
from `../UMLS_FILTER/build_concept_dictionary.py`; this folder only adds the CSV
emission and the similarity layer. No parser logic is duplicated.

## Files in this folder

| File | What it is | Role |
|---|---|---|
| `build_master_csv.py` | Python script (stdlib only) | **Builds** the master CSVs + synonym map from UMLS. Reuses the parsing core from `../UMLS_FILTER/build_concept_dictionary.py`. |
| `similar.py` | Python script (stdlib only) | **Finds similar concepts** for a CUI/term via MRHIER + MRREL. Reads the zipped hierarchy/relationship files; caches results. |
| `README_umls_master.md` | This file | Developer guide — navigation, dependencies, run commands, outputs. |
| `parse_umls_master_data.md` | Companion doc | Usage guidance — normalise a record → expand to similar meds/diagnoses (workflow + worked examples). |

**How the Python files relate** (run order top → bottom):

```
UMLS_FILTER/build_concept_dictionary.py   ← parsing core: harvest(), load_semantic_types(), RRF readers
        ▲  imported by
        │
UMLS_MASTER/build_master_csv.py           ← assembles concepts → writes data/master/*.{csv,jsonl,json}
        │  produces synonym-map.csv + concepts.jsonl
        ▼
UMLS_MASTER/similar.py                    ← reads MRHIER/MRREL zips + names from concepts.jsonl → similar/<CUI>.json
```

- `build_master_csv.py` is the **producer** — run it first; it creates `synonym-map.csv` and `concepts.jsonl`.
- `similar.py` is the **consumer** — it needs `synonym-map.csv` (for `--term`) and `concepts.jsonl`
  (for name enrichment) to already exist, plus the MRHIER/MRREL zips.
- The two scripts do **not** import each other; they only share the on-disk data files.

## Dependencies & prerequisites

- **Python:** 3.9+ (uses `from __future__ import annotations`; developed and tested on 3.13).
- **Packages: none to install.** Both scripts use **only the Python standard library** — no pip
  packages, no third-party venv dependencies for these two files:

  | Script | stdlib modules used |
  |---|---|
  | `build_master_csv.py` | `argparse`, `csv`, `json`, `os`, `sys`, `time`, `collections`, `pathlib` (+ imports `build_concept_dictionary`, which is also stdlib-only) |
  | `similar.py` | `argparse`, `gzip`, `json`, `re`, `sys`, `time`, `zipfile`, `csv`, `pathlib` |

  So any stock interpreter works — the project venv (`.venv\Scripts\python`) or a system `python`.
- **The real dependency is on-disk data:** the UMLS release files in [Data source](#data-source-verified-on-this-machine-2026-09-30).
  - `build_master_csv.py` needs `MRCONSO.RRF` + `MRSTY.RRF`.
  - `similar.py` additionally needs the two `.nlm` zips (MRHIER/MRREL) **and** the already-built
    `synonym-map.csv` / `concepts.jsonl`.
  - Missing files produce a clear "not found" message, not a crash.
- **Run from the project root** (`C:\github\clinical_search_py`) so the relative `data/master/`
  output path and the `../UMLS_FILTER` import both resolve correctly.

## Data source (verified on this machine, 2026-09-30)

| File | Path |
|---|---|
| MRCONSO.RRF (2.2 GB) | `C:\Inference\umls-2026AA-full\2026AA-full\2026AA\META\MRCONSO.RRF` |
| MRSTY.RRF (70 MB) | `...\2026AA\META\MRSTY.RRF` |
| MRHIER (in zip, 2.2 GB) | `...\2026aa-1-meta.nlm` → `2026AA/META/MRHIER.RRF.{aa..ae}.gz` |
| MRREL (in zip, 1.9 GB) | `...\2026aa-2-meta.nlm` → `2026AA/META/MRREL.RRF.{aa..ae}.gz` |

> **Release quirk handled:** this release's `MRSTY.RRF` ships **TUI codes** with the
> name columns empty (`C0027051|T047||||`). `build_concept_dictionary.load_semantic_types`
> reads the *name* column and would match nothing. `build_master_csv.py` therefore
> classifies by TUI code via a validated `TUI_TO_NAME` map (and auto-falls back to the
> name column on releases that populate it).

## 1) Build the master CSVs

```bat
.venv\Scripts\python UMLS_MASTER\build_master_csv.py ^
    --meta-dir C:\Inference\umls-2026AA-full\2026AA-full\2026AA\META
```

One streaming pass over MRCONSO (~20 s) + CSV emission. Output → `data\master\`:

| File | One row per… | Headline ID columns |
|---|---|---|
| `drug-master.csv` | drug CUI | **rxcui** (real RxNorm), cui |
| `problem-terms.csv` | diagnosis/problem CUI (disorder/injury/symptom/finding) | **cui**, icd10 |
| `procedure-terms.csv` | procedure CUI | **cui** |
| `anatomy-terms.csv` | anatomy CUI | **cui** |
| `synonym-map.csv` | every surface form (denormalised) | term → cui / rxcui / icd10 |
| `concepts.jsonl` | one concept per line — the durable checkpoint | — |
| `manifest.json` | release stamp, row counts, chunk lists | — |

### Column schemas

```
drug-master.csv      : cui, canonical, rxcui, category, synonyms, abbreviations, brands, semantic_types, sources
problem-terms.csv    : cui, canonical, icd10, category, synonyms, abbreviations, semantic_types, sources
procedure-terms.csv  : cui, canonical, category, synonyms, abbreviations, semantic_types, sources
anatomy-terms.csv    : (same as procedure-terms)
synonym-map.csv      : term, term_type, cui, canonical, category, kind, rxcui, icd10
```

List-valued cells (`synonyms`, `brands`, `semantic_types`, `sources`) are joined with
`"; "`. `term_type` ∈ {canonical, synonym, abbreviation, brand}.

### Sample rows (real, from the 2026AA build)

```csv
# drug-master.csv
cui,canonical,rxcui,category,synonyms,abbreviations,brands,semantic_types,sources
C0004057,aspirin,1191,medication,acetylsalicylic acid; aspirins; product containing aspirin,,,Organic Chemical; Pharmacologic Substance,CHV; MSH; MTH; NCI; RXNORM; SNOMEDCT_US
C0002645,amoxicillin,723,medication,amoxycillin; p-hydroxyampicillin,,,Organic Chemical,CHV; MSH; MTH; NCI; RXNORM; SNOMEDCT_US

# problem-terms.csv
cui,canonical,icd10,category,synonyms,abbreviations,semantic_types,sources
C0027051,myocardial infarction,I21,cardiovascular,cardiac infarction; heart attack; infarction of heart,AMI,Disease or Syndrome,CHV; HPO; MEDLINEPLUS; MSH; MTH; NCI; SNOMEDCT_US

# synonym-map.csv
term,term_type,cui,canonical,category,kind,rxcui,icd10
heart attack,synonym,C0027051,myocardial infarction,cardiovascular,disorder,,I21
AMI,abbreviation,C0027051,myocardial infarction,cardiovascular,disorder,,I21
aspirin,canonical,C0004057,aspirin,medication,drug,1191,
```

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--meta-dir` | the 2026AA path above | dir holding MRCONSO.RRF / MRSTY.RRF |
| `--out-dir` | `data\master` | where CSVs are written |
| `--csv-only` | off | skip the MRCONSO scan; re-emit CSVs from `concepts.jsonl` (instant) |
| `--min-synonyms` | 1 | min surface forms per concept (1 = max breadth) |
| `--max-concepts` | 0 (no cap) | cap total concepts |
| `--chunk-size N` | 0 (off) | split any partition > N rows into `<stem>-0001.csv`, `-0002.csv`, … |
| `--sabs` | clinical whitelist | comma-separated source vocabularies to keep |

## 2) Find similar concepts (hierarchy + MRREL)

```bat
:: by CUI
.venv\Scripts\python UMLS_MASTER\similar.py --cui C0027051 --max 20

:: by surface term (resolved through synonym-map.csv first)
.venv\Scripts\python UMLS_MASTER\similar.py --term "heart attack" --max 20
```

For a given CUI it streams MRHIER + MRREL once and returns **subtypes** (descendants),
**related** (MRREL `RN`/`RB` edges), and **broader/class** (ancestors), interleaved into a
balanced list. Results are cached to `data\master\similar\<CUI>.json`, so repeat queries
are instant (`--no-cache` forces a fresh scan). First run per CUI ≈ 2–3 min.

Verified output:
- **MI (C0027051)** → acute anterior/inferior MI, acute coronary syndrome, coronary artery disease, cardiomyopathies…
- **aspirin (C0004057)** → antiplatelet drug, NSAID, antithrombotic agents, acetylsalicylic acid, Alka-Seltzer / Anacin / Aggrenox…

## 3) Using it to extract similar meds/diagnoses from a patient record

Two layers:

1. **Normalise** — load `synonym-map.csv` into `{term.lower(): row}`. Scan the record;
   every matched surface form ("amoxil", "heart attack", "MI") resolves to a canonical
   concept + its IDs (rxcui / cui / icd10) + category. This turns free text into a
   structured problem/med list.
2. **Expand** — for each resolved CUI, call `similar.py` to pull subtypes, related
   concepts, and the class it belongs to. That is the "similar medications / diagnoses"
   signal, grounded in UMLS structure rather than string similarity.

```python
import csv, json, subprocess
term = "heart attack"
row = next(r for r in csv.DictReader(open("data/master/synonym-map.csv", encoding="utf-8"))
           if r["term"].lower() == term)          # -> C0027051, I21
out = subprocess.run(["python", "UMLS_MASTER/similar.py", "--cui", row["cui"], "--max", "20"],
                     capture_output=True, text=True).stdout
similar = json.loads(out)["similar"]              # subtypes / related / class
```

## Notes & caveats

- **Category labels** are a soft grouping. A concept with several semantic types keeps the
  most clinically specific one (drug > substance; anything with a real `rxcui` is labelled
  `medication`). Multi-tagged edge cases (e.g. cancer staging) may carry a secondary label.
- **Abbreviation ambiguity** is preserved, not collapsed: "AMI" maps to MI *and* its
  anterior/acute subtypes as separate `synonym-map.csv` rows — disambiguate by context or
  pick the most specific downstream.
- **Scope** is clinical + labs/anatomy (every kind in the extractor's `STY_MAP`). For a
  full Metathesaurus dump, lower `--min-synonyms`, drop the scope, and set `--chunk-size`.
- **Licensing:** outputs are derived from UMLS sources (incl. category 1–4). Check your
  UTS license terms before shipping anything derived from them.
