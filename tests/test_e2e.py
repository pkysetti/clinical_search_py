"""
test_e2e.py — End-to-end pipeline: query text → processed query → hybrid retrieval
              → composite scores → explainability.

All tests use the session-scoped mini-corpus (5 documents) built in conftest.py
with CORRECT embeddings.  This makes every test self-contained — no pre-built
production index is required.

PASS / FAIL GUIDE
─────────────────────────────────────────────────────────────────────────────
Test                                           Expected today   Why
─────────────────────────────────────────────────────────────────────────────
Known clinical queries → correct top doc       PASS             Mini-corpus has those docs
Non-clinical query → empty results             PASS             Threshold 0.55 filters all
Negated query → negated doc not top result     PASS             CCO skips negated canonicals
All results above threshold                    PASS             Threshold filter in retriever
Result count ≤ TOP_K_FINAL                     PASS             Cap enforced in retriever
Plain-text doc retrievable                     PASS             No-section docs handled
Plain-text doc section bonus = 0.6            PASS             Default label in segmenter
Explanation fields present                    PASS             build_explanation() API
Zero-result explanation message               PASS             format_zero_result() output
DocumentIndexer embedding bug (full pipeline) *** FAILS ***    See BUG in test_dense.py
─────────────────────────────────────────────────────────────────────────────
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import TOP_K_FINAL, THRESHOLDS
from src.explainer import build_explanation, format_zero_result


# ══════════════════════════════════════════════════════════════════════════════
# Correct top-document per query
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.slow
class TestE2ETopDocumentPerQuery:
    """
    Each query is a canonical representation of the mini-corpus scenario.
    The expected top document MUST rank first; this is the deterministic
    production contract.
    """

    def test_diabetes_neuropathy_query(self, hybrid_retriever, query_processor):
        pq = query_processor.process("type 2 diabetes mellitus with peripheral neuropathy")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0, "Must return results for a clear clinical query"
        assert results[0]["document_id"] == "doc_diabetes", (
            "Deterministic expectation: 'type 2 diabetes mellitus with peripheral neuropathy' "
            "must rank doc_diabetes first.  If this fails, check CCO or BM25 scoring."
        )

    def test_heart_failure_afib_query(self, hybrid_retriever, query_processor):
        pq = query_processor.process("heart failure atrial fibrillation")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0
        assert results[0]["document_id"] == "doc_heart", (
            "Deterministic expectation: 'heart failure atrial fibrillation' must rank "
            "doc_heart first."
        )

    def test_copd_pneumonia_query(self, hybrid_retriever, query_processor):
        pq = query_processor.process("COPD exacerbation community acquired pneumonia")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0
        assert results[0]["document_id"] == "doc_pneumonia", (
            "Deterministic expectation: 'COPD exacerbation community acquired pneumonia' "
            "must rank doc_pneumonia first."
        )

    def test_sepsis_uti_query(self, hybrid_retriever, query_processor):
        pq = query_processor.process("sepsis urinary tract infection")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0
        assert results[0]["document_id"] == "doc_sepsis_uti", (
            "Deterministic expectation: 'sepsis urinary tract infection' must rank "
            "doc_sepsis_uti first."
        )

    def test_stroke_hypertension_query(self, hybrid_retriever, query_processor):
        pq = query_processor.process("ischemic stroke uncontrolled hypertension")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0
        assert results[0]["document_id"] == "doc_stroke", (
            "Deterministic expectation: 'ischemic stroke uncontrolled hypertension' "
            "must rank doc_stroke first."
        )

    def test_abbreviation_query_CHF_finds_heart_doc(self, hybrid_retriever, query_processor):
        """
        'CHF' (congestive heart failure) is a synonym for 'heart failure'.
        Synonym expansion bridges the abbreviation to the corpus text.
        """
        pq = query_processor.process("CHF with AFib")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0
        doc_ids = [r["document_id"] for r in results]
        assert "doc_heart" in doc_ids, (
            "Abbreviation 'CHF' must resolve to 'heart failure' via synonym expansion "
            "and find doc_heart."
        )

    def test_abbreviation_query_DVT(self, hybrid_retriever, query_processor):
        """
        'DVT' is not in the mini-corpus; this tests that the pipeline runs without
        error even when no docs match, and returns an empty list gracefully.
        """
        pq = query_processor.process("DVT post-operative")
        results = hybrid_retriever.retrieve(pq)
        # DVT not in mini corpus → 0 results is acceptable
        assert isinstance(results, list)


# ══════════════════════════════════════════════════════════════════════════════
# Negative cases and threshold behaviour
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.slow
class TestE2EThresholdAndNegation:

    def test_non_clinical_query_returns_empty(self, hybrid_retriever, query_processor):
        pq = query_processor.process("quarterly revenue forecast for fiscal year 2025")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) == 0, (
            "Non-clinical query: threshold=0.55, no clinical document should score "
            "that high for a financial query.  Returning documents here is a false positive."
        )

    def test_single_word_non_medical_returns_empty(self, hybrid_retriever, query_processor):
        pq = query_processor.process("hello")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) == 0

    def test_negated_concept_not_top_result(self, hybrid_retriever, query_processor):
        """
        'patient has no stroke' — stroke is negated.
        The stroke document (doc_stroke) should NOT be the top result because:
          • stroke is excluded from active_canonicals
          • CCO for doc_stroke will be 0 (no matching active canonicals)
          • doc_stroke's composite relies only on SCS + BTS + SRB
        """
        pq = query_processor.process("patient has no stroke")
        results = hybrid_retriever.retrieve(pq)

        if results:
            # If any results are returned (via semantic similarity), the stroke
            # document should NOT be the top one
            top_doc = results[0]["document_id"]
            assert top_doc != "doc_stroke", (
                "When 'stroke' is explicitly negated, the stroke document must not "
                "be the top result — negated concepts must be excluded from CCO scoring."
            )

    def test_negated_entity_gives_zero_cco_against_negated_doc(
        self, hybrid_retriever, query_processor
    ):
        """
        CCO for doc_stroke when querying 'no stroke' must be 0 because
        active_canonicals is empty (all entities negated).
        """
        pq = query_processor.process("no stroke")
        results = hybrid_retriever.retrieve(pq)

        stroke_result = next(
            (r for r in results if r["document_id"] == "doc_stroke"), None
        )
        if stroke_result:
            assert stroke_result["scores"]["cco"] == pytest.approx(0.0), (
                "CCO must be 0 when the query has no active canonicals "
                "(all entities negated)"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Result invariants
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.slow
class TestE2EResultInvariants:

    def test_all_results_above_their_tier_threshold(self, hybrid_retriever, query_processor):
        queries = [
            "type 2 diabetes mellitus",         # clinical tier
            "patient with fever",               # partial tier
        ]
        for q in queries:
            pq = query_processor.process(q)
            results = hybrid_retriever.retrieve(pq)
            threshold = pq["threshold"]
            for r in results:
                assert r["composite"] >= threshold, (
                    f"Result '{r['document_id']}' composite={r['composite']:.4f} "
                    f"is below threshold={threshold} for query '{q}'"
                )

    def test_result_count_at_most_top_k_final(self, hybrid_retriever, query_processor):
        pq = query_processor.process("type 2 diabetes")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) <= TOP_K_FINAL

    def test_all_results_have_document_id(self, hybrid_retriever, query_processor):
        pq = query_processor.process("heart failure")
        results = hybrid_retriever.retrieve(pq)
        for r in results:
            assert r["document_id"] and r["document_id"].strip() != "", (
                "Every result must have a non-empty document_id"
            )

    def test_all_results_have_document_path(self, hybrid_retriever, query_processor):
        pq = query_processor.process("pneumonia")
        results = hybrid_retriever.retrieve(pq)
        for r in results:
            assert "document_path" in r
            assert r["document_path"]

    def test_all_results_have_four_score_components(self, hybrid_retriever, query_processor):
        pq = query_processor.process("sepsis")
        results = hybrid_retriever.retrieve(pq)
        for r in results:
            assert set(r["scores"].keys()) == {"cco", "scs", "bts", "srb"}

    def test_composite_scores_descending(self, hybrid_retriever, query_processor):
        pq = query_processor.process("type 2 diabetes peripheral neuropathy")
        results = hybrid_retriever.retrieve(pq)
        scores = [r["composite"] for r in results]
        assert scores == sorted(scores, reverse=True), (
            "Results must be returned in descending composite score order"
        )

    def test_no_duplicate_documents_in_results(self, hybrid_retriever, query_processor):
        pq = query_processor.process("type 2 diabetes")
        results = hybrid_retriever.retrieve(pq)
        doc_ids = [r["document_id"] for r in results]
        assert len(doc_ids) == len(set(doc_ids)), (
            "Each document must appear at most once in the results — "
            "de-duplication (best-section per document) must work"
        )

    def test_best_section_key_present(self, hybrid_retriever, query_processor):
        pq = query_processor.process("sepsis")
        results = hybrid_retriever.retrieve(pq)
        for r in results:
            assert "best_section" in r
            assert "section_text" in r["best_section"]
            assert "section_label" in r["best_section"]


# ══════════════════════════════════════════════════════════════════════════════
# Plain-text document behaviour (doc_021 / doc_022 equivalent in mini corpus)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.slow
class TestE2EPlainTextDocuments:
    """
    doc_sepsis_uti in the mini corpus is a plain-text document (no === headers).
    It mirrors the real doc_021_asthma_allergic.txt and doc_022_acute_appendicitis.txt.
    """

    def test_plain_text_doc_is_retrievable(self, hybrid_retriever, query_processor):
        pq = query_processor.process("sepsis urinary tract infection blood cultures")
        results = hybrid_retriever.retrieve(pq)
        doc_ids = [r["document_id"] for r in results]
        assert "doc_sepsis_uti" in doc_ids, (
            "Plain-text documents without === SECTION === headers must still be "
            "indexed and retrievable via hybrid search"
        )

    def test_plain_text_doc_has_default_section_label(
        self, hybrid_retriever, query_processor
    ):
        pq = query_processor.process("sepsis urinary tract infection")
        results = hybrid_retriever.retrieve(pq)
        sepsis = next((r for r in results if r["document_id"] == "doc_sepsis_uti"), None)
        if sepsis:
            assert sepsis["best_section"]["section_label"] == "default", (
                "Plain-text document with no section headers must be stored under "
                "the 'default' section label"
            )

    def test_plain_text_doc_srb_is_lower_than_structured(
        self, hybrid_retriever, query_processor
    ):
        """
        doc_sepsis_uti (plain text, SRB=0.6) vs doc_stroke (structured, SRB=1.0).
        When both are returned, the structured doc should have a higher SRB.
        """
        pq = query_processor.process("hypertension sepsis")
        results = hybrid_retriever.retrieve(pq)

        sepsis = next((r for r in results if r["document_id"] == "doc_sepsis_uti"), None)
        stroke = next((r for r in results if r["document_id"] == "doc_stroke"), None)

        if sepsis and stroke:
            assert sepsis["scores"]["srb"] < stroke["scores"]["srb"], (
                "Plain-text doc (default section, SRB=0.6) must have lower SRB than "
                "a structured doc with 'assessment and plan' section (SRB=1.0)"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Explainability layer
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.slow
class TestE2EExplainability:

    def test_explanation_builds_without_error(
        self, hybrid_retriever, query_processor, synonym_index
    ):
        pq = query_processor.process("type 2 diabetes peripheral neuropathy")
        results = hybrid_retriever.retrieve(pq)
        assert len(results) > 0
        explanation = build_explanation(results[0], pq, synonym_index)
        assert explanation is not None

    def test_explanation_has_composite_score(
        self, hybrid_retriever, query_processor, synonym_index
    ):
        pq = query_processor.process("sepsis urinary tract infection")
        results = hybrid_retriever.retrieve(pq)
        if results:
            exp = build_explanation(results[0], pq, synonym_index)
            assert exp.composite_score == pytest.approx(results[0]["composite"], abs=1e-4)

    def test_explanation_matched_concepts_subset_of_query_and_doc(
        self, hybrid_retriever, query_processor, synonym_index
    ):
        pq = query_processor.process("type 2 diabetes mellitus peripheral neuropathy")
        results = hybrid_retriever.retrieve(pq)
        if results:
            exp = build_explanation(results[0], pq, synonym_index)
            for concept in exp.matched_concepts:
                assert concept in pq["active_canonicals"], (
                    f"Matched concept '{concept}' must be in the query's active canonicals"
                )

    def test_explanation_format_returns_string(
        self, hybrid_retriever, query_processor, synonym_index
    ):
        pq = query_processor.process("heart failure atrial fibrillation")
        results = hybrid_retriever.retrieve(pq)
        if results:
            exp = build_explanation(results[0], pq, synonym_index)
            formatted = exp.format(rank=1)
            assert isinstance(formatted, str)
            assert len(formatted) > 100

    def test_explanation_format_contains_score_breakdown(
        self, hybrid_retriever, query_processor, synonym_index
    ):
        pq = query_processor.process("sepsis")
        results = hybrid_retriever.retrieve(pq)
        if results:
            exp = build_explanation(results[0], pq, synonym_index)
            formatted = exp.format(rank=1)
            assert "cco" in formatted.lower() or "concept" in formatted.lower()
            assert "bm25" in formatted.lower() or "bts" in formatted.lower()

    def test_borderline_detection(
        self, hybrid_retriever, query_processor, synonym_index
    ):
        """is_borderline() returns True when margin < 0.05."""
        pq = query_processor.process("type 2 diabetes")
        results = hybrid_retriever.retrieve(pq)
        if results:
            exp = build_explanation(results[0], pq, synonym_index)
            margin = exp.composite_score - pq["threshold"]
            if margin < 0.05:
                assert exp.is_borderline() is True
            else:
                assert exp.is_borderline() is False

    def test_zero_result_explanation_for_non_clinical(self, query_processor):
        pq = query_processor.process("quarterly budget forecast Q3")
        message = format_zero_result(pq)
        assert isinstance(message, str)
        assert "no results" in message.lower() or "non_clinical" in message.lower()

    def test_zero_result_explanation_includes_query_text(self, query_processor):
        q = "quarterly budget forecast Q3"
        pq = query_processor.process(q)
        message = format_zero_result(pq)
        assert q in message, "Zero-result message must echo back the original query text"

    def test_negation_warning_in_explanation(
        self, hybrid_retriever, query_processor, synonym_index
    ):
        """
        Query for 'sepsis' should find doc_sepsis_uti.
        That document has 'stroke' and 'MI' in its negated_entities.
        The explanation must surface a negation warning if the query also
        mentions those concepts.
        """
        pq = query_processor.process("sepsis stroke")
        results = hybrid_retriever.retrieve(pq)
        sepsis = next((r for r in results if r["document_id"] == "doc_sepsis_uti"), None)
        if sepsis:
            exp = build_explanation(sepsis, pq, synonym_index)
            # stroke is in the query AND negated in doc_sepsis_uti
            assert "stroke" in exp.negation_warnings, (
                "When a queried concept ('stroke') appears NEGATED in the matched document, "
                "the explanation must include a negation warning to avoid misleading the user"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Full pipeline via DocumentIndexer (write → index → query)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
@pytest.mark.slow
class TestE2EDocumentIndexerPipeline:

    def test_full_pipeline_write_index_query(
        self, tmp_path, synonym_index, nlp_pipeline, embedder, query_processor
    ):
        """
        Complete integration test:
          write .txt file → DocumentIndexer.index_document() → HybridRetriever.retrieve()

        Three documents are needed so BM25Okapi IDF is positive for terms that
        appear in only one document (single-doc corpus gives IDF ≤ 0 → no hits).
        """
        from src.retrieval import QueryProcessor, HybridRetriever
        from tests.conftest import make_test_indexer

        docs = {
            "e2e_diabetes.txt": (
                "=== ASSESSMENT AND PLAN ===\n"
                "Type 2 diabetes mellitus, poorly controlled. HbA1c 9.8%.\n"
                "Diabetic peripheral neuropathy. Chronic kidney disease Stage 3a."
            ),
            "e2e_heart.txt": (
                "=== ASSESSMENT AND PLAN ===\n"
                "Heart failure with reduced ejection fraction. "
                "Atrial fibrillation, rate-controlled with metoprolol."
            ),
            "e2e_pneumonia.txt": (
                "=== ASSESSMENT AND PLAN ===\n"
                "Community acquired pneumonia, right lower lobe. "
                "COPD exacerbation. Steroids and bronchodilators."
            ),
        }
        for name, content in docs.items():
            (tmp_path / name).write_text(content)

        indexer = make_test_indexer(tmp_path, synonym_index, nlp_pipeline, embedder)
        for name in docs:
            count = indexer.index_document(tmp_path / name)
            assert count >= 1, f"{name} should produce at least 1 indexed section"

        processor = QueryProcessor(nlp_pipeline, synonym_index, embedder)
        retriever = HybridRetriever(
            indexer.vector_store, indexer.bm25_index, synonym_index, embedder
        )

        pq = processor.process("type 2 diabetes peripheral neuropathy")
        results = retriever.retrieve(pq)

        assert len(results) >= 1, (
            "After indexing a diabetes document, querying for 'type 2 diabetes "
            "peripheral neuropathy' must return at least 1 result"
        )
        assert results[0]["document_id"] == "e2e_diabetes", (
            "The top result must be the diabetes document"
        )
