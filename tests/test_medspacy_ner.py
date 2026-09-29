"""Tests for the medspaCy + QuickUMLS NER backend (NER_ENGINE="medspacy").

These are the heaviest tests in the suite: they require the medspacy/quickumls
packages AND a loadable QuickUMLS pipeline.  They skip gracefully when either
is unavailable, so the default ("spacy") suite stays fast and green.

Run explicitly with:
    pytest -m medspacy
"""

import importlib.util

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.medspacy]


def _medspacy_available() -> bool:
    return importlib.util.find_spec("medspacy") is not None


@pytest.fixture(scope="module")
def medspacy_pipeline():
    """Load a medspaCy pipeline with QuickUMLS enabled; skip if unavailable."""
    if not _medspacy_available():
        pytest.skip("medspacy package not installed")

    import src.nlp as nlp_mod

    try:
        return nlp_mod._load_medspacy_pipeline()
    except Exception as exc:  # noqa: BLE001 — any load failure => skip, don't fail
        pytest.skip(f"could not load medspaCy pipeline: {exc}")


def test_engine_flag_selects_medspacy(monkeypatch):
    """NER_ENGINE='medspacy' routes to the UMLS concept index."""
    import src.nlp as nlp_mod

    monkeypatch.setattr(nlp_mod, "NER_ENGINE", "medspacy")
    assert nlp_mod.is_medspacy_engine() is True
    idx = nlp_mod.build_concept_index()
    assert isinstance(idx, nlp_mod.UMLSConceptIndex)


def test_uml_concept_index_interface():
    """UMLSConceptIndex exposes the same interface as SynonymIndex (drop-in)."""
    import src.nlp as nlp_mod

    idx = nlp_mod.UMLSConceptIndex()
    for method in ("get_canonical", "get_synonyms", "get_specificity",
                   "get_concept", "get_all_terms"):
        assert callable(getattr(idx, method)), f"missing {method}"
    assert isinstance(idx.all_canonicals, list)

    # Graceful fallbacks — no live UMLS db required.
    assert idx.get_specificity("diabetes") == "medium"
    assert idx.get_synonyms("diabetes") == ["diabetes"]
    assert idx.get_canonical("Diabetes") == "diabetes"


def test_entity_contract(medspacy_pipeline, monkeypatch):
    """extract_entities yields the shared entity-dict contract in medspacy mode."""
    import src.nlp as nlp_mod

    monkeypatch.setattr(nlp_mod, "NER_ENGINE", "medspacy")
    concept_index = nlp_mod.UMLSConceptIndex()

    text = "Patient with type 2 diabetes and peripheral neuropathy."
    entities = nlp_mod.extract_entities(text, medspacy_pipeline, concept_index)

    assert isinstance(entities, list)
    required = {"text", "canonical", "label", "start", "end", "negated", "specificity"}
    for ent in entities:
        assert required.issubset(ent.keys()), f"missing keys in {ent}"
        assert ent["specificity"] in {"high", "medium", "low"}
        assert isinstance(ent["negated"], bool)
