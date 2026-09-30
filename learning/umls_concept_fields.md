# UMLS for Clinical Search — Understanding the Concept Fields

A learning guide to the fields you get when you distil the **UMLS Metathesaurus** into a
practical clinical-search vocabulary. These are *simplified, application-ready* columns derived
from the raw UMLS files (`MRCONSO`, `MRSTY`) — not the raw RRF layout itself.

The goal: take messy, redundant medical language ("DM", "Type II Diabetes", "Adult-onset
diabetes") and unify it under one concept that you can **search, classify, and link** to drug
and diagnosis coding systems.

---

## Where these fields come from

| Source file | What it holds | Feeds |
|---|---|---|
| `MRCONSO.RRF` (Metathesaurus) | Every term string (`STR`), its term type (`TTY`), source vocabulary, and the concept it belongs to (`CUI`) | `term`, `term_type`, `cui`, `canonical`, `rxcui`, `icd10` |
| `MRSTY.RRF` (Semantic Types) | The semantic type(s) assigned to each CUI | `category`, `kind` |

A single **CUI** can have many term strings attached to it — that's the whole point of UMLS:
many surface forms, one concept.

```
Diabetes Mellitus      ┐
DM                     ├─▶  C0011849  (one concept)
Type II Diabetes       │
Adult-onset diabetes   ┘
```

---

## The eight fields

### `term` — the actual text
The literal name/string, straight from `MRCONSO.STR`. This is what a user might type or what
appears in a record.

| term |
|---|
| Diabetes Mellitus |
| DM |
| Type II Diabetes |
| Adult-onset diabetes |

All of the above may belong to the **same CUI**.

---

### `term_type` — what kind of name it is
Usually derived from `MRCONSO.TTY` (Term Type). Telling you *how* a term relates to the concept
is useful when ranking search results (a preferred term should outrank an abbreviation).

| TTY | Meaning |
|---|---|
| PT | Preferred Term |
| SY | Synonym |
| AB | Abbreviation |
| FN | Full Name |
| BN | Brand Name |
| IN | Ingredient Name |
| PN | Preferred Name |

| term | term_type |
|---|---|
| Diabetes Mellitus | PT |
| DM | AB |
| Adult-onset diabetes | SY |

> **In this project** the `synonym-map.csv` surfaces these as friendlier labels —
> `canonical`, `synonym`, `abbreviation`, `brand` — rather than raw TTY codes. Same idea,
> easier to read in a CSV.

---

### `cui` — the concept's primary key
The **UMLS Concept Unique Identifier**. Think of it as the primary key of the medical concept:
every synonym shares the same CUI, so you can deduplicate and join on it.

| term | cui |
|---|---|
| Diabetes Mellitus | C0011849 |
| Type 2 diabetes | C0011849 |
| DM | C0011849 |

---

### `canonical` — the normalized search form
A cleaned, normalized version of the term used for **search, deduplication, embeddings, and
autocomplete**. Typically lower-cased and punctuation/variant-normalized.

```
"Diabetes Mellitus"   →  "diabetes mellitus"
"Type-II Diabetes"    →  "type ii diabetes"
```

You match a user's input against `canonical` (and the other `term`s) to land on the right CUI.

---

### `category` — the broad application bucket
A high-level semantic category, derived from `MRSTY` semantic types and collapsed into a small
set of application-friendly buckets.

| Semantic Type (MRSTY) | Category |
|---|---|
| Disease or Syndrome | Disease |
| Neoplastic Process | Disease |
| Pharmacologic Substance | Drug |
| Laboratory Procedure | Procedure |
| Body Part, Organ, or Organ Component | Anatomy |

| term | category |
|---|---|
| Diabetes | Disease |
| Metformin | Drug |
| Hemoglobin A1c test | Procedure |

---

### `kind` — the exact semantic type
More specific than `category`: the precise `MRSTY` semantic type. Two concepts can share a
`category` but differ in `kind`.

| category | kind |
|---|---|
| Disease | Disease or Syndrome |
| Disease | Congenital Abnormality |
| Drug | Pharmacologic Substance |
| Drug | Clinical Drug |
| Procedure | Laboratory Procedure |

For diabetes: **category = Disease**, **kind = Disease or Syndrome**.

> `category` answers "which bucket does this go in?"; `kind` answers "what exactly is it?".

---

### `rxcui` — the RxNorm ID (drugs only)
The **RxNorm Concept ID**. Populated **only for drug concepts**; empty otherwise. It links a UMLS
drug concept to the RxNorm medication vocabulary, which is what e-prescribing and drug databases
use.

| Drug | rxcui |
|---|---|
| Metformin | 6809 |
| Aspirin | 1191 |
| Lisinopril | 29046 |

Useful for: medication normalization, e-prescribing, joining to drug databases.

```
term  = "Metformin 500 MG Oral Tablet"
rxcui = 860975        ← a specific product/strength maps to its own RxNorm ID
```

---

### `icd10` — the ICD-10 diagnosis code
The **ICD-10-CM** code associated with a concept. Populated for diagnosis/problem concepts; empty
for drugs. It links a UMLS concept to the billing/claims coding system.

| Concept | icd10 |
|---|---|
| Type 2 diabetes mellitus | E11.9 |
| Acute bronchitis | J20.9 |
| Essential hypertension | I10 |

Useful for: billing, claims, diagnosis coding.

```
term  = "Type 2 diabetes mellitus"
cui   = C0011860
icd10 = E11.9
```

---

## Example records

**Diagnosis concept** (no drug ID, has an ICD-10 code):

```json
{
  "term": "Type 2 diabetes mellitus",
  "term_type": "PT",
  "cui": "C0011860",
  "canonical": "type 2 diabetes mellitus",
  "category": "Disease",
  "kind": "Disease or Syndrome",
  "rxcui": null,
  "icd10": "E11.9"
}
```

**Drug concept** (has an RxNorm ID, no ICD-10 code):

```json
{
  "term": "Metformin",
  "term_type": "PT",
  "cui": "C0025598",
  "canonical": "metformin",
  "category": "Drug",
  "kind": "Pharmacologic Substance",
  "rxcui": "6809",
  "icd10": null
}
```

Notice the pattern: **`rxcui` and `icd10` are mutually exclusive in practice** — drugs carry an
RxNorm ID, diagnoses carry an ICD-10 code. Both share the same `cui` / `canonical` / `category` /
`kind` backbone.

---

## Why these fields matter

For a clinical semantic-search database on UMLS, **`cui` + `canonical` + `category` + `kind` +
`rxcui` + `icd10`** are the minimum high-value fields to add on top of raw `MRCONSO`, because
together they let you:

- **Unify synonyms** — many surface forms collapse to one `cui` / `canonical`.
- **Classify concepts** — `category` (bucket) + `kind` (exact type) drive routing, filtering, and
  ranking.
- **Link to coding systems** — `rxcui` → RxNorm (medications), `icd10` → ICD-10-CM (diagnoses),
  so a search hit connects straight to e-prescribing and billing/claims.

That combination is what turns a bag of medical strings into a usable, queryable clinical
vocabulary.

---

## How this maps to the project files

The `UMLS_MASTER` build emits exactly these fields as flat CSVs (see `UMLS_MASTER/README.md`):

| This doc's field | Where it lands |
|---|---|
| `term`, `term_type`, `cui`, `canonical`, `category`, `kind`, `rxcui`, `icd10` | `data/master/synonym-map.csv` (one row per surface form) |
| `cui`, `canonical`, `rxcui`, … | `data/master/drug-master.csv` (one row per drug) |
| `cui`, `canonical`, `icd10`, … | `data/master/problem-terms.csv` (one row per diagnosis) |

So the conceptual model in this guide is the same shape as the data you can actually query.
