# UMLS → Database: Deterministic Extraction & Realtime Query Algorithm

How to extract **all** UMLS Metathesaurus data, organize it into a queryable database, serve realtime lookups, and repeat the whole thing deterministically for every new NLM release (twice a year).

Grounded in the real **2026AA Full Release** on this machine (`C:\Inference\umls-2026AA-full\2026AA-full`) — every row count, column layout and quirk cited below was verified against those files on 2026-09-29/30, not copied from documentation.

---

## Table of Contents

1. [The RRF Files — What You Are Actually Parsing](#1-the-rrf-files--what-you-are-actually-parsing)
2. [The Determinism Contract](#2-the-determinism-contract)
3. [Extraction Algorithm (Stages S0–S8)](#3-extraction-algorithm-stages-s0s8)
4. [Database Schema](#4-database-schema)
5. [Realtime Query Patterns](#5-realtime-query-patterns)
6. [Worked Example — Querying Everything Related to Heart Attack](#6-worked-example--querying-everything-related-to-heart-attack)
7. [Flow Diagrams](#7-flow-diagrams)
8. [Release Update Procedure (Every New Release)](#8-release-update-procedure-every-new-release)
9. [Verification & Acceptance Checks](#9-verification--acceptance-checks)
10. [Mapping to Existing Project Assets](#10-mapping-to-existing-project-assets)

---

## 1. The RRF Files — What You Are Actually Parsing

### 1.1 Format rules (all RRF files)

- Pipe-delimited (`|`), **no header row** — the column order *is* the schema
- Trailing pipe at end of line (so `split('|')` yields one empty field)
- UTF-8; empty fields are simply nothing between two pipes
- Large tables ship as **split parts**: `MRHIER.RRF.aa.gz`, `.ab.gz`, … — concatenate in alphabetical order, they are one logical file
- Mis-ordering columns fails **silently** (you read term types as source codes and get a plausible-looking wrong database). This is the #1 regression risk on every release — see §8.2

### 1.2 The identifier ladder (the single most important idea)

UMLS names things at four levels; every join in the whole system uses one of these:

```
CUI   C0027051   CONCEPT — the unit of synonymy. Everything sharing a CUI is a name for the same thing.
 └─ LUI  L…       TERM    — lexical variants of one name ("Heart Attack" / "heart attacks")
     └─ SUI  S…   STRING  — one exact string in one language, case included
         └─ AUI  A…  ATOM  — that string as asserted by ONE specific source vocabulary
```

**Group by CUI to get synonyms. Join on AUI to get hierarchy.** LUI/SUI can be ignored for search; they matter for normalisation and deduplication.

### 1.3 The core files (verified 2026AA facts)

| File | Size / rows (2026AA, verified) | What it is | Role in the DB |
|---|---|---|---|
| **MRCONSO.RRF** | 2.34 GB · **18.1 M rows** | One row per **atom**: one name for one concept from one source. The workhorse — concepts, synonyms, abbreviations, ICD-10/RxNorm codes all come from here | `concepts`, `terms` |
| **MRSTY.RRF** | 73 MB · 3.9 M rows → **3,530,466 CUIs** | CUI → semantic type assignment (a concept can have several types) | `semantic_types`, `concept_semantic_types` |
| **MRHIER.RRF** | ~1.5 GB · **45 M rows** (5 split parts) | One row per **hierarchy path**: where an atom sits in its source's IS-A tree (`PTR` = dot-joined chain of ancestor AUIs to root) | `hierarchy` |
| **MRREL.RRF** | ~4 GB · **66 M rows** (5 split parts) | Concept-to-concept relationships: narrower/broader/other/synonym, with a human label | `relationships` |
| **MRSAB.RRF** | 150 KB | Source vocabulary definitions (SNOMEDCT_US, MSH, RXNORM, ICD10CM…) | `sources` |
| **MRDEF.RRF** | — | Free-text concept definitions (MeSH-style) | optional `definitions` |
| **MRCUI.RRF** | — | Plain index of every CUI in the release | cross-check count |
| **MRMAP / MRSMAP** | — | Crosswalks between UMLS and source identifiers | optional |
| **AMBIGSUI / AMBIGLUI** | — | Ambiguity tables (string → multiple terms) | feeds abbreviation disambiguation |
| **MRXNS_ENG / MRXNW_ENG** | — | SPECIALIST lexical tools (normalisation, word index) — Full Release only | optional fuzzy matching |

### 1.4 Verified column layouts (2026AA)

These were read off the real files, not assumed:

```
MRCONSO (18 cols):
  CUI LAT TS LUI STT SUI ISPREF AUI SAUI SCUI SDUI SAB TTY CODE STR SRL SUPPRESS CVF
  └─0──┴─1──┴─2──┴─3──┴─4──┴─5──┴─6──┴─7──┴─8──┴─9──┴─10─┴─11─┴─12─┴─13─┴─14─┴─15─┴─16──┴─17─
  key fields: 0=CUI  1=LAT(ENG)  2=TS(P/S)  4=STT(PF/VC…)  6=ISPREF(Y/N)  7=AUI
              11=SAB  12=TTY  13=CODE  14=STR  16=SUPPRESS(N usable)

MRSTY (6 cols):   CUI TUI STN STY ATUI CVF
  ⚠ 2026AA ships ONLY the TUI code — STN/STY name columns are EMPTY.
     You must carry a TUI→name table (see §3, stage S1).

MRHIER (9 cols):  CUI AUI CXN PAUI SAB RELA PTR HCD CVF
  key fields: 0=CUI  1=AUI  3=PAUI(parent atom)  6=PTR(ancestor AUI chain, root→atom)

MRREL (verified first 11 cols):
  CUI1 AUI1 REL_LEVEL REL CUI2 AUI2 REL_LEVEL RELA R_CODE ? SAB
  └0───┴1────┴2───────┴3───┴4────┴5────┴6───────┴7─────┴8─────┴9─┴10──
  REL codes seen in data: RN(narrower) RB(broader) RO(other) SY(synonym/crosswalk)
                          CHD(child) PAR(parent) + source-specific (RQ, AQ, QB, RL…)
  RELA = human label ("isa", "associated_with", "translation_of", "mapped_from")
  ⚠ REL=SY rows are cross-source EQUIVALENCE links (MSH↔SNOMED), not clinical
    relationships — keep them in the table but filter them out of clinical queries.
```

### 1.5 What "all UMLS data" means for a meaningful database

Not every file earns a table. The set that makes the data **usable** is:

| Need | Source | Table |
|---|---|---|
| "What is this concept called?" (any language of use) | MRCONSO grouped by CUI | `concepts` + `terms` |
| "What kind of thing is it?" (disease / drug / procedure / finding…) | MRSTY | `semantic_types` join |
| "How specific is it? Where does it sit?" (MI → ischemic heart disease → …) | MRHIER | `hierarchy` |
| "What relates to what?" (MI ↔ ACS, MI ↔ arteriosclerosis, drug↔drug) | MRREL | `relationships` |
| "Which vocabulary said so?" (audit any claim) | MRCONSO.SAB / MRSAB | `sources`, provenance columns |
| "Map to billing / drug codes" | MRCONSO.CODE where SAB=ICD10CM/RXNORM | `icd10` / `rxcui` on `concepts` |

Everything else (MRDEF, MRMAP, SPECIALIST) is optional enrichment — add tables only when a consumer asks for them.

---

## 2. The Determinism Contract

"Same release files in → byte-identical database out." Concretely, the pipeline must honour:

1. **Explicit ordering everywhere.** Every sort has a written key. Default collation:
   `lowercase(term) → length → codepoint`. No reliance on hash order, file order, or dict insertion order for *output* (insertion order is fine internally).
2. **No timestamps in data tables.** Wall-clock values live only in `build_manifest` (which build produced this file, from which release, checksums of inputs).
3. **Fixed decision tables.** SAB preference order, TTY whitelists, TUI→name map are constants in code/config — never derived from data at runtime. Changing them is a versioned code change, not a data accident.
4. **Idempotent rebuild.** Re-running on the same inputs produces the same rows. Rebuilds write to a **new** database file and swap atomically (§8) — never in-place mutation of the live DB.
5. **Bounded memory.** Every big table is streamed line-by-line; nothing is loaded whole except MRSTY/MRSAB (both < 100 MB).
6. **Reproducible provenance.** `build_manifest` records release tag, per-file SHA-256 and row counts — any database can be traced back to exact inputs.

---

## 3. Extraction Algorithm (Stages S0–S8)

Stage order is forced by the joins: MRHIER's `PTR`/`PAUI` are AUIs, which only mean something after MRCONSO has told you which CUI owns each AUI.

```
S0 verify ─► S1 types ─► S2 sources ─► S3 concepts+terms ─► S4 hierarchy
                                                        └─► S5 relationships
        ─► S6 derived indexes ─► S7 load (transaction) ─► S8 verify
```

### S0 — Verify inputs

- Check the release's shipped checksum file (`2026AA.MD5` is present in the release folder) against each RRF you will read; recompute SHA-256 as well and store both.
- **Layout regression check** (the silent-failure guard): for each file, assert the expected column count on a sample of rows, and spot-check ≥ 3 known CUIs (e.g. `C0027051` must carry "Myocardial infarction"; aspirin `C0004057` must be type T121). If any assertion fails → **abort with a loud error**, never guess columns.
- Record everything in `build_manifest`.

### S1 — Semantic types (MRSTY)

- Stream MRSTY → `cui → [TUI codes]`.
- Map codes → names via the fixed `TUI_NAMES` table (2026AA ships codes only — verified). Unknown codes are kept as raw codes, never dropped, and reported in the build log.
- Output: `semantic_types(tui, name)` + `concept_semantic_types(cui, tui)`.

### S2 — Sources (MRSAB)

- Trivial load → `sources(sab, name, description)`. Also fixes the SAB preference list used in S3 (constant in code; MRSAB is only for display names).

### S3 — Concepts and terms (MRCONSO) — the big one

Single streaming pass over 18.1 M rows:

```
for each row:
    keep if LAT=ENG and SUPPRESS=N and 2 ≤ len(STR) ≤ 80
    bucket by CUI:
        canonical candidate  ← row with TS=P, STT=PF, ISPREF=Y
        abbreviations        ← TTY ∈ {AB, ACR, AA, AS}
        brands (drugs)       ← TTY=BN, SAB=RXNORM          (kept apart from synonyms)
        dose-level products  ← TTY ∈ {SCD,SBD,PSN,…}       (discarded — never names)
        icd10                ← CODE where SAB=ICD10CM      (first 3 chars = chapter)
        rxcui                ← CODE where SAB=RXNORM, TTY ∈ {IN,PIN,MIN}
        synonyms             ← everything else name-like   (TTY whitelist)
        aui_map[AUI] = CUI   ← needed by S4/S5
```

**Canonical selection (deterministic):** among preferred rows, pick by SAB rank
(`MTH > SNOMEDCT_US > MSH > NCI > RXNORM > LNC > …` — fixed list), tie-break lexicographically.
Never "shortest string" as a primary rule — it picks acronyms.

Output: `concepts(cui, canonical_name, category, kind, icd10, rxcui)` + `terms(term, term_norm, cui, aui, sab, tty, is_abbrev, is_brand)`.
`category`/`kind` are derived from semantic types via the fixed STY→category table (drug/substance → medication; therapeutic+diagnostic procedure → procedure; sign/symptom+finding → finding; disease/neoplasm/… → diagnosis; allergy text override → allergy).

### S4 — Hierarchy (MRHIER)

- Stream 45 M rows. For each row: `parent = aui_map[PAUI]`, `child = CUI` (when both resolve and parent ≠ child) → edge `(parent_cui, child_cui, depth, sab, rela)`.
- `depth` = number of AUIs in `PTR` (path length to root).
- **Dedup rule:** keep one row per `(parent, child, sab)` with the minimum `CXN`; discard self-loops.
- Output: `hierarchy(parent_cui, child_cui, depth, sab, rela)`.

### S5 — Relationships (MRREL)

- Stream 66 M rows. Keep `(cui1, cui2, rel, rela)` where both CUIs exist in `concepts`.
- **Dedup rule:** one row per `(cui1, cui2, rel, rela)`; store direction as given (RN: cui1 is narrower than cui2).
- Tag `rel='SY'` rows as crosswalks (queryable but excluded from clinical relationship views by default).
- Output: `relationships(cui1, cui2, rel, rela)`.

### S6 — Derived indexes (the "meaningful use" layer)

All computed from S3–S5, all deterministic:

| Index | Rule | Powers |
|---|---|---|
| `term_lookup(term_norm, cui, rank)` | one row per (term, concept); `rank` = SAB rank then TTY class (preferred name < synonym < abbreviation) | realtime term→concept (§5 Q1) |
| `abbreviation_ambiguity(term_norm, n_cuis)` | count distinct CUIs per short uppercase term; flag `n > 3` as ambiguous | safe acronym linking (Q6) |
| `icd10_chapter(code, chapter_name)` | ICD-10 letter → body system (I→cardiovascular, R→symptom, …) | category fallback without hand-tagging |
| FTS index over `terms.term` | SQLite FTS5 / Postgres trigram | prefix & fuzzy search |

### S7 — Load

- Build into a **fresh** database file (`umls_<release>.db`) inside one transaction per table; create indexes after data load (faster).
- SQLite: enable WAL, `PRAGMA synchronous=NORMAL` during build. Postgres: same SQL, add `CREATE INDEX CONCURRENTLY` for the big ones.
- Swap only after S8 passes (§8).

### S8 — Verify

- Row counts vs `build_manifest` expectations (MRCONSO atoms in = terms out + discarded, with discard reasons counted).
- Referential integrity: every `terms.cui`, `hierarchy.*cui`, `relationships.*cui` exists in `concepts`.
- Spot-check battery (fixed list, asserted every build): C0027051 canonical = "myocardial infarction" with synonyms incl. "heart attack"; aspirin has ICD-free drug record; chest pain type = Sign or Symptom; CPR → Therapeutic or Preventive Procedure.
- Emit a build report (JSON + markdown) — this is the artefact reviewers read.

---

## 4. Database Schema

SQLite DDL (Postgres: same shapes, swap types; `TEXT`→`text`, add `BIGINT` ids if you like):

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE build_manifest (
  release_tag   TEXT PRIMARY KEY,          -- '2026AA'
  built_at      TEXT NOT NULL,             -- the ONLY timestamp in the DB
  file_sha256   TEXT,                      -- JSON: {filename: sha256}
  row_counts    TEXT                       -- JSON: {table: count}
);

CREATE TABLE sources (
  sab TEXT PRIMARY KEY, name TEXT, description TEXT
);

CREATE TABLE semantic_types (
  tui TEXT PRIMARY KEY, name TEXT          -- TUI_NAMES table, versioned in code
);

CREATE TABLE concepts (
  cui TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  category TEXT,                           -- medication|diagnosis|procedure|finding|allergy|anatomy|other
  kind TEXT,                               -- disorder|symptom|drug|…
  icd10 TEXT, rxcui TEXT,
  preferred_sab TEXT                       -- provenance of the canonical choice
);
CREATE INDEX idx_concepts_name ON concepts(lower(canonical_name));
CREATE INDEX idx_concepts_cat  ON concepts(category);

CREATE TABLE concept_semantic_types (
  cui TEXT NOT NULL, tui TEXT NOT NULL, PRIMARY KEY (cui, tui)
);

CREATE TABLE terms (
  term_id INTEGER PRIMARY KEY,
  term TEXT NOT NULL, term_norm TEXT NOT NULL,
  cui TEXT NOT NULL REFERENCES concepts(cui),
  aui TEXT, sab TEXT, tty TEXT,
  is_abbrev INTEGER DEFAULT 0, is_brand INTEGER DEFAULT 0
);
CREATE INDEX idx_terms_cui ON terms(cui);

-- realtime lookup: one row per (term, concept), ranked deterministically
CREATE TABLE term_lookup (
  term_norm TEXT NOT NULL, cui TEXT NOT NULL, rank INTEGER NOT NULL,
  PRIMARY KEY (term_norm, cui)
);
CREATE INDEX idx_term_lookup_rank ON term_lookup(term_norm, rank);

CREATE VIRTUAL TABLE terms_fts USING fts5(term, content='terms', content_rowid='term_id');

CREATE TABLE hierarchy (
  parent_cui TEXT NOT NULL, child_cui TEXT NOT NULL,
  depth INTEGER, sab TEXT, rela TEXT,
  PRIMARY KEY (parent_cui, child_cui, sab)
);
CREATE INDEX idx_hier_child ON hierarchy(child_cui);

CREATE TABLE relationships (
  cui1 TEXT NOT NULL, cui2 TEXT NOT NULL,
  rel TEXT NOT NULL,                       -- RN|RB|RO|SY|CHD|PAR|…
  rela TEXT,                               -- 'isa', 'associated_with', …
  PRIMARY KEY (cui1, cui2, rel, rela)
);
CREATE INDEX idx_rel_cui2 ON relationships(cui2);
```

**Scale expectations (2026AA):** concepts ≈ 3.5 M · terms ≈ 15–18 M · hierarchy ≈ a few M deduped edges · relationships ≈ several M deduped. SQLite handles this comfortably (tens of GB, sub-ms indexed lookups); if you outgrow it, the same schema moves to Postgres unchanged.

---

## 5. Realtime Query Patterns

All are indexed lookups or bounded traversals — no full scans on the hot path.

**Q1 — Term → concept + all its names** (the NER/linking call; < 1 ms)
```sql
SELECT c.cui, c.canonical_name, c.category, t.term, t.is_abbrev
FROM term_lookup tl
JOIN concepts c ON c.cui = tl.cui
LEFT JOIN terms t ON t.cui = tl.cui AND t.term_norm = tl.term_norm
WHERE tl.term_norm = ?            -- e.g. 'mi'
ORDER BY tl.rank;
```

**Q2 — Concept → full record** (types, codes, provenance)
```sql
SELECT c.*, group_concat(st.name) FROM concepts c
LEFT JOIN concept_semantic_types cst ON cst.cui = c.cui
LEFT JOIN semantic_types st ON st.tui = cst.tui
WHERE c.cui = ? GROUP BY c.cui;
```

**Q3 — Ancestors / descendants, depth-bounded** (hierarchy walk)
```sql
-- ancestors of C0027051 up to 6 levels (SQLite recursive CTE)
WITH RECURSIVE anc(cui, d) AS (
  SELECT parent_cui, 1 FROM hierarchy WHERE child_cui = ?
  UNION
  SELECT h.parent_cui, a.d + 1 FROM hierarchy h JOIN anc a ON a.cui = h.child_cui WHERE a.d < 6
) SELECT c.canonical_name, MIN(a.d) FROM anc a JOIN concepts c ON c.cui = a.cui GROUP BY a.cui;
```

**Q4 — Clinical relationships of a concept** (exclude crosswalks by default)
```sql
SELECT cui1, cui2, rel, rela FROM relationships
WHERE (cui1 = ? OR cui2 = ?) AND rel != 'SY'
ORDER BY CASE rel WHEN 'RN' THEN 0 WHEN 'RB' THEN 1 ELSE 2 END;
```

**Q5 — Category browse** ("all drugs", "all procedures")
```sql
SELECT canonical_name, icd10 FROM concepts WHERE category = ? ORDER BY canonical_name;
```

**Q6 — Abbreviation disambiguation before linking** (the precision guard)
```sql
SELECT term_norm, COUNT(DISTINCT cui) n FROM terms
WHERE is_abbrev = 1 AND term_norm = ? GROUP BY term_norm;   -- n > 3 → refuse auto-link
```

Latency budget: Q1/Q2/Q5/Q6 sub-millisecond (single index probe); Q3/Q4 bounded by depth/edge count — keep `d < 8` and top-N limits in the API layer.

---

## 6. Worked Example — Querying Everything Related to Heart Attack

This is what the schema is for: starting from one anchor concept (**myocardial infarction, C0027051**), pull every related concept across **diagnosis, drugs, procedures, findings and allergies** — using only the tables in §4. Every query below was executed against SQLite on a mini dataset mirroring the real 2026AA extraction (CUIs and relationship labels taken from `data/filter/heart_attack_graph.json`); expected outputs are shown.

### 6.1 Anchor the core concept

```sql
SELECT c.cui, c.canonical_name, c.icd10
FROM term_lookup tl JOIN concepts c ON c.cui = tl.cui
WHERE tl.term_norm IN ('myocardial infarction', 'heart attack')
ORDER BY tl.rank;
-- → C0027051  Myocardial Infarction  I21     (rank 1 = preferred name first)
```

### 6.2 All subtypes — what MI includes (descendants, depth-bounded)

```sql
WITH RECURSIVE sub(cui, d) AS (
  SELECT child_cui, 1 FROM hierarchy WHERE parent_cui = 'C0027051'
  UNION
  SELECT h.child_cui, s.d + 1 FROM hierarchy h JOIN sub s ON s.cui = h.parent_cui WHERE s.d < 4
)
SELECT c.canonical_name, MIN(s.d) AS depth
FROM sub s JOIN concepts c ON c.cui = s.cui
GROUP BY s.cui ORDER BY depth, canonical_name;
-- → Acute Anterior Wall MI · Non-ST Elevated MI (NSTEMI) · Silent MI · …
```

### 6.3 Clinical context — where MI sits (ancestors)

```sql
WITH RECURSIVE anc(cui, d) AS (
  SELECT parent_cui, 1 FROM hierarchy WHERE child_cui = 'C0027051'
  UNION
  SELECT h.parent_cui, a.d + 1 FROM hierarchy h JOIN anc a ON a.cui = h.child_cui WHERE a.d < 6
)
SELECT c.canonical_name, MIN(a.d)
FROM anc a JOIN concepts c ON c.cui = a.cui
GROUP BY a.cui ORDER BY 2;
-- → Ischemic heart disease → Cardiovascular diseases → … (root chain)
```

### 6.4 Directly related concepts, with the relationship label

```sql
SELECT c.canonical_name, c.category, r.rel, r.rela
FROM relationships r
JOIN concepts c ON c.cui = CASE WHEN r.cui1 = 'C0027051' THEN r.cui2 ELSE r.cui1 END
WHERE (r.cui1 = 'C0027051' OR r.cui2 = 'C0027051') AND r.rel != 'SY'
ORDER BY CASE r.rel WHEN 'RN' THEN 0 WHEN 'RB' THEN 1 ELSE 2 END, c.canonical_name;
-- → Acute Coronary Syndrome   diagnosis   PAR  inverse_isa
-- → Atherosclerosis           diagnosis   RO   associated_with
-- → Troponin                  finding     RO   diagnosed_by
-- → ST segment elevation      finding     RO   diagnosed_by
-- → aspirin                   medication  RO   used_for
-- → clopidogrel               medication  RO   used_for
```

`r.rel != 'SY'` is deliberate: `SY` rows are cross-source equivalence links (MSH↔SNOMED), not clinical relationships.

### 6.5 Drugs — two honest paths

**(a) Drugs linked to MI or any of its subtypes** (relationship-driven):

```sql
WITH RECURSIVE mi_set(cui) AS (
  SELECT 'C0027051'
  UNION
  SELECT h.child_cui FROM hierarchy h JOIN mi_set s ON s.cui = h.parent_cui
)
SELECT DISTINCT c.canonical_name, r.rel, r.rela
FROM relationships r
JOIN concepts c ON c.cui = CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END
WHERE c.category = 'medication' AND r.rel != 'SY'
  AND (r.cui1 IN (SELECT cui FROM mi_set) OR r.cui2 IN (SELECT cui FROM mi_set))
ORDER BY canonical_name;
-- → aspirin (used_for) · clopidogrel (used_for) · …
```

**(b) The full drug catalog** (browse, not relationship-driven):

```sql
SELECT canonical_name, icd10 FROM concepts WHERE category = 'medication' ORDER BY canonical_name;
```

> **Honest limitation:** UMLS core does *not* encode "drug X treats disease Y" as a first-class relationship — the `used_for`-style links above come from source vocabularies (NCI etc.) and are incomplete. For authoritative indications, add one table alongside S5:
> `indications(drug_cui, disease_cui, source)` loaded from RxNorm/DrugBank; every query in this section then gains a `UNION` branch unchanged.

### 6.6 Procedures for heart attack

Same pattern as 6.5a with `category = 'procedure'`:

```sql
-- (mi_set CTE as in 6.5a)
SELECT DISTINCT c.canonical_name, r.rela
FROM relationships r
JOIN concepts c ON c.cui = CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END
WHERE c.category = 'procedure' AND r.rel != 'SY'
  AND (r.cui1 IN (SELECT cui FROM mi_set) OR r.cui2 IN (SELECT cui FROM mi_set))
ORDER BY canonical_name;
-- → Percutaneous Coronary Intervention (treated_with) · Cardiopulmonary Resuscitation · …
```

### 6.7 Allergies relevant to heart-attack care

Allergy concepts that sit in the MI network — directly, or via one of MI's drugs (e.g. *allergy to aspirin*):

```sql
WITH RECURSIVE mi_set(cui) AS (
  SELECT 'C0027051'
  UNION
  SELECT h.child_cui FROM hierarchy h JOIN mi_set s ON s.cui = h.parent_cui
),
mi_drugs(cui) AS (
  SELECT DISTINCT CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END
  FROM relationships r
  WHERE (r.cui1 IN (SELECT cui FROM mi_set) OR r.cui2 IN (SELECT cui FROM mi_set))
    AND r.rel != 'SY'
    AND EXISTS (SELECT 1 FROM concepts c
                WHERE c.category = 'medication'
                  AND c.cui = CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END)
)
SELECT c.canonical_name,
       CASE WHEN c.cui IN (SELECT cui FROM mi_drugs) THEN 'allergy-to-mi-drug' ELSE 'network' END AS link
FROM concepts c
WHERE c.category = 'allergy'
  AND (c.cui IN (SELECT cui FROM mi_drugs)
       OR EXISTS (SELECT 1 FROM relationships r WHERE r.rel != 'SY'
                  AND ((r.cui1 = c.cui AND r.cui2 IN (SELECT cui FROM mi_set))
                    OR (r.cui2 = c.cui AND r.cui1 IN (SELECT cui FROM mi_set))
                    OR (r.cui1 = c.cui AND r.cui2 IN (SELECT cui FROM mi_drugs))
                    OR (r.cui2 = c.cui AND r.cui1 IN (SELECT cui FROM mi_drugs)))))
ORDER BY c.canonical_name;
-- → Allergy to aspirin · Hypersensitivity · anaphylaxis · …
```

Note: *heparin-induced thrombocytopenia* is categorised as a **diagnosis**, not an allergy — it surfaces via 6.4/6.5 instead, which is the clinically correct place for it.

### 6.8 Findings and labs that diagnose MI

```sql
SELECT c.canonical_name, r.rela
FROM relationships r JOIN concepts c ON c.cui = CASE WHEN r.cui1 = 'C0027051' THEN r.cui2 ELSE r.cui1 END
WHERE (r.cui1 = 'C0027051' OR r.cui2 = 'C0027051') AND c.category = 'finding' AND r.rel != 'SY'
ORDER BY canonical_name;
-- → Troponin (diagnosed_by) · ST segment elevation (diagnosed_by)
```

### 6.9 The full bundle in one query

Everything above, faceted, in a single statement:

```sql
WITH RECURSIVE mi_set(cui) AS (
  SELECT 'C0027051'
  UNION
  SELECT h.child_cui FROM hierarchy h JOIN mi_set s ON s.cui = h.parent_cui
),
related(cui, rel, rela) AS (
  SELECT CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END, r.rel, r.rela
  FROM relationships r
  WHERE (r.cui1 IN (SELECT cui FROM mi_set) OR r.cui2 IN (SELECT cui FROM mi_set)) AND r.rel != 'SY'
)
SELECT 'subtype' AS facet, c.canonical_name, NULL AS rel, NULL AS rela
FROM mi_set s JOIN concepts c ON c.cui = s.cui WHERE s.cui != 'C0027051'
UNION ALL
SELECT 'related:' || r.rel, c.canonical_name, r.rel, r.rela
FROM related r JOIN concepts c ON c.cui = r.cui
WHERE c.category IN ('diagnosis','medication','procedure','finding','allergy')
ORDER BY facet, canonical_name;
```

On the mini test dataset this returns 10 rows (3 subtypes + 7 related); on the full 2026AA database it is exactly the "heart-attack care graph" already extracted to `data/filter/heart_attack_graph.json` (240 concepts, 273 edges) — the difference being that here it is a **live query**, not a precomputed file.

### 6.10 What this covers and what it doesn't

| Covered by the schema | Not covered (and where to extend) |
|---|---|
| Every name for every concept, ranked (Q1/§6.1) | Drug **indications & dosing** → `indications` table from RxNorm/DrugBank |
| Concept type, category, ICD-10, RxNorm code (Q2) | Lab reference ranges → LOINC value-set tables |
| Hierarchy up/down, depth-bounded (Q3/§6.2–6.3) | Guideline text → separate document store, linked by CUI |
| Labeled relationships incl. drugs/procedures/allergies (§6.4–6.8) | Negation/context — that's the NLP layer's job, above the DB |
| Abbreviation ambiguity guard (Q6) | Multilingual names — already in `terms` (LAT≠ENG rows), just not indexed by default |

---

## 7. Flow Diagrams

### 7.1 Build pipeline

```
                ┌─────────────────────────── UMLS release (2026AA) ───────────────────────────┐
                │                                                                              │
   MRCONSO.RRF  │  2.34 GB / 18.1M rows        MRSTY 73 MB          MRHIER 45M rows           │
   (atoms)      │                              (CUI→TUI)            (AUI paths)               │
                │                              MRSAB 150 KB         MRREL 66M rows            │
                └──────┬───────────────────────────┬──────────────────────┬───────────────────┘
                       ▼                           ▼                      ▼
        S0 verify: checksums + column-layout regression check + known-CUI spot checks
                       │          (abort loudly on any mismatch — never guess columns)
                       ▼
   S1 MRSTY ──► semantic_types / concept_semantic_types      (TUI→name via fixed table)
   S2 MRSAB ──► sources
   S3 MRCONSO ─► concepts + terms + aui_map                  (single streaming pass)
                       │            canonical = preferred row → SAB rank → lexicographic
                       ▼
   S4 MRHIER ──► hierarchy(parent, child, depth)             (needs aui_map from S3)
   S5 MRREL  ──► relationships(cui1, cui2, rel, rela)        (SY tagged as crosswalk)
                       │
                       ▼
   S6 derived: term_lookup(rank) · abbreviation_ambiguity · icd10_chapter · FTS5
                       │
                       ▼
   S7 load into FRESH umls_<release>.db (per-table transactions, indexes last)
                       │
                       ▼
   S8 verify: counts vs manifest · referential integrity · fixed spot-check battery
                       │
                       ▼
              build report (JSON + markdown)  ──►  human review  ──►  atomic swap (§8)
```

### 7.2 Realtime query path (e.g. clinical search NER)

```
query text: "no chest pain, MI ruled out"
        │
        ▼
tokenize / span candidates ("mi", "chest pain")
        │
        ▼
Q1 term_lookup(term_norm)          ──►  CUI + canonical + category     < 1 ms
        │            (rank-ordered; abbreviations flagged)
        ▼
Q6 ambiguity check for short forms ──►  "MI" → 1 CUI? link : defer to context
        │
        ▼
Q2 concept record (types, icd10)   ──►  feeds scoring / explanation    < 1 ms
        │
        ▼
Q4 relationships (optional, top-N) ──►  related concepts for expansion
        │
        ▼
linked entities: [{cui, canonical, category, negated}]  ──►  retrieval pipeline
```

### 7.3 Release update cycle (twice a year)

```
NLM ships 2026AB (Jan / Jul)
        │
        ▼
1 download + verify shipped MD5/SHA-256
        │
        ▼
2 S0 layout regression check ──► FAIL? stop, adapt column map in code, re-run
        │ PASS
        ▼
3 deterministic rebuild  ──►  umls_2026AB.db   (fresh file, live DB untouched)
        │
        ▼
4 diff report: concepts/terms/edges added · removed · changed
   (CUIs are stable across releases → diffs are meaningful; names move)
        │
        ▼
5 review diff (auto-generated markdown) — sign-off
        │
        ▼
6 atomic swap:  umls_live.db → umls_2026AA.db.prev
                umls_2026AB.db → umls_live.db          (rename = atomic on one volume)
        │
        ▼
7 smoke tests (Q1–Q6 battery) ──► FAIL? swap back (rollback = one rename)
        │ PASS
        ▼
8 archive .prev after 30 days; log build_manifest entry
```

---

## 8. Release Update Procedure (Every New Release)

NLM ships **twice a year** (`2026AA` → `2026AB` → `2027AA`…). CUIs are stable by design, so the same concept keeps its id; what moves is names, synonymy, types and relationships. The update is therefore a **rebuild + diff + swap**, never a patch:

1. **Download** the new release (Level 0 Subset if you ship derived data — ICD-10-CM is licence category 4 and absent from Level 0; Full Subset for maximum coverage). Verify the shipped checksum file, then record SHA-256 of every file you read.
2. **Layout regression check first** (§3 S0). Releases have changed formats before; the silent-failure mode is reading `SAB` where `LAT` used to be. The known-CUI spot-check battery catches it in seconds. If the layout moved, update the column map **in code** (one place), commit, re-run.
3. **Rebuild** into a fresh database file. Determinism (§2) means the only differences between builds are real data changes — which is exactly what you want to see in the diff.
4. **Diff** old vs new: concepts added/removed; canonical name changes; synonym set changes per CUI; hierarchy/relationship edge deltas. Emit markdown + JSON. A healthy release shows hundreds-to-thousands of small changes, not tens of thousands (if it does, suspect a parsing regression before believing the data).
5. **Review** the diff report — this is the human gate. Pay attention to: canonical name flips on high-traffic concepts, abbreviation ambiguity changes, new `SY` crosswalks.
6. **Swap atomically** (rename on the same volume). The application reads a stable path (`umls_live.db`), so cutover is instant and zero-downtime under WAL.
7. **Rollback** is one rename back — keep the previous build for 30 days.
8. **Schedule it:** put a calendar/cron reminder ~1 week after each NLM release date (January and July). The whole cycle is scripted end-to-end; human time is step 5 only.

---

## 9. Verification & Acceptance Checks

Run on every build (S8) — all must pass before swap:

- [ ] Checksums of all input files match the release's shipped manifest
- [ ] Column-count + known-CUI spot checks pass (C0027051 = "myocardial infarction" + "heart attack"; aspirin C0004057 drug record; chest pain C0008031 = Sign or Symptom; CPR C0007203 = Therapeutic or Preventive Procedure)
- [ ] `terms` row count = kept atoms (LAT=ENG, SUPPRESS=N, length bounds) — discard reasons counted and reported
- [ ] Referential integrity: no orphan CUIs in `terms`, `hierarchy`, `relationships`, `concept_semantic_types`
- [ ] Q1–Q6 smoke queries return expected results with p95 < 5 ms (local disk)
- [ ] Diff vs previous release reviewed and signed off (first build: skip diff, keep report)

---

## 10. Mapping to Existing Project Assets

| Asset | Relationship to this design |
|---|---|
| `UMLS_FILTER/build_concept_dictionary.py` | Proves the streaming-parse approach on real 2026AA data; its SAB/TTY decision tables are the same constants S3 uses |
| `UMLS_FILTER/extract_heart_attack_graph.py` | Worked example of S3+S4+S5 on a scoped concept set (240 concepts, 273 edges) — the seed for the spot-check battery |
| `UMLS_FILTER/RRF_REFERENCE.md` | Column-level reference; update it in the same commit as any column-map change |
| `src/nlp.py` `SynonymIndex` / `UMLSConceptIndex` | The realtime consumers: Q1+Q2 replace the in-memory JSON dictionary with DB-backed lookups (same interface, no caller changes) |
| `data/synonyms.json` | Becomes a *view* over `term_lookup` + `concepts` — hand-maintained file retired once the DB is live |
| `Architecture/umls_concept_extraction_plan.md` | Phases 2–4 of that plan are this document's S3–S8; the execution log there records what was verified on 2026AA |

**Build order suggestion:** (1) S0–S3 + Q1/Q2 behind `SynonymIndex` → immediate search-quality win; (2) S4/S5 + Q3/Q4 → relationship-aware expansion; (3) S6 FTS + Q6 → robust acronym handling. Each step is independently shippable and rollback-safe by construction.
