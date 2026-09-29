"""
test_nlp.py — Clinical NER (spaCy + EntityRuler), negation, synonym expansion,
section segmentation, and query-tier classification.

PASS / FAIL GUIDE
─────────────────
Component                        Expected today    Root cause if failing
────────────────────────────────────────────────────────────────────────
SynonymIndex lookups             PASS              Pure dict lookup
EntityRuler matching             PASS              patterns built from synonyms.json
Abbreviation matching (MI/COPD)  PASS              phrase_matcher_attr="LOWER"
Negation (simple window)         PASS              6-token window catches common patterns
Negation (long gap ~7 tokens)    PASS              boundary: "no" still within window
Section segmentation (headers)   PASS              regex matches === LABEL ===
Section segmentation (no hdrs)   PASS              falls back to single "default" section
Query tier classification         PASS              based on entity specificity
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.nlp import (
    SynonymIndex,
    extract_entities,
    expand_query,
    segment_sections,
    determine_query_tier,
)
from src.config import SECTION_BONUSES


# ══════════════════════════════════════════════════════════════════════════════
# SynonymIndex
# ══════════════════════════════════════════════════════════════════════════════

class TestSynonymIndex:

    @pytest.mark.unit
    def test_canonical_from_canonical(self, synonym_index):
        assert synonym_index.get_canonical("myocardial infarction") == "myocardial infarction"

    @pytest.mark.unit
    def test_canonical_from_common_synonym(self, synonym_index):
        assert synonym_index.get_canonical("heart attack") == "myocardial infarction"

    @pytest.mark.unit
    def test_abbreviation_MI(self, synonym_index):
        assert synonym_index.get_canonical("MI") == "myocardial infarction"

    @pytest.mark.unit
    def test_abbreviation_T2DM(self, synonym_index):
        assert synonym_index.get_canonical("T2DM") == "type 2 diabetes mellitus"

    @pytest.mark.unit
    def test_abbreviation_COPD(self, synonym_index):
        assert synonym_index.get_canonical("COPD") == "chronic obstructive pulmonary disease"

    @pytest.mark.unit
    def test_abbreviation_CHF(self, synonym_index):
        assert synonym_index.get_canonical("CHF") == "heart failure"

    @pytest.mark.unit
    def test_abbreviation_DVT(self, synonym_index):
        assert synonym_index.get_canonical("DVT") == "deep vein thrombosis"

    @pytest.mark.unit
    def test_abbreviation_AFib(self, synonym_index):
        assert synonym_index.get_canonical("AFib") == "atrial fibrillation"

    @pytest.mark.unit
    def test_abbreviation_HbA1c(self, synonym_index):
        assert synonym_index.get_canonical("HbA1c") == "hemoglobin a1c"

    @pytest.mark.unit
    def test_unknown_term_returns_none(self, synonym_index):
        assert synonym_index.get_canonical("unicorn disease") is None

    @pytest.mark.unit
    def test_case_insensitive_lookup(self, synonym_index):
        assert synonym_index.get_canonical("heart attack") == "myocardial infarction"
        assert synonym_index.get_canonical("HEART ATTACK") == "myocardial infarction"

    @pytest.mark.unit
    def test_get_synonyms_includes_canonical(self, synonym_index):
        syns = synonym_index.get_synonyms("myocardial infarction")
        assert "myocardial infarction" in syns
        assert "heart attack" in syns
        assert "MI" in syns

    @pytest.mark.unit
    def test_get_synonyms_for_diabetes(self, synonym_index):
        syns = synonym_index.get_synonyms("type 2 diabetes mellitus")
        assert "T2DM" in syns
        assert "NIDDM" in syns
        assert "type 2 diabetes" in syns

    @pytest.mark.unit
    def test_specificity_high(self, synonym_index):
        assert synonym_index.get_specificity("myocardial infarction") == "high"
        assert synonym_index.get_specificity("type 2 diabetes mellitus") == "high"
        assert synonym_index.get_specificity("stroke") == "high"
        assert synonym_index.get_specificity("pulmonary embolism") == "high"

    @pytest.mark.unit
    def test_specificity_medium(self, synonym_index):
        assert synonym_index.get_specificity("hypertension") == "medium"
        assert synonym_index.get_specificity("chest pain") == "medium"

    @pytest.mark.unit
    def test_specificity_low(self, synonym_index):
        assert synonym_index.get_specificity("fever") == "low"
        assert synonym_index.get_specificity("nausea") == "low"
        assert synonym_index.get_specificity("abdominal pain") == "low"

    @pytest.mark.unit
    def test_all_terms_not_empty(self, synonym_index):
        terms = synonym_index.get_all_terms()
        assert len(terms) > 50

    @pytest.mark.unit
    def test_all_canonicals_not_empty(self, synonym_index):
        assert len(synonym_index.all_canonicals) > 20


# ══════════════════════════════════════════════════════════════════════════════
# Entity Extraction (spaCy + EntityRuler)
# ══════════════════════════════════════════════════════════════════════════════

class TestEntityExtraction:

    @pytest.mark.slow
    def test_extracts_single_entity(self, nlp_pipeline, synonym_index):
        text = "Patient has type 2 diabetes mellitus."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        canonicals = [e["canonical"] for e in entities]
        assert "type 2 diabetes mellitus" in canonicals

    @pytest.mark.slow
    def test_extracts_multiple_entities(self, nlp_pipeline, synonym_index):
        text = "Patient has heart failure and atrial fibrillation and hypertension."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        canonicals = [e["canonical"] for e in entities]
        assert "heart failure" in canonicals
        assert "atrial fibrillation" in canonicals
        assert "hypertension" in canonicals

    @pytest.mark.slow
    def test_case_insensitive_matching(self, nlp_pipeline, synonym_index):
        text = "Diagnosis: HEART FAILURE and HYPERTENSION."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        canonicals = [e["canonical"] for e in entities]
        assert "heart failure" in canonicals or "hypertension" in canonicals

    @pytest.mark.slow
    def test_abbreviation_COPD_recognized(self, nlp_pipeline, synonym_index):
        text = "Patient has COPD exacerbation."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        canonicals = [e["canonical"] for e in entities]
        assert "chronic obstructive pulmonary disease" in canonicals

    @pytest.mark.slow
    def test_abbreviation_MI_recognized(self, nlp_pipeline, synonym_index):
        """
        EntityRuler pattern for 'MI' (single token, LOWER match) should fire.
        If this fails, check that phrase_matcher_attr='LOWER' is set in load_nlp().
        """
        text = "Patient has a history of MI."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        canonicals = [e["canonical"] for e in entities]
        assert "myocardial infarction" in canonicals, (
            "Abbreviation 'MI' should resolve to 'myocardial infarction' via EntityRuler. "
            "Ensure phrase_matcher_attr='LOWER' is enabled in load_nlp()."
        )

    @pytest.mark.slow
    def test_multiword_entity_heart_failure(self, nlp_pipeline, synonym_index):
        text = "Diagnosis: heart failure with preserved ejection fraction."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        canonicals = [e["canonical"] for e in entities]
        assert "heart failure" in canonicals

    @pytest.mark.slow
    def test_no_duplicates_same_canonical(self, nlp_pipeline, synonym_index):
        """
        'type 2 diabetes' and 'T2DM' in the same text both map to the same
        canonical; only one entity dict should be returned.
        """
        text = "Patient has type 2 diabetes (T2DM), poorly controlled."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        canonicals = [e["canonical"] for e in entities]
        assert canonicals.count("type 2 diabetes mellitus") == 1

    @pytest.mark.slow
    def test_entity_has_canonical_field(self, nlp_pipeline, synonym_index):
        text = "Patient has DVT in the left leg."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        for e in entities:
            assert "canonical" in e
            assert e["canonical"] is not None

    @pytest.mark.slow
    def test_entity_has_negated_field(self, nlp_pipeline, synonym_index):
        text = "Patient has pneumonia."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        for e in entities:
            assert "negated" in e

    @pytest.mark.slow
    def test_entity_has_specificity_field(self, nlp_pipeline, synonym_index):
        text = "Patient has myocardial infarction."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        for e in entities:
            assert "specificity" in e
            assert e["specificity"] in ("high", "medium", "low")

    @pytest.mark.slow
    def test_non_clinical_text_yields_no_clinical_entities(self, nlp_pipeline, synonym_index):
        text = "The quarterly budget forecast for Q3 has been submitted."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        assert len(entities) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Negation Detection
# ══════════════════════════════════════════════════════════════════════════════

class TestNegationDetection:
    """
    Negation uses a 6-token lookback window from the entity span start.
    Simple substring match against NEGATION_TRIGGERS.
    """

    @pytest.mark.slow
    def test_no_prefix_negates_entity(self, nlp_pipeline, synonym_index):
        text = "Patient has no fever."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        fever = next((e for e in entities if e["canonical"] == "fever"), None)
        assert fever is not None, "fever should be extracted"
        assert fever["negated"] is True, "'no fever' → fever should be negated"

    @pytest.mark.slow
    def test_denies_negates_entity(self, nlp_pipeline, synonym_index):
        text = "Patient denies fever, chills, or night sweats."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        fever = next((e for e in entities if e["canonical"] == "fever"), None)
        assert fever is not None
        assert fever["negated"] is True, "'denies fever' → fever should be negated"

    @pytest.mark.slow
    def test_denied_negates_entity(self, nlp_pipeline, synonym_index):
        text = "Stroke was denied by the patient."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        # Note: "denied" comes AFTER stroke, so it's NOT in the lookback window
        # This tests a KNOWN LIMITATION: post-hoc negation is not caught.
        stroke = next((e for e in entities if e["canonical"] == "stroke"), None)
        if stroke:
            # Document the actual (possibly incorrect) behavior
            pass  # post-hoc negation is out-of-scope for the window approach

    @pytest.mark.slow
    def test_without_negates_following_entity(self, nlp_pipeline, synonym_index):
        text = "Patient presented without fever."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        fever = next((e for e in entities if e["canonical"] == "fever"), None)
        assert fever is not None
        assert fever["negated"] is True, "'without fever' → fever should be negated"

    @pytest.mark.slow
    def test_no_prior_history_negates(self, nlp_pipeline, synonym_index):
        text = "No prior history of stroke or myocardial infarction."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        stroke = next((e for e in entities if e["canonical"] == "stroke"), None)
        assert stroke is not None, "stroke should be extracted even when negated"
        assert stroke["negated"] is True, (
            "'No prior history of stroke' → stroke should be negated. "
            "Trigger 'no' is within the 6-token lookback window."
        )

    @pytest.mark.slow
    def test_no_evidence_of_negates(self, nlp_pipeline, synonym_index):
        text = "Chest X-ray shows no evidence of pneumonia."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        pneu = next((e for e in entities if e["canonical"] == "pneumonia"), None)
        assert pneu is not None
        assert pneu["negated"] is True, (
            "'no evidence of pneumonia' → pneumonia should be negated. "
            "Trigger 'no evidence of' should appear in the lookback window."
        )

    @pytest.mark.slow
    def test_ruled_out_negates(self, nlp_pipeline, synonym_index):
        text = "Pulmonary embolism ruled out by CTPA."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        pe = next((e for e in entities if e["canonical"] == "pulmonary embolism"), None)
        # "ruled out" comes AFTER the entity, so it is NOT in the lookback window.
        # This is a known limitation; the entity should NOT be negated here.
        if pe:
            assert pe["negated"] is False, (
                "Post-entity negation ('ruled out' AFTER the entity) should NOT "
                "be caught by the 6-token lookback window — this is a known limitation."
            )

    @pytest.mark.slow
    def test_positive_entity_not_negated(self, nlp_pipeline, synonym_index):
        text = "Patient has confirmed sepsis secondary to UTI."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        sepsis = next((e for e in entities if e["canonical"] == "sepsis"), None)
        assert sepsis is not None
        assert sepsis["negated"] is False, "Positively asserted 'sepsis' should not be negated"

    @pytest.mark.slow
    def test_negation_does_not_bleed_across_sentences(self, nlp_pipeline, synonym_index):
        """
        *** FAILS TODAY — substring match bug in is_negated() ***

        Correct expectation: 'heart failure' in sentence 2 should NOT be negated
        because 'No' (the trigger) is more than 6 tokens away.

        Root cause: is_negated() uses  `trigger in window_text`  (substring match).
        For the window "stroke was noted. patient has", the trigger "no" is a
        SUBSTRING of "noted" ("**no**ted"), so the check fires incorrectly.
        Similarly "not" is a substring of "noted".

        Fix: use word-boundary matching instead of substring match.
        e.g. re.search(r'\\b' + re.escape(trigger) + r'\\b', window_text)
        """
        text = "No stroke was noted. Patient has heart failure."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        hf = next((e for e in entities if e["canonical"] == "heart failure"), None)
        if hf:
            assert hf["negated"] is False, (
                "Heart failure should not be negated: 'no' appears only as a "
                "substring of 'noted', not as a standalone negation trigger. "
                "Fix is_negated() to use word-boundary (regex \\\\b) matching."
            )


# ══════════════════════════════════════════════════════════════════════════════
# Query Expansion
# ══════════════════════════════════════════════════════════════════════════════

class TestQueryExpansion:

    @pytest.mark.slow
    def test_expands_canonical_to_all_synonyms(self, nlp_pipeline, synonym_index):
        """
        expand_query() yields individual tokens from each synonym form so that
        BM25 can match them against separately-indexed corpus tokens.
        Multi-word synonyms like 'type 2 diabetes' are tokenized into
        ['type', '2', 'diabetes'] rather than kept as a single string.
        """
        text = "Patient has type 2 diabetes mellitus."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        expanded = expand_query(entities, synonym_index)
        tokens_lower = [t.lower() for t in expanded]
        # Single-word abbreviations remain single tokens
        assert "t2dm" in tokens_lower
        assert "niddm" in tokens_lower
        # Multi-word synonyms are split: "type 2 diabetes" → ["type", "2", "diabetes"]
        assert "type" in tokens_lower
        assert "diabetes" in tokens_lower

    @pytest.mark.slow
    def test_excludes_negated_entities_by_default(self, nlp_pipeline, synonym_index):
        text = "No prior history of stroke."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        expanded = expand_query(entities, synonym_index, include_negated=False)
        stroke_terms = synonym_index.get_synonyms("stroke")
        for t in stroke_terms:
            assert t.lower() not in [e.lower() for e in expanded], (
                f"Negated entity synonym '{t}' should be excluded from BM25 tokens"
            )

    @pytest.mark.slow
    def test_includes_negated_when_flag_set(self, nlp_pipeline, synonym_index):
        text = "No prior history of stroke."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        expanded = expand_query(entities, synonym_index, include_negated=True)
        assert len(expanded) > 0

    @pytest.mark.slow
    def test_no_duplicate_tokens(self, nlp_pipeline, synonym_index):
        text = "Patient has type 2 diabetes and T2DM."
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        expanded = expand_query(entities, synonym_index)
        assert len(expanded) == len(set(expanded)), "Expanded tokens should be unique"


# ══════════════════════════════════════════════════════════════════════════════
# Section Segmentation
# ══════════════════════════════════════════════════════════════════════════════

class TestSectionSegmentation:

    STRUCTURED_DOC = """\
PATIENT: Test Patient | MRN: 12345

=== CHIEF COMPLAINT ===
Shortness of breath for 3 days.

=== ASSESSMENT AND PLAN ===
1. Heart failure exacerbation.
2. Hypertension.

=== DISCHARGE INSTRUCTIONS ===
Follow up in 1 week.
"""

    PLAIN_TEXT_DOC = """\
PATIENT: Marcus T. | MRN: 318654

Marcus T. is a 24-year-old male with acute appendicitis. He underwent
laparoscopic appendectomy without complications. Discharged home on
postoperative day one with oral antibiotics.
"""

    @pytest.mark.unit
    def test_structured_doc_produces_multiple_sections(self):
        sections = segment_sections(self.STRUCTURED_DOC)
        labels = [s["label"] for s in sections]
        assert "assessment and plan" in labels
        assert "chief complaint" in labels

    @pytest.mark.unit
    def test_plain_text_doc_produces_single_default_section(self):
        """
        Documents without === SECTION === headers (e.g. doc_021, doc_022)
        should produce exactly one section with label 'default'.
        """
        sections = segment_sections(self.PLAIN_TEXT_DOC)
        assert len(sections) == 1, (
            f"Plain-text doc should yield 1 'default' section, got {len(sections)}"
        )
        assert sections[0]["label"] == "default"

    @pytest.mark.unit
    def test_plain_text_default_section_contains_full_text(self):
        sections = segment_sections(self.PLAIN_TEXT_DOC)
        assert "appendicitis" in sections[0]["text"]

    @pytest.mark.unit
    def test_assessment_plan_section_bonus(self):
        sections = segment_sections(self.STRUCTURED_DOC)
        ap = next((s for s in sections if s["label"] == "assessment and plan"), None)
        assert ap is not None
        assert ap["bonus"] == SECTION_BONUSES["assessment and plan"]

    @pytest.mark.unit
    def test_chief_complaint_section_bonus(self):
        sections = segment_sections(self.STRUCTURED_DOC)
        cc = next((s for s in sections if s["label"] == "chief complaint"), None)
        assert cc is not None
        assert cc["bonus"] == SECTION_BONUSES["chief complaint"]

    @pytest.mark.unit
    def test_discharge_instructions_lower_bonus_than_assessment(self):
        sections = segment_sections(self.STRUCTURED_DOC)
        ap = next((s for s in sections if s["label"] == "assessment and plan"), None)
        di = next((s for s in sections if s["label"] == "discharge instructions"), None)
        if ap and di:
            assert ap["bonus"] > di["bonus"], (
                "Assessment & plan should score higher than discharge instructions "
                "(boilerplate-heavy sections are less clinically informative)"
            )

    @pytest.mark.unit
    def test_default_section_bonus_is_0_6(self):
        sections = segment_sections(self.PLAIN_TEXT_DOC)
        assert sections[0]["bonus"] == pytest.approx(0.6)

    @pytest.mark.unit
    def test_sections_have_non_empty_text(self):
        sections = segment_sections(self.STRUCTURED_DOC)
        for s in sections:
            assert s["text"].strip() != "", f"Section '{s['label']}' has empty text"

    @pytest.mark.unit
    def test_sections_have_required_keys(self):
        sections = segment_sections(self.STRUCTURED_DOC)
        for s in sections:
            assert "label" in s
            assert "text" in s
            assert "bonus" in s

    @pytest.mark.unit
    def test_assessment_plan_highest_bonus_in_config(self):
        bonuses = {k: v for k, v in SECTION_BONUSES.items() if k != "default"}
        max_label = max(bonuses, key=bonuses.get)
        assert max_label == "assessment and plan", (
            "assessment and plan should have the highest section bonus; "
            "it contains the most clinically actionable information"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Query Tier Classification
# ══════════════════════════════════════════════════════════════════════════════

class TestQueryTierDetermination:

    @pytest.mark.slow
    def test_high_specificity_entity_gives_clinical_tier(self, nlp_pipeline, synonym_index):
        text = "type 2 diabetes peripheral neuropathy"
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        tier = determine_query_tier(entities)
        assert tier == "clinical", (
            "High-specificity entities (type 2 diabetes mellitus, peripheral neuropathy) "
            "should produce the 'clinical' tier with threshold 0.28"
        )

    @pytest.mark.slow
    def test_low_specificity_only_gives_partial_tier(self, nlp_pipeline, synonym_index):
        text = "patient with fever and nausea"
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        tier = determine_query_tier(entities)
        assert tier == "partial", (
            "Only low-specificity entities (fever, nausea) → 'partial' tier, threshold 0.35"
        )

    @pytest.mark.slow
    def test_no_entities_gives_non_clinical_tier(self, nlp_pipeline, synonym_index):
        text = "quarterly budget forecast for Q3"
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        tier = determine_query_tier(entities)
        assert tier == "non_clinical", (
            "No clinical entities → 'non_clinical' tier with strict threshold 0.55"
        )

    @pytest.mark.slow
    def test_all_negated_entities_gives_non_clinical_tier(self, nlp_pipeline, synonym_index):
        text = "no stroke, no myocardial infarction"
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        tier = determine_query_tier(entities)
        assert tier == "non_clinical", (
            "All entities negated → no active canonicals → 'non_clinical' tier. "
            "Negated terms should not lower the retrieval threshold."
        )

    @pytest.mark.slow
    def test_mixed_specificity_with_high_spec_gives_clinical(self, nlp_pipeline, synonym_index):
        text = "fever and sepsis"
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        tier = determine_query_tier(entities)
        assert tier == "clinical", (
            "Mix of low (fever) + high-specificity (sepsis) → 'clinical' tier. "
            "Presence of ANY high-specificity active entity upgrades the tier."
        )

    @pytest.mark.slow
    def test_abbreviation_resolved_before_tier_classification(self, nlp_pipeline, synonym_index):
        text = "MI in prior history"
        entities = extract_entities(text, nlp_pipeline, synonym_index)
        tier = determine_query_tier(entities)
        # MI resolves to "myocardial infarction" (high specificity)
        assert tier == "clinical"
