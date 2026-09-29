# Approach: extracting a concept dictionary from UMLS

This document explains *why* the extraction is built the way it is.
`README.md` covers how to run it; `RRF_REFERENCE.md` is the column-level
reference for the UMLS files.

**Scope:** this folder covers extraction only — turning the UMLS release files
into a compact, reviewable JSON dictionary. How that dictionary is consumed
downstream (query linking, expansion, ranking) is deliberately out of scope.

---

## 1. What we are extracting, and why

UMLS is not a dictionary of words. It is a map of **concepts**, each with a
stable identifier (CUI), onto which every participating vocabulary's names are
attached. Each name is an **atom** with its own identifier (AUI).

That single fact is the whole basis of the extraction: **rows sharing a CUI are
synonyms of each other.** One concept as UMLS actually stores it:

| CUI | SAB | TTY | CODE | STR |
|---|---|---|---|---|
| C0027051 | MTH | PN | — | Myocardial Infarction |
| C0027051 | SNOMEDCT_US | PT | 22298006 | Myocardial infarction |
| C0027051 | SNOMEDCT_US | FN | 22298006 | Myocardial infarction (disorder) |
| C0027051 | SNOMEDCT_US | SY | 22298006 | Cardiac infarction |
| C0027051 | CHV | SY | — | heart attack |
| C0027051 | MSH | ET | D009203 | Coronary Thrombosis |
| C0027051 | SNOMEDCT_US | AB | 22298006 | MI |
| C0027051 | ICD10CM | PT | I21.9 | Acute myocardial infarction, unspecified |

Group by CUI, filter sensibly, and that becomes one record.

---

## 2. The one design decision that matters

**Extract once, offline. Never read UMLS at request time.**

The full Metathesaurus is 38 GB uncompressed with ~3.6 million concepts.
Distilling it to a scoped JSON file — typically a few thousand concepts and
~12 MB — gives you an artefact that loads in under a second, has no database
dependency, and can be diffed, reviewed and version-controlled like source code.

```
ONCE PER RELEASE (offline)
──────────────────────────
MRCONSO.RRF ┐
MRSTY.RRF   ├──► extractor ──► concepts.json  (or partitioned per domain)
MRHIER.RRF  ┘     stream, filter, group by CUI
```

NLM ships two releases a year (`2026AA`, `2026AB`), so rebuilding is a
twice-yearly job, not a live integration.

---

## 3. Pipeline stages, and why they run in this order

The order is forced by how the files join, not by preference.

1. **MRSTY first.** It is small and it tells you which concepts are even worth
   carrying. Filtering to disorders, symptoms, injuries, procedures and drugs
   removes organisms, geographic regions, lab specimen types and abstract
   "Intellectual Product" concepts — an order-of-magnitude reduction *before*
   touching the big file.

2. **MRCONSO second.** Harvest names for the surviving CUIs and build the
   `AUI → CUI` map in the same pass. One read, two products.

3. **MRHIER last.** Its `PTR` column spells ancestry in **AUIs, not CUIs**, so it
   is literally uninterpretable without step 2's map. This asymmetry is the single
   most common source of confusion when people first join these files.

Everything streams line by line. `MRCONSO.RRF` in pandas costs several GB of RAM
for no benefit, since each row is touched exactly once.

---

## 4. How each output field is derived

| Field | Source | Derivation |
|---|---|---|
| `cui` | MRCONSO `CUI` | The concept identity; the join key for everything |
| `canonical` | MRCONSO `STR` | Row flagged `TS=P, STT=PF, ISPREF=Y`, tie-broken by source preference |
| `synonyms` | MRCONSO `STR` grouped by `CUI` | English, `SUPPRESS=N`, whitelisted `SAB`, name-like `TTY` |
| `abbreviations` | MRCONSO `TTY` ∈ AB/ACR/AA/AS | Segregated, and ambiguity-filtered |
| `brands` | MRCONSO `TTY=BN` (drugs only) | Kept apart from synonyms |
| `icd10` | MRCONSO `CODE` where `SAB=ICD10CM` | Truncated to the 3-character category |
| `rxcui` | MRCONSO `CODE` where `SAB=RXNORM`, `TTY` ∈ IN/PIN/MIN | Ingredient-level code |
| `category` | derived from `icd10` | ICD-10 chapter → body system; falls back to a semantic-type group |
| `kind` | MRSTY `STY` | Disease or Syndrome → disorder, Sign or Symptom → symptom, … |
| `specificity` | MRHIER `PTR` | Shallowest path depth; `≤3` low, `≤7` medium, else high |
| `semantic_types` | MRSTY `STY` | Retained verbatim for downstream filtering |
| `sources` | MRCONSO `SAB` | Provenance, for auditing a bad synonym |

`category` and `specificity` are the only two fields UMLS does not hand you.
Deriving category from the ICD-10 chapter produces sensible labels with no
hand-tagging: `I00–I99` → cardiovascular, `R00–R99` → symptom. So myocardial
infarction and hypertension both land on *cardiovascular* while chest pain lands
on *symptom*.

---

## 5. Extraction decisions worth defending

### English, non-suppressed, whitelisted sources

`LAT=ENG` and `SUPPRESS=N` are not optional. The precomputed subsets include
obsolete and editorially suppressed atoms; skip that filter and you ship retired
terminology as current. The `SAB` whitelist is where you trade coverage for
precision — trimming it shrinks the output and raises quality, at the cost of
losing lay wording if you cut `CHV` and `MEDLINEPLUS`.

### ICD/CPT rubric text is not synonym text

`ICD10CM` atoms read "Acute myocardial infarction, unspecified" and "Pain in
throat and chest". Those are billing labels, not clinical language. They are
mined for the **code** and excluded from `synonyms` by default
(`--icd-synonyms` restores them if you specifically want claims wording).

### SNOMED semantic tags are metadata

A fully specified name ends in `(disorder)`, `(finding)`, `(procedure)`. Strip
the trailing tag; it is structural information, not part of the name.

### Abbreviations are segregated and ambiguity-guarded

`MS` resolves to multiple sclerosis, mitral stenosis, morphine sulfate *and*
mental status. The extractor counts how many CUIs each short uppercase form maps
to and drops those above a threshold (default 3), then keeps the survivors in
their own `abbreviations` list rather than mixing them into `synonyms` — so a
consumer can weight or drop them independently.

### Length bounds

Some sources carry full descriptive sentences as single atom strings. A 2–60
character bound removes nearly all of them without losing real synonyms.

### Drugs get their own rules

RxNorm models a drug at several levels, and most are not names:

| TTY | Example | Treatment |
|---|---|---|
| `IN` | morphine sulfate | canonical, and the `rxcui` |
| `PIN` / `MIN` | morphine sulfate anhydrous | synonym |
| `BN` | MS Contin | `brands` — matches the ingredient, is not a name for it |
| `SCD` / `SBD` / `PSN` | morphine sulfate 15 MG oral tablet | discarded |

Leaving dose-level product strings in makes every strength and dose form a
separate "synonym" of the ingredient — a hundred noise strings per drug.

**If medications are a first-class concern, extract them from standalone RxNorm
instead.** It is free, carries no additional licence restrictions, updates weekly
rather than twice a year, and ships the real ingredient/brand/product
relationships (`RXNREL`) that UMLS only approximates through CUI grouping.

### Three relationships, not one

The most common modelling error is flattening these together:

```
SAME CONCEPT (same CUI)        myocardial infarction · cardiac infarction ·
                               heart attack · infarction of heart · MI · AMI

DESCENDANT (different CUI)     STEMI (C0521106) · NSTEMI (C1961113) ·
                               silent MI · reinfarction

RELATED BUT DISTINCT           angina pectoris (C0002962)
```

Only the first group is synonymy, and only the first group belongs in
`synonyms`. Descendants come from MRHIER (`--expand-descendants`) or MRREL and
should stay identifiable as descendants. `angina` is neither — writing it into a
chest-pain synonym list silently destroys the distinction.

### Partitioned output

`--split-by group` writes `drugs.json`, `conditions.json`, `findings.json`,
`procedures.json`, `anatomy.json` and an `index.json` of counts. The partition
comes straight from MRSTY, so it costs nothing, and it lets a consumer load only
the domains it cares about — which is also the cleanest way to keep drug concepts
out of a symptom lookup.

---

## 6. Scoping the extraction

Three levers, cheapest first:

- **`--kinds`** — semantic scope, e.g. `disorder,symptom,injury,procedure`.
- **`--seed-file`** — a hand-picked list of terms or CUIs. Fifty seeds relevant to
  your domain beat three million concepts you will never query.
- **`--expand-descendants`** — with a seed file, also pull everything beneath
  those concepts in the hierarchy. Fifty seeds typically become a few thousand
  relevant concepts.

Then `--max-concepts` caps the result, ranked by synonym richness so the useful
entries survive the cut.

---

## 7. Licence considerations

- A free UMLS licence and UTS account are required, renewed annually.
- Source vocabularies carry licence categories 0–4. **Category 0** means no
  restrictions beyond the standard UMLS terms; 1–4 add restrictions.
- The **Level 0 Subset** contains only category-0 sources — the right choice if
  anything derived from the data leaves the building.
- **ICD-10-CM is category 4**, so it is *absent* from the Level 0 subset. If you
  need the `icd10` field under Level 0, fill it from NLM's free SNOMED CT →
  ICD-10-CM map (`--icd10-map`).
- SNOMED CT is governed by its own affiliate terms (Appendix 2 of the licence),
  separate from the 0–4 scheme.
- `MRCONSO.SRL` records each atom's restriction level — the `sources` field is
  kept in the output so you can always prove which vocabularies an artefact drew
  on.

---

## 8. What this extraction does not do

- **No lexical normalisation.** The SPECIALIST Lexical Tools (`luiNorm`) ship only
  in the Full Release and would improve fuzzy matching.
- **No relationship graph.** `MRREL` is where "related concepts" lives; only
  descendant expansion via MRHIER is wired in.
- **No post-coordination.** SNOMED expression constraints need a terminology
  server, not a flat file.
- **No manual review — and it needs one.** A generated dictionary is a first
  draft. Sort by synonym count, read the top few hundred entries, delete what is
  wrong. That review is where the last 10% of quality lives, and no amount of
  filtering logic replaces it.
