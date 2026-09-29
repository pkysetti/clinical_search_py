# UMLS_FILTER — extracting a concept dictionary from UMLS

Turns a UMLS Metathesaurus release into a compact JSON synonym dictionary you can
hold in memory, instead of querying a 38 GB database.

**Scope:** extraction only. Consuming the dictionary downstream (query linking,
expansion, ranking) is out of scope here.

```json
{
  "cui": "C0027051",
  "canonical": "myocardial infarction",
  "synonyms": ["cardiac infarction", "coronary thrombosis",
               "heart attack", "infarction of heart"],
  "abbreviations": ["AMI", "MI"],
  "brands": [],
  "icd10": "I21",
  "rxcui": null,
  "category": "cardiovascular",
  "kind": "disorder",
  "specificity": "high",
  "semantic_types": ["Disease or Syndrome"],
  "sources": ["CHV", "ICD10CM", "MSH", "MTH", "NCI", "SNOMEDCT_US"]
}
```

## Files

| File | Purpose |
|---|---|
| `build_concept_dictionary.py` | The extractor. Streams the RRF files, writes the JSON. |
| `make_fixture.py` | Writes a tiny synthetic `fixture/` of RRF files so the whole pipeline can be exercised before the real download finishes. |
| `APPROACH.md` | Why the extraction is built this way — design decisions and their rationale. |
| `RRF_REFERENCE.md` | Column-by-column reference for MRCONSO, MRSTY, MRHIER and how they join. |

## Which UMLS download

Requires a free UMLS licence and UTS account, renewed annually.

| You need | Download | Notes |
|---|---|---|
| Just synonyms, fastest start | **MRCONSO.RRF** (492 MB zip) | One file. No semantic types, no hierarchy → no `kind`, weak `category`, no `specificity`. |
| Production, licence-safe | **Level 0 Subset** (1.9 GB zip) | All `MR*` tables, category-0 sources only. **Excludes ICD-10-CM** (licence category 4) → supply `--icd10-map`. |
| Everything, simplest | **Full Subset** (5.4 GB zip) | Same tables with ICD10CM present. Contains category 1–4 sources (CPT, MedDRA, ICD-10-CM, CDT) — check terms before shipping derived data. |

None of the three include MetamorphoSys, the Semantic Network, or the SPECIALIST
Lexicon; those are only in the Full Release (`umls-2026AA-full.zip`).

After unzipping, the files land in `<release>/META/`. Three of them do all the
work: `MRCONSO.RRF`, `MRSTY.RRF`, `MRHIER.RRF`.

## Quick start

```bash
# 1. Exercise the pipeline on synthetic data first (two seconds, no download)
python make_fixture.py
python build_concept_dictionary.py --meta-dir fixture --out sample.json --min-synonyms 1

# 2. Then the real thing
python build_concept_dictionary.py --meta-dir ./2026AA/META \
    --kinds disorder,symptom,injury,procedure \
    --max-concepts 2000 --out concepts.json

# 3. Scoped to your own seed list, plus everything beneath those concepts
python build_concept_dictionary.py --meta-dir ./2026AA/META \
    --seed-file seeds.txt --expand-descendants --out concepts.json

# 4. One file per domain
python build_concept_dictionary.py --meta-dir ./2026AA/META \
    --split-by group --out-dir ./dict
# → dict/drugs.json  dict/conditions.json  dict/findings.json
#   dict/procedures.json  dict/anatomy.json  dict/index.json
```

No third-party dependencies — standard library only. Files stream line by line,
so a 2.2 GB `MRCONSO.RRF` runs in a few hundred MB of RAM.

## Options

| Flag | Default | Effect |
|---|---|---|
| `--meta-dir` | — | Directory holding the RRF files (or pass `--mrconso/--mrsty/--mrhier` individually) |
| `--out` | `concepts.json` | Output file when not splitting |
| `--split-by` | `none` | `group` / `kind` / `category` — writes one JSON per partition plus `index.json` |
| `--out-dir` | — | Directory for split output |
| `--kinds` | all | Semantic scope, e.g. `disorder,symptom,procedure` |
| `--seed-file` | — | One term or CUI per line; restricts the extraction to those concepts |
| `--expand-descendants` | off | With a seed file, also keep hierarchical descendants |
| `--sabs` | see script | Comma-separated source vocabularies to keep, in preference order |
| `--canonical` | `preferred` | `preferred` uses UMLS's own flags; `shortest` favours the plainest label |
| `--icd-synonyms` | off | Also treat ICD/CPT rubric text as synonyms |
| `--icd10-map` | — | `CUI<TAB>ICD10` TSV, for Level 0 builds with no ICD10CM |
| `--min-synonyms` | 2 | Drop concepts with fewer surface forms than this |
| `--max-concepts` | 0 (all) | Cap the output, ranked by synonym richness |
| `--min-len` / `--max-len` | 2 / 60 | Character bounds on a synonym string |
| `--no-abbrev` | off | Drop abbreviations entirely |
| `--abbrev-ambiguity-max` | 3 | Discard an abbreviation mapping to more CUIs than this |
| `--low-depth` / `--medium-depth` | 3 / 7 | Hierarchy-depth thresholds for `specificity` |
| `--jsonl` | — | Also write one concept per line |

## Field mapping at a glance

| JSON key | Comes from |
|---|---|
| `cui` | MRCONSO `CUI` |
| `canonical` | MRCONSO `STR` where `TS=P, STT=PF, ISPREF=Y` |
| `synonyms` | every other MRCONSO `STR` for that CUI |
| `abbreviations` | MRCONSO rows with `TTY` ∈ AB/ACR/AA/AS |
| `brands` | MRCONSO `TTY=BN` (drug concepts only) |
| `icd10` | MRCONSO `CODE` where `SAB=ICD10CM`, truncated to 3 chars |
| `rxcui` | MRCONSO `CODE` where `SAB=RXNORM`, `TTY` ∈ IN/PIN/MIN |
| `category` | derived from the ICD-10 chapter |
| `kind` | MRSTY `STY` |
| `specificity` | MRHIER `PTR` depth |
| `sources` | MRCONSO `SAB` |

Full detail in `RRF_REFERENCE.md`; the reasoning in `APPROACH.md`.

## Things that will bite you

- **UMLS does not merge near-synonyms.** "angina pectoris" is `C0002962`, not a
  synonym of chest pain (`C0008031`). Cross-concept relationships live in
  `MRREL.RRF`, not in CUI synonymy.
- **Abbreviations are the main precision risk.** The ambiguity guard handles the
  worst cases; review the surviving lists by hand.
- **Level 0 has no ICD-10-CM.** Use the Full Subset, or build a `CUI<TAB>ICD10`
  TSV from NLM's free SNOMED CT → ICD-10-CM map and pass `--icd10-map`.
- **Release churn.** NLM ships twice a year. CUIs are stable but names move — the
  release is stamped in the output's `meta` block, so diff on rebuild.
- **Review the output.** A generated dictionary is a first draft; the last 10% of
  quality is a human reading the top few hundred concepts.
