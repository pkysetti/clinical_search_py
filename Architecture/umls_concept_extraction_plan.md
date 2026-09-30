# UMLS Concept Extraction — Plan

Goal: parse **as many UMLS concepts as possible** from the Metathesaurus into a JSON artefact, and wire that artefact into the search pipeline (`src/nlp.py` concept indexes, medspaCy NER).

**Key finding that shapes this plan:** a complete, documented extractor already exists in `UMLS_FILTER/` — it was verified working on the bundled fixture on 2026-09-29 (Phase 0 below). This plan is therefore **verify → extract at scale → integrate**, not build-from-scratch. Writing a second parser would duplicate ~700 lines of already-defended logic (`APPROACH.md`).

---

## Table of Contents

1. [Goal and Scope](#goal-and-scope)
2. [Current State — What Is on Disk](#current-state--what-is-on-disk)
3. [Key Decision — Reuse the Existing Extractor](#key-decision--reuse-the-existing-extractor)
4. [Phased Plan](#phased-plan)
   - [Phase 0 — Fixture Verification (done)](#phase-0--fixture-verification-done)
   - [Phase 1 — Real-Release Column Layout Check (top risk)](#phase-1--real-release-column-layout-check-top-risk)
   - [Phase 2 — Full Extraction from a Real UMLS Release](#phase-2--full-extraction-from-a-real-umls-release)
   - [Phase 3 — Integration with `src/`](#phase-3--integration-with-src)
   - [Phase 4 — Rebuild the QuickUMLS Database for medspaCy NER](#phase-4--rebuild-the-quickumls-database-for-medspacy-ner)
   - [Phase 5 — Acceptance Criteria](#phase-5--acceptance-criteria)
5. [Risks and Mitigations](#risks-and-mitigations)
6. [Open Questions](#open-questions)

---

## Goal and Scope

**In scope**

- Extract UMLS concepts (CUI, canonical name, synonyms, abbreviations, ICD-10, semantic types, specificity) into JSON — as many concepts as the chosen release/scope allows.
- Make the extracted JSON consumable by the existing pipeline: `SynonymIndex` (spaCy engine), `UMLSConceptIndex` (medspaCy engine), and the QuickUMLS NER database.
- Keep everything reproducible: one command per artefact, release stamped in the output `meta` block.

**Out of scope**

- Query-time UMLS access (extraction is offline, once per release — see `APPROACH.md` §2).
- Relationship mining (`MRREL`), SPECIALIST lexical normalisation, post-coordination.
- Manual curation of the output (flagged as a human review step, not an automation task).

---

## Current State — What Is on Disk

| Asset | Location | Status |
|---|---|---|
| Extractor (720 lines, stdlib-only, streaming) | `UMLS_FILTER/build_concept_dictionary.py` | **Verified working** on fixture, 2026-09-29 |
| Fixture RRF files (synthetic, 9 CUIs) | `UMLS_FILTER/fixture/` (`MRCONSO.RRF`, `MRSTY.RRF`, `MRHIER.RRF`) | Only UMLS data currently on this machine |
| Sample outputs | `UMLS_FILTER/dict/` (`conditions.json`, `drugs.json`, `findings.json`, `procedures.json`, `index.json`) | Built from fixture |
| Design docs | `UMLS_FILTER/README.md`, `APPROACH.md`, `RRF_REFERENCE.md` | Complete |
| Concept-index interface | `src/nlp.py` — `SynonymIndex`, `UMLSConceptIndex` | Both read `{concepts: [{canonical, synonyms, category, specificity}]}` |
| Current hand-written dictionary | `data/synonyms.json` (~250 lines) | What the spaCy engine uses today |
| QuickUMLS database | `data/index/quickumls/` | **Empty stub** — 36-byte simstring file, empty `cui-semtypes.db/`. medspaCy NER currently has *zero* local UMLS concepts |
| QuickUMLS db builder | `download_dependencies.py --umls-path <dir>` (runs `python -m quickumls.install`) | Ready, but needs a real UMLS install dir |

The only UMLS files present in the project are the **fixture** files under `UMLS_FILTER/fixture/`. A full extraction therefore requires downloading a real release (Level 0 Subset or Full Subset — see `UMLS_FILTER/README.md` "Which UMLS download").

---

## Key Decision — Reuse the Existing Extractor

`build_concept_dictionary.py` already does exactly what was requested — stream MRCONSO/MRSTY/MRHIER, group by CUI, emit JSON — with more capability than a from-scratch script would have:

- Streaming line-by-line (a 2.2 GB `MRCONSO.RRF` runs in a few hundred MB of RAM)
- Canonical-name resolution (`TS=P, STT=PF, ISPREF=Y`, source-preference tie-break)
- Abbreviation segregation with ambiguity guard ("MS" → 4 CUIs → dropped)
- Drug-level rules (RxNorm `IN/PIN/MIN/BN` kept; dose-level `SCD/SBD/PSN` discarded)
- ICD-10 code mining without leaking billing rubric text into synonyms
- `kind` / `category` / `specificity` derivation from MRSTY + MRHIER depth
- Scoping levers: `--kinds`, `--seed-file`, `--expand-descendants`, `--max-concepts`, `--split-by group|kind|category`

Its output schema is a **superset** of the UMLS-native shape discussed earlier:

```json
{
  "cui": "C0027051",
  "canonical": "myocardial infarction",
  "synonyms": ["cardiac infarction", "coronary thrombosis", "heart attack", "infarction of heart"],
  "abbreviations": ["AMI", "MI"],
  "brands": [],
  "icd10": "I21",
  "rxcui": null,
  "category": "cardiovascular",
  "kind": "disorder",
  "specificity": "high",
  "semantic_types": ["Disease or Syndrome"],
  "sources": ["CHV", "MSH", "MTH", "NCI", "SNOMEDCT_US"]
}
```

Decision: **extend and integrate this extractor; do not write a parallel one.**

---

## Phased Plan

### Phase 0 — Fixture Verification (done)

Run the pipeline on the synthetic fixture, no download needed:

```bat
cd C:\github\clinical_search_py
.venv\Scripts\python UMLS_FILTER\build_concept_dictionary.py ^
    --meta-dir UMLS_FILTER\fixture --out UMLS_FILTER\dict\plan_check.json --min-synonyms 1
```

**Result (2026-09-29): PASS.** `9 concepts, 23 surface forms`. Verified behaviours:

- C0027051 → canonical `myocardial infarction`, synonyms incl. `heart attack`, abbreviations `MI`/`AMI`, `icd10: I21`, `category: cardiovascular`, `specificity: high`
- 4 ambiguous `MS` abbreviations dropped (maps to 4 CUIs)
- 4 dose-level drug strings (`morphine sulfate 15 MG oral tablet`, …) skipped
- Spanish row (`Infarto del Miocardio`) filtered out by `LAT=ENG`

### Phase 1 — Real-Release Column Layout Check (top risk)

The extractor assumes this **18-column** MRCONSO layout (`MRCONSO_COLS` in `build_concept_dictionary.py`; the same assumption is baked into `quickumls.constants.HEADERS_MRCONSO`):

```
CUI LAT TS LUI STT SUI ISPREF AUI SAUI SCUI SDUI SAB TTY CODE STR SRL SUPPRESS CVF
```

NLM's documented layout for real Metathesaurus releases has historically been **39 columns with `SAB` in column 2 and `LAT` in column 20** — i.e. a different order. The fixture was generated to match the *assumed* layout, so everything passes on the fixture while a real file could be misparsed. This is exactly the failure mode `RRF_REFERENCE.md` warns about: *"mis-ordering the columns fails silently — you read term types as source codes and get a plausible-looking, wrong dictionary."*

**Before any scale run, verify against the actual release:**

1. Count fields on real rows:
   ```bat
   .venv\Scripts\python -c "print(len(open(r'<release>\META\MRCONSO.RRF',encoding='utf-8').readline().rstrip('\n').split('|')))"
   ```
2. Spot-check a known concept — find the `C00271946` / `C0027051` rows and confirm which column holds `SAB`, `LAT`, `STR`:
   ```bat
   .venv\Scripts\python -c "import io; [print(r[:25]) for r in open(r'<release>\META\MRCONSO.RRF',encoding='utf-8') if r.startswith('C00271946|')][:3]"
   ```
3. Cross-check against NLM's table documentation for that specific release (column order is part of the release spec).
4. **If the real layout differs:** add a small layout adapter at the top of `build_concept_dictionary.py` (map physical column index → logical field name) and keep the rest of the pipeline untouched. The same fix must be applied to the QuickUMLS build path (Phase 4), since `quickumls.install` shares the assumption — either patch a copy of its header map or pre-convert the file.

### Phase 2 — Full Extraction from a Real UMLS Release

**Prerequisite:** download a release via the UTS account (Level 0 Subset ≈ 1.9 GB zip, or Full Subset ≈ 5.4 GB zip) and unzip so `MRCONSO.RRF`, `MRSTY.RRF`, `MRHIER.RRF` sit in `<release>/META/`.

```bat
:: Clinical scope for the search pipeline (recommended primary artefact)
.venv\Scripts\python UMLS_FILTER\build_concept_dictionary.py ^
    --meta-dir <release>\META --release 2026AA ^
    --kinds disorder,symptom,injury,procedure,drug,substance,finding,anatomy ^
    --out data\umls_concepts.json

:: Maximum coverage ("as many as possible" — no semantic-type filter)
.venv\Scripts\python UMLS_FILTER\build_concept_dictionary.py ^
    --meta-dir <release>\META --release 2026AA ^
    --min-synonyms 1 --out data\umls_full.json

:: One file per domain + index
.venv\Scripts\python UMLS_FILTER\build_concept_dictionary.py ^
    --meta-dir <release>\META --split-by group --out-dir data\umls_dict
```

Scale expectations: `MRCONSO.RRF` ≈ 2.2 GB / ~15 M rows; a few minutes of CPU; hundreds of MB RAM (streaming). The unscoped full dump is multi-GB JSON — fine as a reference artefact, but the *pipeline* dictionary should be the scoped one (Phase 3 explains why).

### Phase 3 — Integration with `src/`

The extractor's output is already **structurally compatible** with `SynonymIndex`: it reads `data["concepts"]` and uses `canonical`, `synonyms`, `category`, `specificity` — all present. Two gaps to close:

1. **Abbreviations live in a separate field.** `SynonymIndex` only reads `synonyms`, so `MI`/`AMI` would not be indexed by the spaCy engine. Fix (small, in `src/nlp.py`): when building `_term_to_canonical`, also merge each concept's `abbreviations` list. This is the single highest-value integration change for search quality.
2. **Point the config at the new file.** Either copy the scoped artefact to `data/synonyms.json` (drop-in, zero code change) or add a `UMLS_CONCEPTS_FILE` path in `src/config.py` and let `SYNONYMS_FILE` default to it when present.

**Dictionary size guidance:** the spaCy engine builds one EntityRuler pattern per term — a multi-million-term full dump would make that ruler enormous and slow. Use the **scoped** artefact (clinical kinds, typically a few thousand concepts) for the pipeline; keep `umls_full.json` as the reference/audit artefact. The medspaCy engine does not need this dictionary at all — it reads the QuickUMLS db (Phase 4).

**Optional enhancement:** extend `UMLSConceptIndex` to load the extracted JSON at startup (CUI ↔ name map, synonym sets) so `get_canonical` / `get_synonyms` work offline without a live QuickUMLS handle. Low priority — only needed if medspaCy mode must run without the db.

### Phase 4 — Rebuild the QuickUMLS Database for medspaCy NER

Today `data/index/quickumls/` is an empty stub, so medspaCy NER resolves nothing locally. Build it from the same release:

```bat
:: Option A — via the project's downloader (preferred, keeps one entry point)
.venv\Scripts\python download_dependencies.py --umls-path <release>\META

:: Option B — direct
.venv\Scripts\python -m quickumls.install <release>\META data\index\quickumls
```

Caveats:

- `quickumls.install` carries the **same 18-column assumption** as the extractor — Phase 1's layout check/fix applies here too.
- After a real db lands, re-run `search.py` and re-tune `THRESHOLDS` / `EXACT_MATCH_BTS_MIN` in `src/config.py` (per the config comments, they were tuned for `all-MiniLM-L6-v2`; the embedding model is now `UFNLP/gatortron-base-2k`).

### Phase 5 — Acceptance Criteria

- [ ] Phase 0 fixture run reproduces `UMLS_FILTER/dict/` outputs (done)
- [ ] Real-release column layout verified against NLM docs; adapter added if needed; extractor + quickumls both parse real rows correctly
- [ ] Scoped extraction completes; output passes `json.load`; `meta.concept_count` / `synonym_count` match row-level spot counts
- [ ] ≥ 3 known CUIs spot-checked with correct canonical + synonyms (e.g. `C00271946` myocardial infarction, `C0020538` hypertension, `C0008031` chest pain)
- [ ] `SynonymIndex` loads the scoped artefact; abbreviations resolve (`MI` → `myocardial infarction`)
- [ ] `search.py` end-to-end: a clinical query returns expected documents with sensible explanations
- [ ] QuickUMLS db non-empty (`cui-semtypes.db/` contains data files); medspaCy NER resolves `MI` to a CUI on a test sentence

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Column layout mismatch** (extractor + quickumls assume 18 cols; real releases documented as 39) | Silently wrong dictionary — the worst failure mode | Phase 1 verification *before* any scale run; adapter at the parse boundary |
| ICD-10-CM is licence category 4, absent from Level 0 Subset | Missing `icd10` field under Level 0 | Use Full Subset, or supply `--icd10-map` (NLM's free SNOMED→ICD-10 map) |
| CHV frozen at 2011, all-lowercase | Stale lay wording in synonyms | Trim SAB whitelist if quality suffers; `MEDLINEPLUS` covers current plain language |
| Full Metathesaurus JSON is multi-GB | Disk pressure; EntityRuler bloat if fed to spaCy engine | Scoped artefact for the pipeline; full dump kept as reference only |
| Release churn (NLM ships twice a year) | Names drift between releases | `meta.release` stamped in every output; diff on rebuild |
| Generated dictionary is a first draft | Bad synonyms survive into search | Human review pass over top few hundred concepts by synonym count (`APPROACH.md` §8) |

---

## Open Questions

1. **Which release download will be used** — Level 0 Subset (licence-safe, no ICD-10-CM) or Full Subset (simplest, category 1–4 sources present)?
2. **Pipeline dictionary scope** — clinical kinds only (recommended), or the full unscoped dump?
3. **Abbreviation merge** — confirm extending `SynonymIndex` to read `abbreviations` (recommended: yes).

---

## Execution Log — 2026-09-29/30

Real UMLS release found at `C:\Inference\umls-2026AA-full\2026AA-full` (Full Release; `MRCONSO.RRF` extracted, `MRSTY/MRHIER/MRREL` recovered from the `.nlm` archives — they are plain ZIPs, no MetamorphoSys needed).

- **Phase 1: PASS.** Real 2026AA `MRCONSO.RRF` has exactly the assumed 18-column layout (`CUI LAT TS LUI STT SUI ISPREF AUI SAUI SCUI SDUI SAB TTY CODE STR SRL SUPPRESS CVF`). The 39-column concern did not materialise for this release.
- **Phase 2 (scoped): DONE** — `UMLS_FILTER/extract_heart_attack_graph.py` extracted the heart-attack care graph: core C0027051 + curated clinical seeds + MI ancestry spine + top subtypes + 1-hop MRREL neighbours → **`data/filter/heart_attack_graph.json`: 240 concepts, 25 hierarchy + 248 relationship edges** (diagnosis 70, medication 109, finding 33, procedure 21, allergy 7).
- **Phase 5 (partial): DONE** — `UMLS_FILTER/render_heart_attack_graph.py` → `data/filter/heart_attack_graph.html`, self-contained interactive radial graph (hover tooltips with CUI/types/synonyms/ICD-10, edge labels, category isolation, pan/zoom). Published as an Artifact.
- **Discovery:** this release's `MRSTY.RRF` ships only TUI codes (name columns empty) — the extractor maps codes via a verified `TUI_NAMES` table (anchors: MI=T047 disease, drugs=T121, chest pain=T184, CPR/PCI=T061, echo/angiography=T060 diagnostic procedure; remainder from quickumls' official list).
- **Not yet done:** full unscoped Metathesaurus dump (Phase 2 option B), `SynonymIndex` abbreviation merge (Phase 3), QuickUMLS db rebuild (Phase 4) — all still valid next steps.
