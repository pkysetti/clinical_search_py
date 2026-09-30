# UMLS Usage Guidance — extracting similar medications & diagnoses from a patient record

This is the **how-to-use** guide for the `UMLS_MASTER` artifacts (see `README.md` for build
commands and file schemas). It walks through turning free-text patient-record language into
standardised concepts, then expanding each concept to clinically related ones — using only the
offline UMLS data on this machine.

---

## 1. What you have

| Artefact | Role in the workflow |
|---|---|
| `data/master/synonym-map.csv` | **Normalisation layer** — any surface form → CUI + rxcui/icd10 + category |
| `data/master/drug-master.csv` | Drug reference — real RxNorm IDs (`rxcui`) per drug CUI |
| `data/master/problem-terms.csv` | Diagnosis/problem reference — CUI + ICD-10 |
| `data/master/procedure-terms.csv`, `anatomy-terms.csv` | Procedure / anatomy references |
| `UMLS_MASTER/similar.py` | **Expansion layer** — CUI → subtypes / related / class (MRHIER + MRREL) |

The two layers do different jobs:

```
                 ┌─────────────────────────────┐
 patient text  ─▶│ 1. NORMALISE (synonym-map)  │  "amoxil" / "heart attack" / "MI"
                 └──────────────┬──────────────┘            ▼
                                │              CUI + rxcui/icd10 + category
                 ┌──────────────▼──────────────┐
                 │ 2. EXPAND (similar.py)      │  CUI → similar meds / diagnoses
                 └──────────────┬──────────────┘            ▼
                                │        subtypes · related · class (clinically grounded)
```

---

## 2. How the master data is built & how the files link

All six data files come from **one build pass** (`build_master_csv.py`) over the same UMLS release.
The key mental model: there is a **single parent dataset**, and every other file is either a
*view* of it or a *flattening* of it — so they can never disagree about a given concept.

### Build pipeline (stages)

```
   MRSTY.RRF (semantic types)              MRCONSO.RRF (terms, names, codes)
            │                                        │
            └───────────────┬────────────────────────┘
                            ▼
   1. CLASSIFY   each in-scope CUI → kind + category   (TUI code → semantic type → bucket)
   2. HARVEST    one streaming pass over MRCONSO: canonical, synonyms, abbreviations,
                 brands, ICD-10, RxNorm (rxcui), sources            [drug rules applied]
   3. ASSEMBLE   one row per CUI  ────────────────▶  concepts.jsonl    ← PARENT + checkpoint
                            │
   4. PARTITION  split the SAME concept set by `kind`
                 ┌──────────┼──────────────┬───────────────┐
                 ▼          ▼              ▼               ▼
            drug-master  problem-terms  procedure-terms  anatomy-terms    ← VIEWS (1 row / CUI)
                            │
   5. FLATTEN  explode every surface form (canonical + synonyms + abbreviations + brands)
                 into one row each, repeating the concept's identity columns
                 ────────────────────────────────▶  synonym-map.csv      ← LOOKUP (many rows / CUI)
```

### The files and their grain

| File | Grain | Key | What it is |
|---|---|---|---|
| `concepts.jsonl` | **1 row per CUI** | `cui` (primary key) | **Parent / source of truth** — the full concept record; also the resume checkpoint |
| `drug-master.csv` | 1 row per drug CUI | `cui` | View where `kind` ∈ {drug, substance} — adds `rxcui`, `brands` |
| `problem-terms.csv` | 1 row per problem CUI | `cui` | View where `kind` ∈ {disorder, injury, symptom, finding} — adds `icd10` |
| `procedure-terms.csv` | 1 row per procedure CUI | `cui` | View where `kind` = procedure |
| `anatomy-terms.csv` | 1 row per anatomy CUI | `cui` | View where `kind` = anatomy |
| `synonym-map.csv` | **many rows per CUI** (1 per surface form) | `term` (lookup), `cui` (foreign key) | Flattened term→concept lookup used for matching |

### What ties them together

1. **One pass, one concept set.** All six files are projections of the *same* in-memory list of
   concepts assembled in a single run — nothing is built independently, so they always agree on a CUI.
2. **CUI is the universal join key.** `concepts.jsonl` and the four master CSVs are keyed 1:1 by
   `cui`; `synonym-map.csv` carries `cui` as a foreign key on every term row. Join on `cui` to move
   between any of them.
3. **`kind` is the router.** A concept's semantic `kind` (from MRSTY) decides *exactly one* of the
   four master CSVs it lands in — so those four files are mutually exclusive and together cover every
   concept in `concepts.jsonl`.
4. **The JSONL is the parent; the CSVs are derived.** You can regenerate all five CSVs from
   `concepts.jsonl` alone (`--csv-only`) without re-reading MRCONSO — which proves the dependency
   direction: `concepts.jsonl` → master CSVs, and `concepts.jsonl` → `synonym-map.csv`.

### Concrete example — aspirin across the files

One concept (C0004057) appears in each layer at its natural grain:

```
concepts.jsonl      1 row :  {cui:C0004057, canonical:"aspirin", kind:drug, rxcui:1191, ...}
drug-master.csv     1 row :  C0004057, aspirin, rxcui=1191, category=medication, ...
synonym-map.csv     N rows : "aspirin" (canonical), "acetylsalicylic acid" (synonym), ...
                    all with cui=C0004057, rxcui=1191   ← join back to the concept by cui
```

**In short:** `concepts.jsonl` is the single source of truth; the four `drug-master.csv` /
`*-terms.csv` files are kind-partitioned views of it; and `synonym-map.csv` is its surface-form
flattening — all joined by the CUI.

---

## 3. Step 1 — Normalise a patient record

Load `synonym-map.csv` once into a lookup, then match the record's terms against it. Every hit
resolves to a canonical concept and its IDs.

```python
import csv

def load_synonym_map(path="data/master/synonym-map.csv"):
    """term (lowercase) -> list of candidate rows (a term can be ambiguous)."""
    from collections import defaultdict
    m = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m[row["term"].lower()].append(row)
    return m

SMAP = load_synonym_map()

def resolve(term: str):
    """Return the best candidate row for a surface form, or None."""
    cands = SMAP.get(term.lower())
    if not cands:
        return None
    # prefer canonical > synonym > abbreviation > brand
    rank = {"canonical": 0, "synonym": 1, "abbreviation": 2, "brand": 3}
    return sorted(cands, key=lambda r: (rank.get(r["term_type"], 9), r["cui"]))[0]

print(resolve("heart attack"))   # -> C0027051 myocardial infarction, I21, cardiovascular
print(resolve("aspirin"))        # -> C0004057 aspirin, rxcui 1191, medication
```

**Matching strategy for a whole record.** A patient record is prose, so you need to find the
terms inside it. Two robust options:

- **Phrase scan (simplest):** for each term in `synonym-map.csv`, check whether it appears in the
  record text (word-boundary match). Cheap, no model needed — good for a POC. Longer/multi-word
  terms first so "heart attack" wins over "attack".
- **NER (more precise):** run the project's medspaCy/QuickUMLS NER (`src/nlp.py`) to get entity
  spans, then resolve each span through `synonym-map.csv`. Handles negation ("no history of MI")
  and context better.

Either way, the output of Step 1 is a list of `{term, cui, rxcui?, icd10?, category, kind}` —
a structured medication + problem list pulled from free text.

---

## 4. Step 2 — Expand each concept to similar ones

For every resolved CUI, call `similar.py`. It returns a balanced mix of:

- **subtypes** (hierarchy descendants) — e.g. MI → STEMI / NSTEMI
- **related** (MRREL `RN`/`RB` edges) — e.g. aspirin → acetylsalicylic acid, Alka-Seltzer
- **broader / class** (hierarchy ancestors) — e.g. aspirin → antiplatelet drug, NSAID

```bat
:: by CUI
.venv\Scripts\python UMLS_MASTER\similar.py --cui C0027051 --max 20

:: or straight from a surface term (resolves via synonym-map first)
.venv\Scripts\python UMLS_MASTER\similar.py --term "heart attack" --max 20
```

Machine-readable JSON goes to **stdout**; a human table goes to **stderr**. Results are cached to
`data/master/similar/<CUI>.json`, so the second call for the same CUI is instant.

### Verified example — similar diagnoses (myocardial infarction, C0027051)

```
 1. [descendant] acute anterior wall myocardial infarction        C2349195
 2. [mrrel RB]   acute myocardial infarction                      C0155626
 3. [ancestor]   abnormal cardiovascular system physiology        C4023587
 4. [descendant] acute anteroapical myocardial infarction         C0264698
 5. [mrrel RB]   acute MI of anterolateral wall                   C0155627
 6. [ancestor]   acute coronary syndrome                          C0948089
 7. [descendant] acute infarction of papillary muscle             C0264674
 8. [mrrel RB]   acute MI of inferolateral wall                   C0340308
 9. [ancestor]   cardiomyopathies                                 C0878544
10. [descendant] acute inferior myocardial infarction             C0264700
...
14. [ancestor]   coronary artery disease                          C1956346
```

### Verified example — similar medications (aspirin, C0004057 / rxcui 1191)

```
 1. [descendant] acetaminophen / aspirin                          C1873940
 2. [mrrel RB]   acetyl salicylate                                C0304348
 3. [ancestor]   analgesic and antipyretic                        C0280004
 5. [mrrel RB]   acetylsalicylate sodium                          C0981808
 6. [ancestor]   analgesics                                       C0002771
 8. [mrrel RB]   aggrenox (brand)                                 C0732282
11. [mrrel RB]   alka seltzer (brand)                             C0051166
12. [ancestor]   anti-inflammatory agents, non-steroidal          C0003211
14. [ancestor]   antiplatelet drug                                C0085826
17. [ancestor]   antithrombotic agents                            C1704311
```

Note how the **class** comes through the ancestors (`antiplatelet drug`, `NSAID`,
`antithrombotic agents`) — that is the clinically meaningful "similar medication" signal, grounded
in UMLS structure rather than string similarity.

---

## 5. End-to-end: from a record snippet to similar meds/diagnoses

```python
import csv, json, subprocess

RECORD = ("Pt presents with chest pain and a history of heart attack. "
          "Currently on aspirin 81 mg daily and atorvastatin.")

def load_synonym_map(path="data/master/synonym-map.csv"):
    from collections import defaultdict
    m = defaultdict(list)
    for row in csv.DictReader(open(path, encoding="utf-8")):
        m[row["term"].lower()].append(row)
    return m

SMAP = load_synonym_map()
rank = {"canonical": 0, "synonym": 1, "abbreviation": 2, "brand": 3}

def best(term):
    c = SMAP.get(term.lower())
    return sorted(c, key=lambda r: (rank.get(r["term_type"], 9), r["cui"]))[0] if c else None

# 1) pull the clinical terms out of the record (phrase scan for the POC)
found = []
for term in ("heart attack", "chest pain", "aspirin", "atorvastatin"):
    row = best(term)
    if row:
        found.append(row)

# 2) expand each to similar concepts
for row in found:
    out = subprocess.run(
        ["python", "UMLS_MASTER/similar.py", "--cui", row["cui"], "--max", "8"],
        capture_output=True, text=True).stdout
    sim = json.loads(out)["similar"]
    print(f"\n{row['canonical']}  [{row['cui']}]  rxcui={row['rxcui'] or '-'}  icd10={row['icd10'] or '-'}")
    for s in sim[:5]:
        print(f"   - {s['relation']:>10}: {s['name'] or s['cui']}")
```

This gives you, per matched concept: its standard ID (RxNorm / CUI / ICD-10) **and** the set of
clinically related concepts — ready for downstream tasks like drug-interaction checks, guideline
matching, or "patients with similar diagnoses" cohorting.

---

## 6. Practical tips & caveats

- **Ambiguity is preserved, not collapsed.** "AMI" maps to MI *and* its anterior/acute subtypes as
  separate `synonym-map.csv` rows. Pick the most specific candidate (or disambiguate by context)
  before expanding.
- **Category is a soft label.** It helps grouping but don't treat it as authoritative — a
  multi-tagged concept keeps its most clinically specific tag (anything with a real `rxcui` is
  labelled `medication`).
- **Similarity is per-CUI and cached.** First scan for a CUI ≈ 2–3 min (streams MRHIER + MRREL);
  repeat calls are instant. For a whole record's concept set, batch the CUIs so you scan the
  hierarchy/relationship files once instead of once-per-concept.
- **Scope** is clinical + labs/anatomy. If you need broader recall, rebuild with a lower
  `--min-synonyms` / wider scope and `--chunk-size` (see `README.md`).
- **Licensing.** Outputs derive from UMLS sources (incl. category 1–4). Check your UTS license
  before shipping anything derived from them.

## 7. Quick reference

```bat
:: rebuild the master CSVs (one ~20s pass)
.venv\Scripts\python UMLS_MASTER\build_master_csv.py --meta-dir C:\Inference\umls-2026AA-full\2026AA-full\2026AA\META

:: re-emit CSVs from the checkpoint (no MRCONSO scan)
.venv\Scripts\python UMLS_MASTER\build_master_csv.py --csv-only

:: similar concepts for a CUI / term
.venv\Scripts\python UMLS_MASTER\similar.py --cui C0027051 --max 20
.venv\Scripts\python UMLS_MASTER\similar.py --term "aspirin" --max 20
```
