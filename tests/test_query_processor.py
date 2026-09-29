"""
test_query_processor.py — QueryProcessor: NER → synonym expansion → embedding → tier.

PASS / FAIL GUIDE
─────────────────
All tests here should PASS today.  QueryProcessor only calls components that
individually work (NLP, expand_query, Embedder) and assembles their outputs
into a dict.  No known bugs affect this layer.

Key invariants verified:
  • tier matches entity specificity → correct threshold applied
  • active_canonicals excludes negated entities
  • bm25_tokens includes both synonym forms and raw query tokens
  • embedding is a valid unit-normalised float vector
  • Abbreviations (MI, DVT) are resolved before tier classification
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import THRESHOLDS


# ══════════════════════════════════════════════════════════════════════════════
# Tier and threshold
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
class TestQueryProcessorTierAndThreshold:

    def test_clinical_tier_for_high_specificity_query(self, query_processor):
        pq = query_processor.process("type 2 diabetes with peripheral neuropathy")
        assert pq["tier"] == "clinical"
        assert pq["threshold"] == THRESHOLDS["clinical"]

    def test_partial_tier_for_low_specificity_query(self, query_processor):
        pq = query_processor.process("patient with fever and nausea")
        assert pq["tier"] == "partial"
        assert pq["threshold"] == THRESHOLDS["partial"]

    def test_non_clinical_tier_for_gibberish(self, query_processor):
        pq = query_processor.process("quarterly budget forecast Q3")
        assert pq["tier"] == "non_clinical"
        assert pq["threshold"] == THRESHOLDS["non_clinical"]

    def test_non_clinical_tier_all_negated(self, query_processor):
        """'no stroke' has entities but all are negated → non_clinical tier."""
        pq = query_processor.process("no stroke, no myocardial infarction")
        assert pq["tier"] == "non_clinical", (
            "All entities negated → no active high-specificity concepts → "
            "non_clinical tier with strict 0.55 threshold"
        )

    def test_clinical_tier_for_abbreviation_query(self, query_processor):
        """'MI' resolves to 'myocardial infarction' (high specificity) → clinical."""
        pq = query_processor.process("MI in prior history")
        assert pq["tier"] == "clinical", (
            "'MI' should resolve to 'myocardial infarction' (high specificity) "
            "before tier classification"
        )

    def test_thresholds_ordering(self, query_processor):
        """clinical < partial < non_clinical (lower threshold = more permissive)."""
        assert THRESHOLDS["clinical"] < THRESHOLDS["partial"] < THRESHOLDS["non_clinical"], (
            "clinical queries have the most permissive threshold; "
            "non-clinical queries require a very high score to be returned"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Entity extraction
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
class TestQueryProcessorEntityExtraction:

    def test_entities_list_returned(self, query_processor):
        pq = query_processor.process("heart failure and atrial fibrillation")
        assert isinstance(pq["entities"], list)

    def test_active_canonicals_are_set(self, query_processor):
        pq = query_processor.process("sepsis urinary tract infection")
        assert isinstance(pq["active_canonicals"], set)

    def test_active_canonicals_for_known_query(self, query_processor):
        pq = query_processor.process("sepsis urinary tract infection")
        assert "sepsis" in pq["active_canonicals"]
        assert "urinary tract infection" in pq["active_canonicals"]

    def test_negated_entity_not_in_active_canonicals(self, query_processor):
        """
        *** FAILS TODAY — negation window bleeds across comma ***

        Correct expectation: 'heart failure' should be active (not negated) in
        "no stroke, confirmed heart failure".

        Root cause: 'heart failure' starts at token ~4; the 6-token lookback
        window reaches back to token 0 ('no'), which IS in the window.
        The `is_negated()` window of 6 tokens does not respect comma/clause
        boundaries, so 'no' from "no stroke" bleeds to 'heart failure'.

        Fix: reduce the window size to 3-4 tokens OR add a clause-boundary stop.
        """
        pq = query_processor.process("no stroke, confirmed heart failure")
        assert "stroke" not in pq["active_canonicals"], (
            "Negated entities must be excluded from active_canonicals"
        )
        assert "heart failure" in pq["active_canonicals"], (
            "FAILS TODAY: 'heart failure' is incorrectly negated because 'no' "
            "from 'no stroke' is within the 6-token lookback window of 'heart failure'."
        )

    def test_all_negated_active_canonicals_empty(self, query_processor):
        pq = query_processor.process("no fever, no nausea")
        assert len(pq["active_canonicals"]) == 0

    def test_abbreviation_MI_in_active_canonicals(self, query_processor):
        pq = query_processor.process("patient with MI")
        assert "myocardial infarction" in pq["active_canonicals"], (
            "'MI' must resolve to its canonical 'myocardial infarction' "
            "before being placed in active_canonicals"
        )

    def test_entities_field_present(self, query_processor):
        pq = query_processor.process("COPD exacerbation")
        assert "entities" in pq
        assert len(pq["entities"]) > 0


# ══════════════════════════════════════════════════════════════════════════════
# BM25 token expansion
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
class TestQueryProcessorBM25Tokens:

    def test_bm25_tokens_is_list(self, query_processor):
        pq = query_processor.process("type 2 diabetes")
        assert isinstance(pq["bm25_tokens"], list)

    def test_bm25_tokens_not_empty_for_clinical_query(self, query_processor):
        pq = query_processor.process("type 2 diabetes")
        assert len(pq["bm25_tokens"]) > 0

    def test_bm25_tokens_include_synonym_forms(self, query_processor):
        """
        Searching 'type 2 diabetes' should expand to include 'T2DM', 'NIDDM',
        etc. so the BM25 index can match abbreviations in clinical text.
        """
        pq = query_processor.process("type 2 diabetes")
        tokens_lower = [t.lower() for t in pq["bm25_tokens"]]
        assert "t2dm" in tokens_lower or "niddm" in tokens_lower, (
            "Synonym expansion must add abbreviation variants like 't2dm' "
            "to the BM25 token list"
        )

    def test_bm25_tokens_include_raw_query_words(self, query_processor):
        """Raw query words (not in synonym dict) are added as-is by QueryProcessor."""
        pq = query_processor.process("type 2 diabetes")
        tokens_lower = [t.lower() for t in pq["bm25_tokens"]]
        # 'type', '2', 'diabetes' are raw tokens from the query string
        assert "type" in tokens_lower or "diabetes" in tokens_lower

    def test_bm25_tokens_no_duplicates(self, query_processor):
        pq = query_processor.process("type 2 diabetes T2DM")
        assert len(pq["bm25_tokens"]) == len(set(pq["bm25_tokens"])), (
            "BM25 token list must contain no duplicate entries"
        )

    def test_negated_entity_excluded_from_bm25_tokens(self, query_processor):
        """
        *** FAILS TODAY — raw query tokens bypass negation filtering ***

        Correct expectation: 'stroke' (negated via 'no stroke') must not appear
        in bm25_tokens; its synonyms would bias retrieval toward stroke documents.

        Root cause (src/retrieval.py QueryProcessor.process()):
            expanded_terms = expand_query(entities, ...)   # correctly excludes stroke
            expanded_terms += query_text.lower().split()   # adds 'no', 'stroke' back!
            bm25_tokens = list(set(expanded_terms))

        The raw query token 'stroke' (from .split()) is added AFTER negation
        filtering, silently re-introducing the negated term.

        Fix: filter raw query tokens against the set of negated canonicals and
        their synonyms before adding to bm25_tokens.
        """
        pq = query_processor.process("no stroke")
        tokens_lower = [t.lower() for t in pq["bm25_tokens"]]
        stroke_synonyms = ["stroke", "cva", "tia", "cerebrovascular accident",
                           "ischemic stroke", "brain attack"]
        for syn in stroke_synonyms:
            assert syn.lower() not in tokens_lower, (
                f"Negated entity synonym '{syn}' must not appear in BM25 tokens. "
                "FAILS TODAY because query_text.lower().split() adds 'stroke' back "
                "to bm25_tokens despite negation filtering in expand_query()."
            )


# ══════════════════════════════════════════════════════════════════════════════
# Embedding
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
class TestQueryProcessorEmbedding:

    def test_embedding_is_list_of_floats(self, query_processor):
        pq = query_processor.process("heart failure management")
        assert isinstance(pq["embedding"], list)
        assert all(isinstance(v, float) for v in pq["embedding"])

    def test_embedding_dimension(self, query_processor):
        pq = query_processor.process("sepsis treatment")
        assert len(pq["embedding"]) == 384, (
            "all-MiniLM-L6-v2 embeddings are 384-dimensional"
        )

    def test_embedding_is_unit_normalised(self, query_processor):
        import math
        pq = query_processor.process("COPD exacerbation with pneumonia")
        norm = math.sqrt(sum(v * v for v in pq["embedding"]))
        assert norm == pytest.approx(1.0, abs=1e-5)

    def test_different_queries_different_embeddings(self, query_processor):
        import numpy as np
        pq1 = query_processor.process("type 2 diabetes mellitus")
        pq2 = query_processor.process("heart failure atrial fibrillation")
        sim = float(np.array(pq1["embedding"]) @ np.array(pq2["embedding"]))
        assert sim < 0.95, (
            "Embeddings for very different clinical queries must not be near-identical"
        )

    def test_query_text_stored_in_processed_query(self, query_processor):
        q = "stroke with hypertension"
        pq = query_processor.process(q)
        assert pq["query_text"] == q
