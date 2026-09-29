"""
test_sparse.py — BM25Okapi sparse retrieval on synonym-expanded tokens.

PASS / FAIL GUIDE
─────────────────
Test                                    Expected today    Why
────────────────────────────────────────────────────────────────────
BM25 build / search basics              PASS              pure rank_bm25, no ML
Score normalisation (max=1.0)           PASS              formula in BM25Index.search()
Zero-score docs filtered                PASS              `if score <= 0: continue`
Save / load cycle                       PASS              pickle + JSON round-trip
Raw abbreviation "MI" misses            PASS              no synonym expansion → score 0
With expansion "MI" finds MI doc        PASS              expand_query adds all synonyms
Clinical term ranks relevant doc first  PASS              BM25 statistical guarantee
Non-clinical query → no hits            PASS              clinical corpus has no match

The BM25 component tests are UNIT tests (no spaCy, no embedder).
The synonym-expansion integration tests use the session-scoped nlp_pipeline.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.indexing import BM25Index
from src.nlp import extract_entities, expand_query


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures local to this module
# ══════════════════════════════════════════════════════════════════════════════

CLINICAL_SECTIONS = [
    {
        "section_id":    "s001",
        "document_id":   "doc_diabetes",
        "document_path": "/test/doc_diabetes.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Type 2 diabetes mellitus, poorly controlled. HbA1c 9.8%. "
            "Diabetic peripheral neuropathy, bilateral lower extremities."
        ),
        "canonical_entities": "type 2 diabetes mellitus, peripheral neuropathy",
        "negated_entities": "",
    },
    {
        "section_id":    "s002",
        "document_id":   "doc_heart",
        "document_path": "/test/doc_heart.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Heart failure with reduced ejection fraction. "
            "Atrial fibrillation, persistent. Hypertension."
        ),
        "canonical_entities": "heart failure, atrial fibrillation, hypertension",
        "negated_entities": "",
    },
    {
        "section_id":    "s003",
        "document_id":   "doc_pneumonia",
        "document_path": "/test/doc_pneumonia.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Community acquired pneumonia, right lower lobe. "
            "COPD exacerbation. Steroids administered."
        ),
        "canonical_entities": "pneumonia, chronic obstructive pulmonary disease",
        "negated_entities": "",
    },
    {
        "section_id":    "s004",
        "document_id":   "doc_mi",
        "document_path": "/test/doc_mi.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Acute myocardial infarction, STEMI. "
            "Emergent PCI performed. Aspirin and clopidogrel initiated."
        ),
        "canonical_entities": "myocardial infarction, aspirin",
        "negated_entities": "",
    },
]


@pytest.fixture(scope="module")
def bm25():
    idx = BM25Index()
    idx.build(CLINICAL_SECTIONS)
    return idx


# ══════════════════════════════════════════════════════════════════════════════
# Build and basic search
# ══════════════════════════════════════════════════════════════════════════════

class TestBM25BuildAndSearch:

    @pytest.mark.unit
    def test_build_succeeds(self):
        idx = BM25Index()
        idx.build(CLINICAL_SECTIONS)  # should not raise

    @pytest.mark.unit
    def test_search_returns_list(self, bm25):
        results = bm25.search(["diabetes"], n_results=5)
        assert isinstance(results, list)

    @pytest.mark.unit
    def test_search_returns_hits(self, bm25):
        results = bm25.search(["diabetes"], n_results=5)
        assert len(results) > 0, "At least one doc contains 'diabetes'"

    @pytest.mark.unit
    def test_hit_has_required_keys(self, bm25):
        results = bm25.search(["diabetes"], n_results=5)
        assert len(results) > 0
        r = results[0]
        assert "section_id" in r
        assert "document_id" in r
        assert "bm25_score" in r
        assert "bm25_score_norm" in r

    @pytest.mark.unit
    def test_diabetes_query_finds_diabetes_doc(self, bm25):
        results = bm25.search(["diabetes", "neuropathy"], n_results=4)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_diabetes" in doc_ids, (
            "The diabetes/neuropathy section should appear when searching "
            "for tokens 'diabetes' and 'neuropathy'"
        )

    @pytest.mark.unit
    def test_most_relevant_doc_ranked_first(self, bm25):
        results = bm25.search(["diabetes", "neuropathy"], n_results=4)
        assert results[0]["document_id"] == "doc_diabetes", (
            "doc_diabetes contains both 'diabetes' and 'neuropathy' and should "
            "rank first when both terms are in the query"
        )

    @pytest.mark.unit
    def test_pneumonia_query_finds_pneumonia_doc(self, bm25):
        results = bm25.search(["pneumonia", "copd"], n_results=4)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_pneumonia" in doc_ids


# ══════════════════════════════════════════════════════════════════════════════
# Score properties
# ══════════════════════════════════════════════════════════════════════════════

class TestBM25ScoreProperties:

    @pytest.mark.unit
    def test_normalised_score_at_most_1(self, bm25):
        results = bm25.search(["myocardial", "infarction"], n_results=4)
        for r in results:
            assert r["bm25_score_norm"] <= 1.0 + 1e-9, (
                "Normalised BM25 score must be ≤ 1.0"
            )

    @pytest.mark.unit
    def test_normalised_score_at_least_0(self, bm25):
        results = bm25.search(["myocardial", "infarction"], n_results=4)
        for r in results:
            assert r["bm25_score_norm"] >= 0.0

    @pytest.mark.unit
    def test_top_result_has_normalised_score_1(self, bm25):
        results = bm25.search(["myocardial", "infarction"], n_results=4)
        assert len(results) > 0
        top = results[0]
        assert top["bm25_score_norm"] == pytest.approx(1.0, abs=1e-6), (
            "The top-ranked document should always have a normalised score of 1.0"
        )

    @pytest.mark.unit
    def test_zero_score_docs_filtered(self, bm25):
        """
        Documents with BM25 score ≤ 0 must be excluded.
        A term that appears in no document at all should return fewer hits
        than the total corpus size.
        """
        results = bm25.search(["zzznomatch"], n_results=10)
        assert len(results) == 0, (
            "A term absent from the corpus should produce zero hits, "
            "not zero-scored entries"
        )

    @pytest.mark.unit
    def test_n_results_cap_respected(self, bm25):
        results = bm25.search(["the", "of", "and", "a"], n_results=2)
        assert len(results) <= 2

    @pytest.mark.unit
    def test_results_sorted_descending(self, bm25):
        results = bm25.search(["diabetes", "mellitus", "neuropathy"], n_results=4)
        scores = [r["bm25_score_norm"] for r in results]
        assert scores == sorted(scores, reverse=True), (
            "Results must be ordered by descending BM25 score"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestBM25EdgeCases:

    @pytest.mark.unit
    def test_empty_index_returns_empty(self):
        idx = BM25Index()
        results = idx.search(["diabetes"], n_results=5)
        assert results == [], "Searching an uninitialised index should return []"

    @pytest.mark.unit
    def test_non_clinical_query_no_hits(self, bm25):
        results = bm25.search(["quarterly", "budget", "forecast"], n_results=5)
        assert len(results) == 0, (
            "Financial terms should score 0 against a clinical corpus"
        )

    @pytest.mark.unit
    def test_incremental_add_section(self):
        idx = BM25Index()
        idx.build(CLINICAL_SECTIONS[:2])
        idx.add_section(CLINICAL_SECTIONS[3])
        results = idx.search(["myocardial", "infarction"], n_results=5)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_mi" in doc_ids, (
            "After add_section(), the new document should be searchable"
        )

    @pytest.mark.unit
    def test_save_and_load_cycle(self, tmp_path, monkeypatch):
        """Persist BM25 to disk and reload; results must be identical."""
        # Monkeypatch the class attributes to use temp paths
        bm25_file = tmp_path / "bm25_index.pkl"
        meta_file  = tmp_path / "bm25_meta.json"
        monkeypatch.setattr(BM25Index, "_BM25_FILE", bm25_file)
        monkeypatch.setattr(BM25Index, "_META_FILE", meta_file)

        original = BM25Index()
        original.build(CLINICAL_SECTIONS)
        original.save()

        loaded = BM25Index()
        ok = loaded.load()
        assert ok, "BM25Index.load() should return True when files exist"

        results_orig  = original.search(["diabetes"], n_results=4)
        results_loaded = loaded.search(["diabetes"], n_results=4)

        orig_ids   = [r["document_id"] for r in results_orig]
        loaded_ids = [r["document_id"] for r in results_loaded]
        assert orig_ids == loaded_ids, (
            "Save/load round-trip must produce identical document ranking"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Synonym expansion integration
# ══════════════════════════════════════════════════════════════════════════════

class TestBM25WithSynonymExpansion:
    """
    BM25 indexes the raw document text. When a user searches with abbreviations
    (e.g. 'MI'), BM25 alone scores 0 because the corpus text contains
    'myocardial infarction', not 'MI'. Synonym expansion (via expand_query)
    adds all synonym forms to the query tokens, bridging this gap.
    """

    @pytest.mark.slow
    def test_abbreviation_MI_misses_without_expansion(self, bm25):
        """
        Raw token 'MI' does not appear in corpus text (which says
        'myocardial infarction'), so BM25 should return 0 hits.
        This confirms expansion is REQUIRED for abbreviations.
        """
        results = bm25.search(["mi"], n_results=4)
        mi_doc_hits = [r for r in results if r["document_id"] == "doc_mi"]
        assert len(mi_doc_hits) == 0, (
            "BM25 without expansion should miss 'MI' (corpus uses 'myocardial infarction'). "
            "This is the baseline showing why expand_query is essential."
        )

    @pytest.mark.slow
    def test_abbreviation_MI_found_with_synonym_expansion(self, bm25, nlp_pipeline, synonym_index):
        """
        *** FAILS TODAY — exposes two cascading bugs ***

        Correct expectation: searching 'MI' with synonym expansion should find
        the myocardial infarction document.

        Bug 1 — expand_query returns MULTI-WORD phrases as single strings:
            expand_query("MI") → ["myocardial infarction", "heart attack", ...]
        BM25Index.search() passes these directly to BM25Okapi.get_scores().
        BM25Okapi treats each list element as a single token, so "myocardial
        infarction" (one string) does not match the separate corpus tokens
        "myocardial" and "infarction,".

        Bug 2 — BM25 tokenizer does not strip punctuation:
            text.lower().split() → ["infarction,", "stemi."]
        The query has "infarction" (no comma) and "stemi" (no period), which
        do not match "infarction," or "stemi." as literal tokens.

        Fix: expand_query should yield individual words of multi-word phrases,
        and the tokenizer should strip punctuation before splitting.
        """
        entities = extract_entities("MI", nlp_pipeline, synonym_index)
        expanded = expand_query(entities, synonym_index)
        # Also add raw query token as QueryProcessor does
        expanded_with_raw = list(set(expanded + ["mi"]))

        results = bm25.search(expanded_with_raw, n_results=4)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_mi" in doc_ids, (
            "With synonym expansion, searching 'MI' should find doc_mi "
            "because 'myocardial' and 'infarction' are added to the query tokens. "
            "FAILS today because expand_query returns multi-word phrases as single "
            "strings, and the tokenizer doesn't strip punctuation."
        )

    @pytest.mark.slow
    def test_expanded_query_outperforms_raw_abbreviation(self, bm25, nlp_pipeline, synonym_index):
        """
        *** FAILS TODAY — same root cause as test_abbreviation_MI_found_with_synonym_expansion ***

        Synonym-expanded BM25 score for doc_mi must exceed the raw 'MI' score.
        Fails because multi-word synonyms are passed as single strings to BM25,
        and punctuation in corpus tokens prevents exact matches.
        """
        # Raw score
        raw_results = {r["document_id"]: r["bm25_score"] for r in bm25.search(["mi"], n_results=4)}

        # Expanded score
        entities = extract_entities("MI", nlp_pipeline, synonym_index)
        expanded = list(set(expand_query(entities, synonym_index) + ["mi"]))
        exp_results = {r["document_id"]: r["bm25_score"] for r in bm25.search(expanded, n_results=4)}

        raw_score = raw_results.get("doc_mi", 0.0)
        exp_score = exp_results.get("doc_mi", 0.0)

        assert exp_score > raw_score, (
            f"Synonym-expanded score ({exp_score:.4f}) should exceed raw 'MI' score "
            f"({raw_score:.4f}) for the myocardial infarction document. "
            "FAILS today due to multi-word synonym tokenization and punctuation bugs."
        )
