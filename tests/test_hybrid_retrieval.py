"""
test_hybrid_retrieval.py — Hybrid retrieval: RRF fusion, composite scoring,
threshold filtering, section bonus, and retrieval-path labelling.

PASS / FAIL GUIDE
─────────────────────────────────────────────────────────────────────────────
Test                                     Expected today    Notes
─────────────────────────────────────────────────────────────────────────────
RRF formula correctness                  PASS              pure math
Concept overlap scoring                  PASS              pure math
Composite score formula                  PASS              SCORE_WEIGHTS * components
Threshold filtering                      PASS              docs below threshold dropped
Section bonus applied                    PASS              SRB term in formula
Top-K cap                                PASS              TOP_K_FINAL limit
Dense-only / sparse-only fallback        PASS              RRF handles partial lists
Retrieval-path labelling                 PASS              _retrieval_paths() logic

Unit tests (TestRRF, TestConceptOverlap) mock both indexes → no ML needed.
Integration tests (TestHybridRetriever) use the session conftest fixtures.
─────────────────────────────────────────────────────────────────────────────
"""

import sys
from pathlib import Path
from collections import defaultdict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retrieval import reciprocal_rank_fusion, concept_overlap_score
from src.config import SCORE_WEIGHTS, TOP_K_FINAL, THRESHOLDS


# ══════════════════════════════════════════════════════════════════════════════
# Reciprocal Rank Fusion — unit tests (pure math, no models)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRRF:

    def test_single_list_produces_scores(self):
        ranked = [["doc_a", "doc_b", "doc_c"]]
        scores = reciprocal_rank_fusion(ranked, k=60)
        assert set(scores.keys()) == {"doc_a", "doc_b", "doc_c"}

    def test_single_list_rank_order_preserved(self):
        ranked = [["doc_a", "doc_b", "doc_c"]]
        scores = reciprocal_rank_fusion(ranked, k=60)
        assert scores["doc_a"] > scores["doc_b"] > scores["doc_c"]

    def test_document_in_both_lists_gets_boosted(self):
        """A document appearing in BOTH dense and sparse lists ranks higher."""
        dense  = ["doc_shared", "doc_only_dense"]
        sparse = ["doc_shared", "doc_only_sparse"]
        scores = reciprocal_rank_fusion([dense, sparse], k=60)
        assert scores["doc_shared"] > scores["doc_only_dense"], (
            "doc appearing in both lists should outscore one appearing in only dense"
        )
        assert scores["doc_shared"] > scores["doc_only_sparse"], (
            "doc appearing in both lists should outscore one appearing in only sparse"
        )

    def test_rrf_formula_correctness(self):
        """Verify formula: score(d) = 1/(k+rank) for each list."""
        scores = reciprocal_rank_fusion([["doc_a"]], k=60)
        expected = 1.0 / (60 + 1)
        assert scores["doc_a"] == pytest.approx(expected, rel=1e-6)

    def test_rrf_two_lists_additive(self):
        """Two-list score = sum of per-list 1/(k+rank)."""
        scores = reciprocal_rank_fusion([["doc_a"], ["doc_a"]], k=60)
        expected = 2 * (1.0 / (60 + 1))
        assert scores["doc_a"] == pytest.approx(expected, rel=1e-6)

    def test_higher_k_compresses_scores(self):
        """Larger k → rank differences matter less."""
        s_small_k = reciprocal_rank_fusion([["a", "b"]], k=1)
        s_large_k = reciprocal_rank_fusion([["a", "b"]], k=1000)
        diff_small = s_small_k["a"] - s_small_k["b"]
        diff_large = s_large_k["a"] - s_large_k["b"]
        assert diff_small > diff_large, (
            "With k=1 rank differences are large; with k=1000 they compress toward zero"
        )

    def test_empty_lists_returns_empty(self):
        scores = reciprocal_rank_fusion([[], []], k=60)
        assert scores == {}

    def test_scores_all_positive(self):
        ranked = [["a", "b", "c"], ["b", "c", "a"]]
        scores = reciprocal_rank_fusion(ranked, k=60)
        for doc_id, score in scores.items():
            assert score > 0, f"RRF score for '{doc_id}' must be positive"


# ══════════════════════════════════════════════════════════════════════════════
# Concept Overlap Score — unit tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestConceptOverlapScore:

    def test_identical_sets_score_1(self, synonym_index):
        concepts = {"myocardial infarction", "hypertension"}
        score = concept_overlap_score(concepts, concepts, synonym_index)
        assert score == pytest.approx(1.0)

    def test_disjoint_sets_score_0(self, synonym_index):
        q = {"myocardial infarction"}
        d = {"pneumonia"}
        score = concept_overlap_score(q, d, synonym_index)
        assert score == pytest.approx(0.0)

    def test_partial_overlap_between_0_and_1(self, synonym_index):
        q = {"myocardial infarction", "hypertension"}
        d = {"myocardial infarction", "heart failure"}
        score = concept_overlap_score(q, d, synonym_index)
        assert 0.0 < score < 1.0

    def test_high_specificity_concept_weighs_more(self, synonym_index):
        """
        'myocardial infarction' (high) should contribute more to overlap than
        'fever' (low).  A query that matches only the high-spec concept in a
        2-concept doc should score higher than one matching only the low-spec.
        """
        doc = {"myocardial infarction", "fever"}

        q_high = {"myocardial infarction"}     # matches high-specificity concept
        q_low  = {"fever"}                     # matches low-specificity concept

        score_high = concept_overlap_score(q_high, doc, synonym_index)
        score_low  = concept_overlap_score(q_low, doc, synonym_index)

        assert score_high > score_low, (
            "Matching a high-specificity concept (myocardial infarction, weight=3.0) "
            "must contribute more overlap than matching a low-specificity one (fever, weight=0.5)"
        )

    def test_empty_query_canonicals_returns_0(self, synonym_index):
        score = concept_overlap_score(set(), {"pneumonia"}, synonym_index)
        assert score == pytest.approx(0.0)

    def test_empty_doc_canonicals_returns_0(self, synonym_index):
        score = concept_overlap_score({"pneumonia"}, set(), synonym_index)
        assert score == pytest.approx(0.0)

    def test_both_empty_returns_0(self, synonym_index):
        score = concept_overlap_score(set(), set(), synonym_index)
        assert score == pytest.approx(0.0)

    def test_score_symmetric(self, synonym_index):
        """Overlap score is symmetric: query∩doc == doc∩query."""
        a = {"sepsis", "urinary tract infection"}
        b = {"urinary tract infection", "heart failure"}
        assert concept_overlap_score(a, b, synonym_index) == pytest.approx(
            concept_overlap_score(b, a, synonym_index)
        )


# ══════════════════════════════════════════════════════════════════════════════
# HybridRetriever — mocked indexes (unit) + real session fixtures (integration)
# ══════════════════════════════════════════════════════════════════════════════

class _MockVectorStore:
    """Minimal VectorStore stub returning predetermined hits."""
    def __init__(self, hits):
        self._hits = hits

    def query(self, embedding, n_results=20):
        return self._hits[:n_results]


class _MockBM25Index:
    """Minimal BM25Index stub returning predetermined hits."""
    def __init__(self, hits):
        self._hits = hits

    def search(self, query_terms, n_results=20):
        return self._hits[:n_results]


def _make_mock_hit(
    section_id, document_id, cosine_similarity=0.8,
    bm25_score_norm=0.9, section_bonus=1.0,
    canonical_entities="sepsis", negated_entities="",
    section_label="assessment and plan",
):
    """Build a mock section dict compatible with HybridRetriever."""
    return {
        "section_id":         section_id,
        "document_id":        document_id,
        "document_path":      f"/test/{document_id}.txt",
        "section_label":      section_label,
        "section_text":       f"Clinical text for {document_id}.",
        "section_bonus":      section_bonus,
        "canonical_entities": canonical_entities,
        "negated_entities":   negated_entities,
        "cosine_similarity":  cosine_similarity,
        "bm25_score":         bm25_score_norm * 10,
        "bm25_score_norm":    bm25_score_norm,
    }


@pytest.fixture
def mock_retriever(synonym_index, embedder):
    from src.retrieval import HybridRetriever

    dense_hits = [
        _make_mock_hit("s1", "doc_sepsis", cosine_similarity=0.9, bm25_score_norm=0.0,
                       canonical_entities="sepsis, urinary tract infection"),
        _make_mock_hit("s2", "doc_diabetes", cosine_similarity=0.6, bm25_score_norm=0.0,
                       canonical_entities="type 2 diabetes mellitus"),
    ]
    sparse_hits = [
        _make_mock_hit("s1", "doc_sepsis", cosine_similarity=0.0, bm25_score_norm=0.95,
                       canonical_entities="sepsis, urinary tract infection"),
        _make_mock_hit("s3", "doc_stroke", cosine_similarity=0.0, bm25_score_norm=0.7,
                       canonical_entities="stroke, hypertension"),
    ]

    return HybridRetriever(
        vector_store=_MockVectorStore(dense_hits),
        bm25_index=_MockBM25Index(sparse_hits),
        synonym_index=synonym_index,
        embedder=embedder,
    )


@pytest.fixture
def sepsis_processed_query(query_processor):
    return query_processor.process("sepsis urinary tract infection")


# ── Unit tests using mocked indexes ──────────────────────────────────────────

@pytest.mark.slow
class TestHybridRetrieverUnit:

    def test_retrieve_returns_list(self, mock_retriever, sepsis_processed_query):
        results = mock_retriever.retrieve(sepsis_processed_query)
        assert isinstance(results, list)

    def test_doc_in_both_lists_retrieved(self, mock_retriever, sepsis_processed_query):
        results = mock_retriever.retrieve(sepsis_processed_query)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_sepsis" in doc_ids, (
            "doc_sepsis appears in both dense and sparse lists (RRF boost); "
            "it must appear in the final results"
        )

    def test_results_sorted_by_composite_score(self, mock_retriever, sepsis_processed_query):
        results = mock_retriever.retrieve(sepsis_processed_query)
        scores = [r["composite"] for r in results]
        assert scores == sorted(scores, reverse=True), (
            "Results must be ordered by descending composite score"
        )

    def test_all_four_score_components_present(self, mock_retriever, sepsis_processed_query):
        results = mock_retriever.retrieve(sepsis_processed_query)
        assert len(results) > 0
        scores = results[0]["scores"]
        assert set(scores.keys()) == {"cco", "scs", "bts", "srb"}, (
            "Every result must expose all four score components: "
            "cco (concept overlap), scs (cosine), bts (BM25), srb (section bonus)"
        )

    def test_composite_score_matches_weighted_sum(self, mock_retriever, sepsis_processed_query):
        results = mock_retriever.retrieve(sepsis_processed_query)
        assert len(results) > 0
        for r in results:
            s = r["scores"]
            expected = (
                SCORE_WEIGHTS["cco"] * s["cco"] +
                SCORE_WEIGHTS["scs"] * s["scs"] +
                SCORE_WEIGHTS["bts"] * s["bts"] +
                SCORE_WEIGHTS["srb"] * s["srb"]
            )
            assert r["composite"] == pytest.approx(expected, abs=1e-4), (
                "composite score must equal the weighted sum of (cco, scs, bts, srb)"
            )

    def test_score_weights_sum_to_1(self):
        total = sum(SCORE_WEIGHTS.values())
        assert total == pytest.approx(1.0, abs=1e-9), (
            "SCORE_WEIGHTS must sum to exactly 1.0 so that composite ∈ [0, 1]"
        )

    def test_results_above_threshold(self, mock_retriever, sepsis_processed_query):
        threshold = sepsis_processed_query["threshold"]
        results = mock_retriever.retrieve(sepsis_processed_query)
        for r in results:
            assert r["composite"] >= threshold, (
                f"Document '{r['document_id']}' composite score {r['composite']:.4f} "
                f"is below the threshold {threshold} — it should not be returned"
            )

    def test_top_k_final_cap(self, synonym_index, embedder, query_processor):
        """Create many hits and verify TOP_K_FINAL cap is respected."""
        from src.retrieval import HybridRetriever
        many_hits = [
            _make_mock_hit(f"s{i}", f"doc_{i}", cosine_similarity=0.9,
                           bm25_score_norm=0.9, canonical_entities="sepsis")
            for i in range(20)
        ]
        retriever = HybridRetriever(
            _MockVectorStore(many_hits), _MockBM25Index(many_hits),
            synonym_index, embedder,
        )
        pq = query_processor.process("sepsis")
        results = retriever.retrieve(pq)
        assert len(results) <= TOP_K_FINAL, (
            f"HybridRetriever must never return more than TOP_K_FINAL={TOP_K_FINAL} results"
        )

    def test_non_clinical_query_high_threshold_filters_results(
        self, synonym_index, embedder, query_processor
    ):
        """
        A non-clinical query gets threshold 0.55.  Low-scoring hits (from typical
        clinical documents that don't match a budget query) should be filtered out.
        """
        from src.retrieval import HybridRetriever
        # Hits with composite ≈ 0.30 (typical for low-concept-overlap clinical docs)
        low_hits = [
            _make_mock_hit(f"s{i}", f"doc_{i}", cosine_similarity=0.3,
                           bm25_score_norm=0.0, canonical_entities="", section_bonus=0.6)
            for i in range(3)
        ]
        retriever = HybridRetriever(
            _MockVectorStore(low_hits), _MockBM25Index([]),
            synonym_index, embedder,
        )
        pq = query_processor.process("quarterly budget forecast")
        results = retriever.retrieve(pq)
        # composite ≈ 0.30 * 0.4 (cco=0) + 0.30 * 0.30 (scs) + 0.0 (bts) + 0.6 * 0.10 (srb)
        # ≈ 0 + 0.09 + 0 + 0.06 = 0.15 → below non_clinical threshold 0.55
        assert len(results) == 0, (
            "A non-clinical query has threshold=0.55; low-scoring clinical docs "
            "should all be filtered out"
        )

    def test_retrieval_path_dense_only(self, synonym_index, embedder, query_processor):
        from src.retrieval import HybridRetriever
        dense_hit = _make_mock_hit("s1", "doc_a", cosine_similarity=0.9, canonical_entities="sepsis")
        retriever = HybridRetriever(
            _MockVectorStore([dense_hit]), _MockBM25Index([]),
            synonym_index, embedder,
        )
        pq = query_processor.process("sepsis")
        results = retriever.retrieve(pq)
        if results:
            assert results[0]["retrieval_paths"] == "dense", (
                "When a section is found only by dense retrieval, path should be 'dense'"
            )

    def test_retrieval_path_both(self, synonym_index, embedder, query_processor):
        from src.retrieval import HybridRetriever
        shared_hit = _make_mock_hit("s1", "doc_a", cosine_similarity=0.9,
                                    bm25_score_norm=0.9, canonical_entities="sepsis")
        retriever = HybridRetriever(
            _MockVectorStore([shared_hit]), _MockBM25Index([shared_hit]),
            synonym_index, embedder,
        )
        pq = query_processor.process("sepsis")
        results = retriever.retrieve(pq)
        if results:
            assert results[0]["retrieval_paths"] == "dense + sparse", (
                "When a section is retrieved by both dense and sparse, path must be 'dense + sparse'"
            )

    def test_section_bonus_srb_component(self, synonym_index, embedder, query_processor):
        """
        Two otherwise identical documents: one with 'assessment and plan' bonus (1.0)
        and one with 'default' bonus (0.6).  The high-bonus doc should score higher.
        """
        from src.retrieval import HybridRetriever

        ap_hit = _make_mock_hit("s1", "doc_ap", cosine_similarity=0.7,
                                bm25_score_norm=0.5, section_bonus=1.0,
                                canonical_entities="sepsis",
                                section_label="assessment and plan")
        df_hit = _make_mock_hit("s2", "doc_df", cosine_similarity=0.7,
                                bm25_score_norm=0.5, section_bonus=0.6,
                                canonical_entities="sepsis",
                                section_label="default")

        retriever = HybridRetriever(
            _MockVectorStore([ap_hit, df_hit]),
            _MockBM25Index([ap_hit, df_hit]),
            synonym_index, embedder,
        )
        pq = query_processor.process("sepsis")
        results = retriever.retrieve(pq)

        if len(results) == 2:
            assert results[0]["document_id"] == "doc_ap", (
                "Document with 'assessment and plan' section (SRB=1.0) should rank higher "
                "than identical document with 'default' section (SRB=0.6)"
            )


# ── Integration tests using real session mini-corpus ─────────────────────────

@pytest.mark.slow
class TestHybridRetrieverIntegration:

    def test_diabetes_query_returns_results(self, hybrid_retriever, query_processor):
        pq = query_processor.process("type 2 diabetes mellitus peripheral neuropathy")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0

    def test_diabetes_doc_in_top_results(self, hybrid_retriever, query_processor):
        pq = query_processor.process("type 2 diabetes mellitus peripheral neuropathy")
        results = hybrid_retriever.retrieve(pq)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_diabetes" in doc_ids, (
            "The mini-corpus doc_diabetes contains 'type 2 diabetes mellitus' and "
            "'peripheral neuropathy' and must appear in the results"
        )

    def test_diabetes_doc_is_top_result(self, hybrid_retriever, query_processor):
        pq = query_processor.process("type 2 diabetes mellitus peripheral neuropathy")
        results = hybrid_retriever.retrieve(pq)
        assert results[0]["document_id"] == "doc_diabetes", (
            "doc_diabetes should be the top-ranked result for a diabetes/neuropathy query"
        )

    def test_non_clinical_query_no_results(self, hybrid_retriever, query_processor):
        pq = query_processor.process("quarterly budget forecast Q3 revenue")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) == 0, (
            "A non-clinical query hits the 0.55 threshold; no clinical mini-corpus "
            "document should score that high for a financial query"
        )

    def test_plain_text_doc_retrievable(self, hybrid_retriever, query_processor):
        """
        doc_sepsis_uti is the plain-text (no section headers) mini-corpus doc.
        It should still be findable for a sepsis query.
        """
        pq = query_processor.process("sepsis urinary tract infection")
        results = hybrid_retriever.retrieve(pq)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_sepsis_uti" in doc_ids, (
            "Plain-text documents without section headers must still be retrievable "
            "via dense + sparse retrieval"
        )

    def test_plain_text_doc_section_bonus_is_default(self, hybrid_retriever, query_processor):
        """
        doc_sepsis_uti has no section headers → section_label='default' → bonus=0.6.
        Verify the SRB component reflects this.
        """
        pq = query_processor.process("sepsis urinary tract infection")
        results = hybrid_retriever.retrieve(pq)
        sepsis = next((r for r in results if r["document_id"] == "doc_sepsis_uti"), None)
        if sepsis:
            assert sepsis["scores"]["srb"] == pytest.approx(0.6, abs=1e-4), (
                "Plain-text doc should have SRB=0.6 (default bonus), "
                "lower than structured docs with 'assessment and plan' (SRB=1.0)"
            )
