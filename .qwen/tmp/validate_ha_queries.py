"""Validate the heart-attack worked-example queries from the README against real SQLite.

Dataset mirrors the real 2026AA extraction (CUIs, categories, relationship
labels all taken from data/heart_attack_graph.json).
"""
import sqlite3

con = sqlite3.connect(":memory:")
DDL = """
CREATE TABLE build_manifest (release_tag TEXT PRIMARY KEY, built_at TEXT NOT NULL, file_sha256 TEXT, row_counts TEXT);
CREATE TABLE sources (sab TEXT PRIMARY KEY, name TEXT, description TEXT);
CREATE TABLE semantic_types (tui TEXT PRIMARY KEY, name TEXT);
CREATE TABLE concepts (cui TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, category TEXT, kind TEXT, icd10 TEXT, rxcui TEXT, preferred_sab TEXT);
CREATE INDEX idx_concepts_name ON concepts(lower(canonical_name));
CREATE INDEX idx_concepts_cat ON concepts(category);
CREATE TABLE concept_semantic_types (cui TEXT NOT NULL, tui TEXT NOT NULL, PRIMARY KEY (cui, tui));
CREATE TABLE terms (term_id INTEGER PRIMARY KEY, term TEXT NOT NULL, term_norm TEXT NOT NULL, cui TEXT NOT NULL REFERENCES concepts(cui), aui TEXT, sab TEXT, tty TEXT, is_abbrev INTEGER DEFAULT 0, is_brand INTEGER DEFAULT 0);
CREATE INDEX idx_terms_cui ON terms(cui);
CREATE TABLE term_lookup (term_norm TEXT NOT NULL, cui TEXT NOT NULL, rank INTEGER NOT NULL, PRIMARY KEY (term_norm, cui));
CREATE INDEX idx_term_lookup_rank ON term_lookup(term_norm, rank);
CREATE TABLE hierarchy (parent_cui TEXT NOT NULL, child_cui TEXT NOT NULL, depth INTEGER, sab TEXT, rela TEXT, PRIMARY KEY (parent_cui, child_cui, sab));
CREATE INDEX idx_hier_child ON hierarchy(child_cui);
CREATE TABLE relationships (cui1 TEXT NOT NULL, cui2 TEXT NOT NULL, rel TEXT NOT NULL, rela TEXT, PRIMARY KEY (cui1, cui2, rel, rela));
CREATE INDEX idx_rel_cui2 ON relationships(cui2);
"""
con.executescript(DDL)

# ── mini dataset (real CUIs from the 2026AA extraction) ─────────────────────
concepts = [
    ("C0027051", "Myocardial Infarction", "diagnosis", "disorder", "I21"),
    ("C2349195", "Acute Anterior Wall Myocardial Infarction", "diagnosis", "disorder", "I21"),
    ("C4255010", "Non-ST Elevated Myocardial Infarction", "diagnosis", "disorder", "I21"),
    ("C0340324", "Silent myocardial infarction", "diagnosis", "disorder", None),
    ("C0948089", "Acute Coronary Syndrome", "diagnosis", "disorder", None),
    ("C0007222", "Cardiovascular Diseases", "diagnosis", "disorder", None),
    ("C0004153", "Atherosclerosis", "diagnosis", "disorder", None),
    ("C0004057", "aspirin", "medication", "drug", None),
    ("C0019134", "heparin", "medication", "drug", None),
    ("C0070166", "clopidogrel", "medication", "drug", None),
    ("C1532338", "Percutaneous Coronary Intervention", "procedure", None, None),
    ("C0007203", "Cardiopulmonary Resuscitation", "procedure", None, None),
    ("C0004058", "Allergy to aspirin", "allergy", None, None),
    ("C0020517", "Hypersensitivity", "allergy", None, None),
    ("C0041199", "Troponin", "finding", None, None),
    ("C0520886", "ST segment elevation (finding)", "finding", None, None),
]
con.executemany("INSERT INTO concepts VALUES (?,?,?,?,?,NULL,?)",
                [(c[0], c[1], c[2], c[3], c[4], "MTH") for c in concepts])

terms = [
    ("Myocardial infarction", "myocardial infarction", "C0027051", 0),
    ("heart attack", "heart attack", "C0027051", 0),
    ("MI", "mi", "C0027051", 1),
    ("aspirin", "aspirin", "C0004057", 0),
    ("PCI", "pci", "C1532338", 1),
]
con.executemany("INSERT INTO terms (term, term_norm, cui, is_abbrev) VALUES (?,?,?,?)", terms)
con.executemany("INSERT INTO term_lookup VALUES (?,?,?)", [
    ("myocardial infarction", "C0027051", 1),
    ("heart attack", "C0027051", 2),
    ("mi", "C0027051", 3),
    ("aspirin", "C0004057", 1),
    ("pci", "C1532338", 2),
])

hierarchy = [
    ("C0007222", "C0027051", 8, "SNOMEDCT_US", "isa"),
    ("C0027051", "C2349195", 9, "SNOMEDCT_US", "isa"),
    ("C0027051", "C4255010", 9, "SNOMEDCT_US", "isa"),
    ("C0027051", "C0340324", 9, "SNOMEDCT_US", "isa"),
]
con.executemany("INSERT INTO hierarchy VALUES (?,?,?,?,?)", hierarchy)

relationships = [
    ("C0027051", "C0948089", "PAR", "inverse_isa"),
    ("C0027051", "C0004153", "RO", "associated_with"),
    ("C0027051", "C0004057", "RO", "used_for"),
    ("C0027051", "C0070166", "RO", "used_for"),
    ("C2349195", "C1532338", "RO", "treated_with"),
    ("C0948089", "C0007203", "RO", "treated_with"),
    ("C0004057", "C0004058", "RO", "causes"),
    ("C0027051", "C0041199", "RO", "diagnosed_by"),
    ("C0027051", "C0520886", "RO", "diagnosed_by"),
    ("C0027051", "C0019134", "SY", "mapped_from"),  # crosswalk — must be filtered
]
con.executemany("INSERT INTO relationships VALUES (?,?,?,?)", relationships)

CORE = "C0027051"
ok = 0

def check(name, sql, args=(), expect=None):
    global ok
    rows = con.execute(sql, args).fetchall()
    status = "OK " if (expect is None or len(rows) == expect) else "FAIL"
    if status == "OK ":
        ok += 1
    print(f"[{status}] {name}: {len(rows)} rows")
    for r in rows[:8]:
        print("      ", r)
    return rows

print("=" * 70)
check("6.1 anchor core", """
SELECT c.cui, c.canonical_name, c.icd10
FROM term_lookup tl JOIN concepts c ON c.cui = tl.cui
WHERE tl.term_norm IN ('myocardial infarction','heart attack')
ORDER BY tl.rank""")

check("6.2 subtypes", """
WITH RECURSIVE sub(cui, d) AS (
  SELECT child_cui, 1 FROM hierarchy WHERE parent_cui = ?
  UNION
  SELECT h.child_cui, s.d + 1 FROM hierarchy h JOIN sub s ON s.cui = h.parent_cui WHERE s.d < 4
)
SELECT c.canonical_name, MIN(s.d) AS depth
FROM sub s JOIN concepts c ON c.cui = s.cui
GROUP BY s.cui ORDER BY depth, canonical_name""", (CORE,), expect=3)

check("6.3 ancestors", """
WITH RECURSIVE anc(cui, d) AS (
  SELECT parent_cui, 1 FROM hierarchy WHERE child_cui = ?
  UNION
  SELECT h.parent_cui, a.d + 1 FROM hierarchy h JOIN anc a ON a.cui = h.child_cui WHERE a.d < 6
)
SELECT c.canonical_name, MIN(a.d) FROM anc a JOIN concepts c ON c.cui = a.cui
GROUP BY a.cui ORDER BY 2""", (CORE,), expect=1)

check("6.4 related w/ labels", """
SELECT c.canonical_name, c.category, r.rel, r.rela
FROM relationships r
JOIN concepts c ON c.cui = CASE WHEN r.cui1 = ? THEN r.cui2 ELSE r.cui1 END
WHERE (r.cui1 = ? OR r.cui2 = ?) AND r.rel != 'SY'
ORDER BY CASE r.rel WHEN 'RN' THEN 0 WHEN 'RB' THEN 1 ELSE 2 END, c.canonical_name""",
      (CORE, CORE, CORE), expect=6)

check("6.5a drugs linked to MI set", """
WITH RECURSIVE mi_set(cui) AS (
  SELECT ?
  UNION
  SELECT h.child_cui FROM hierarchy h JOIN mi_set s ON s.cui = h.parent_cui
)
SELECT DISTINCT c.canonical_name, r.rel, r.rela
FROM relationships r
JOIN concepts c ON c.cui = CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END
WHERE c.category = 'medication' AND r.rel != 'SY'
  AND (r.cui1 IN (SELECT cui FROM mi_set) OR r.cui2 IN (SELECT cui FROM mi_set))
ORDER BY canonical_name""", (CORE,), expect=2)

check("6.5b drug catalog browse", """
SELECT canonical_name, icd10 FROM concepts WHERE category = 'medication' ORDER BY canonical_name""",
      expect=3)

check("6.6 procedures linked to MI set", """
WITH RECURSIVE mi_set(cui) AS (
  SELECT ?
  UNION
  SELECT h.child_cui FROM hierarchy h JOIN mi_set s ON s.cui = h.parent_cui
)
SELECT DISTINCT c.canonical_name, r.rela
FROM relationships r
JOIN concepts c ON c.cui = CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END
WHERE c.category = 'procedure' AND r.rel != 'SY'
  AND (r.cui1 IN (SELECT cui FROM mi_set) OR r.cui2 IN (SELECT cui FROM mi_set))
ORDER BY canonical_name""", (CORE,), expect=1)

check("6.7 allergies in care network", """
WITH RECURSIVE mi_set(cui) AS (
  SELECT ?
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
ORDER BY c.canonical_name""", (CORE,), expect=1)

check("6.8 findings/labs linked to MI", """
SELECT c.canonical_name, r.rela
FROM relationships r JOIN concepts c ON c.cui = CASE WHEN r.cui1 = ? THEN r.cui2 ELSE r.cui1 END
WHERE (r.cui1 = ? OR r.cui2 = ?) AND c.category = 'finding' AND r.rel != 'SY'
ORDER BY canonical_name""", (CORE, CORE, CORE), expect=2)

check("6.9 full bundle", """
WITH RECURSIVE mi_set(cui) AS (
  SELECT ?
  UNION
  SELECT h.child_cui FROM hierarchy h JOIN mi_set s ON s.cui = h.parent_cui
),
related(cui, rel, rela) AS (
  SELECT CASE WHEN r.cui1 IN (SELECT cui FROM mi_set) THEN r.cui2 ELSE r.cui1 END, r.rel, r.rela
  FROM relationships r
  WHERE (r.cui1 IN (SELECT cui FROM mi_set) OR r.cui2 IN (SELECT cui FROM mi_set)) AND r.rel != 'SY'
)
SELECT 'subtype' AS facet, c.canonical_name, NULL AS rel, NULL AS rela
FROM mi_set s JOIN concepts c ON c.cui = s.cui WHERE s.cui != ?
UNION ALL
SELECT 'related:' || r.rel, c.canonical_name, r.rel, r.rela
FROM related r JOIN concepts c ON c.cui = r.cui
WHERE c.category IN ('diagnosis','medication','procedure','finding','allergy')
ORDER BY facet, canonical_name""", (CORE, CORE), expect=10)

print("=" * 70)
print(f"{ok}/9 query groups passed")
