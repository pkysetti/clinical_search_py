# RRF reference: MRCONSO, MRSTY, MRHIER

Column-level reference for the three UMLS Rich Release Format files this pipeline
reads. See `APPROACH.md` for the reasoning and `README.md` for usage.

---

## Format rules

All RRF files follow the same four rules:

- **Pipe-delimited** (`|`)
- **No header row** — the column order below *is* your schema
- **A trailing pipe** at the end of every line
- **Column order fixed** by the release specification; UTF-8 throughout

Empty fields are simply nothing between two pipes. Because there is no header,
mis-ordering the columns fails silently — you read term types as source codes and
get a plausible-looking, wrong dictionary.

---

## The identifier ladder

UMLS names things at four levels, and every join uses one of them. This is the
most useful thing to hold in your head:

```
CUI  C0027051   concept  — the unit of synonymy. Everything sharing it is a synonym.
 └─ LUI  L…      term     — lexical variants of one name: "Heart Attack" / "heart attacks"
     └─ SUI  S…   string   — one exact string in one language, case included
         └─ AUI  A…  atom  — that string as asserted by ONE specific source
```

**Group by CUI to get synonyms. Join on AUI to get hierarchy.** LUI and SUI can
be ignored for search; they matter for normalisation and deduplication.

---

## MRCONSO.RRF — concepts, names and sources

18 columns. One row per **atom** (one name from one source). Roughly 15 million
rows; 2.2 GB uncompressed. The file you cannot do without.

| # | Column | Meaning | In the pipeline |
|---|---|---|---|
| 1 | `CUI` | Concept unique identifier | **Group by this** → synonym set |
| 2 | `LAT` | Language of the string: `ENG`, `SPA`, `FRE`… | **Filter** to `ENG` |
| 3 | `TS` | Term status. `P` = preferred term of the concept, `S` = non-preferred | Canonical name (1 of 3) |
| 4 | `LUI` | Term identifier — groups lexical variants | Skip for search |
| 5 | `STT` | String type. `PF` = preferred form; `VC`/`VO`/`VW` = case, order, word-order variants | Canonical name (2 of 3) |
| 6 | `SUI` | String identifier — one exact string in one language | Skip for search |
| 7 | `ISPREF` | Whether this atom is preferred within its term. `Y`/`N` | Canonical name (3 of 3) |
| 8 | `AUI` | Atom identifier — this exact string from this exact source | **Join key to MRHIER** |
| 9 | `SAUI` | The source's own atom id, where it has one | Skip |
| 10 | `SCUI` | The source's own concept id. For SNOMED rows, the SNOMED concept id | Useful if you also run native SNOMED |
| 11 | `SDUI` | The source's own descriptor id — e.g. MeSH `D009203` | Optional provenance |
| 12 | `SAB` | Source abbreviation: `SNOMEDCT_US`, `MSH`, `RXNORM`, `CHV`, `ICD10CM`… | **Whitelist** and source ranking |
| 13 | `TTY` | Term type — see table below | **The most useful filter in the file** |
| 14 | `CODE` | The source's code for this string: `22298006`, `I21.9` | `icd10` / `rxcui` fields |
| 15 | `STR` | The actual string | **The synonym itself** |
| 16 | `SRL` | Source restriction level, 0–4 — the licence category | Audit what you shipped |
| 17 | `SUPPRESS` | `N` usable; `O` obsolete, `E` editor-suppressed, `Y` source-suppressed | **Filter to `N`** |
| 18 | `CVF` | Content view flag — marks rows in NLM's curated subsets | Skip |

### Term types (`TTY`) you will actually meet

| TTY | Meaning | Keep? |
|---|---|---|
| `PT` | Preferred term | yes |
| `PN` | Metathesaurus preferred name (from `MTH`) | yes — canonical |
| `FN` | Fully specified name (SNOMED; carries a `(disorder)` tag) | yes, strip the tag |
| `SY` | Synonym | yes |
| `MH` | MeSH main heading | yes |
| `EN` / `ET` / `PEP` | MeSH entry terms | yes — curated "what else people call it" |
| `AB` / `ACR` / `AA` | Abbreviation / acronym | yes, segregated |
| `IN` / `PIN` / `MIN` | RxNorm ingredient forms | yes, drugs |
| `BN` | RxNorm brand name | yes, as `brands` |
| `SCD` / `SBD` / `PSN` | RxNorm dose-level products | **no** |
| `PM` | MeSH permuted term ("Attack, Heart") | no — usually suppressed anyway |
| `IS` / `LO` / `OAP` | Obsolete or legacy forms | no |

### Sources (`SAB`) worth whitelisting

| SAB | What it is | Brings |
|---|---|---|
| `MTH` | NLM's own editorial layer, not an external vocabulary | The default canonical name (`CODE` is usually `NOCODE`) |
| `SNOMEDCT_US` | SNOMED CT US Edition | The bulk of clinical synonymy, plus the IS-A hierarchy |
| `MSH` | MeSH — NLM's PubMed indexing vocabulary | Clean canonical names, curated entry terms, chemicals |
| `CHV` | Consumer Health Vocabulary | Lay/patient wording ("heart attack"). **Frozen at 2011**, all lowercase |
| `MEDLINEPLUS` | NLM consumer health site vocabulary | Current plain language, narrow coverage |
| `RXNORM` | Normalised drug names | Ingredients, brands, `rxcui` |
| `LNC` | LOINC | Lab and observation names |
| `NCI` | NCI Thesaurus | Oncology depth, good abbreviations |
| `HPO` | Human Phenotype Ontology | Phenotype/finding granularity |
| `ICD10CM` | ICD-10-CM | **Codes only, never synonym text.** Licence category 4 |

Check versions in your own download rather than trusting any list:

```bash
awk -F'|' '$4=="MTH" || $4=="MSH" || $4=="CHV" {print $3, $4, $5, $6}' MRSAB.RRF
```

---

## MRSTY.RRF — what kind of thing the concept is

6 columns. One row per concept-to-semantic-type assignment, so a concept with two
types produces two rows. Small; loads in seconds.

| # | Column | Meaning | In the pipeline |
|---|---|---|---|
| 1 | `CUI` | Same concept identifier as MRCONSO | **Join key** |
| 2 | `TUI` | Semantic type id: `T047` Disease or Syndrome, `T184` Sign or Symptom, `T121` Pharmacologic Substance (127 types) | Stable filter value |
| 3 | `STN` | Tree number, e.g. `B2.2.1.2.1`. Prefixes nest, so you can select whole branches | Group by prefix |
| 4 | `STY` | Human-readable type name | **The `kind` field** |
| 5 | `ATUI` | Attribute identifier for this assignment | Skip |
| 6 | `CVF` | Content view flag | Skip |

This file is the main noise filter. Without it the dictionary fills with
organisms, geographic regions, lab specimen types and abstract "Intellectual
Product" concepts that will never help a clinical query.

---

## MRHIER.RRF — where the atom sits in the tree

9 columns. One row per **path**, not per concept: an atom with three routes to the
root produces three rows, which is why this is the largest file on disk.

| # | Column | Meaning | In the pipeline |
|---|---|---|---|
| 1 | `CUI` | The concept this atom belongs to | Where depth is recorded |
| 2 | `AUI` | The atom whose position this row describes | **Join key to MRCONSO** |
| 3 | `CXN` | Context number — distinguishes multiple positions in a polyhierarchy | Take the minimum depth |
| 4 | `PAUI` | The immediate parent atom | Direct parent lookups |
| 5 | `SAB` | Which vocabulary's hierarchy this path belongs to | **Filter**, usually `SNOMEDCT_US` |
| 6 | `RELA` | Relationship label, usually `isa`, sometimes `part_of` | Keep `isa` for taxonomy |
| 7 | `PTR` | Path to root: a dot-joined chain of ancestor **AUIs** | **Depth = dots + 1**; ancestry = membership test |
| 8 | `HCD` | The source's own hierarchy code, where published | Skip |
| 9 | `CVF` | Content view flag | Skip |

Example — the deeper the path, the more specific the concept:

```
C0027051|A0003|1|A9000|SNOMEDCT_US|isa|A9001.A9002.A9003.A9004.A9005.A9006.A9007.A9000|||
                                       └────────────── 8 ancestors = deep = specific
```

---

## How the three join

Two keys, at two different levels:

```
MRSTY.RRF   ──join on CUI──┐
 CUI → semantic type       │
                           ├──►  one concept record
MRCONSO.RRF ──group by CUI─┤       kind, semantic_types      ← MRSTY
 CUI ↔ AUI ↔ STR           │       canonical, synonyms,
      │                    │         abbreviations, icd10    ← MRCONSO
      │ AUI→CUI map        │       specificity               ← MRHIER
      ▼                    │       category  ← derived from icd10 chapter
MRHIER.RRF  ──join on AUI──┘
 AUI → PTR path
```

**The asymmetry that catches people out:** `PTR` spells ancestry in AUIs, not
CUIs. MRHIER therefore cannot be interpreted at all without MRCONSO's
atom-to-concept map — which is why the extractor must pass over MRCONSO before it
can use the hierarchy.

That fixes the order of the passes:

1. **MRSTY** — decide which CUIs are in scope (cheap, big reduction)
2. **MRCONSO** — harvest names *and* build the `AUI → CUI` map (one pass, two products)
3. **MRHIER** — now `PTR` is interpretable: count dots for depth, test seed AUIs
   for descendants

---

## The fourth file, when you need it

`MRREL.RRF` holds concept-to-concept relationships, joined on `CUI1`/`CUI2` with
`REL` and `RELA` naming the link (`RN` narrower, `RB` broader, `RO` other,
`CHD`/`PAR` child/parent). MRHIER tells you *how deep*; MRREL tells you *what
relates to what*. It is the file you need to merge STEMI/NSTEMI under myocardial
infarction, or to relate angina pectoris to chest pain — neither of which plain
CUI synonymy will give you. It is also the largest file in the release, so filter
it to your target CUIs on the first pass.
