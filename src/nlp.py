"""
Clinical NLP module.

Two interchangeable NER backends are supported (selected by config.NER_ENGINE):

  * "spacy"    (default) — spaCy model + clinical EntityRuler overlay built
                           from data/synonyms.json. Lightweight, no UMLS needed.
  * "medspacy" (opt-in)  — medspaCy + QuickUMLS UMLS-backed clinical NER.
                           Requires the medspacy/quickumls packages and a
                           QuickUMLS database (see download_dependencies.py).

Both backends produce the SAME entity-dict contract consumed downstream:
  {text, canonical, label, start, end, negated, specificity[, cui]}

Responsibilities:
  - Load the configured NER pipeline
  - Extract clinical entities from text
  - Detect negated entities (e.g. "no chest pain")
  - Expand terms using the concept dictionary / UMLS
  - Segment documents into labelled sections
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import spacy
from spacy.language import Language
from spacy.pipeline import EntityRuler

from .config import (
    NER_ENGINE, SPACY_MODEL, SYNONYMS_FILE,
    NEGATION_TRIGGERS, SECTION_BONUSES,
)


def is_medspacy_engine() -> bool:
    """True when the medspaCy + QuickUMLS backend is selected."""
    return NER_ENGINE == "medspacy"


def build_concept_index():
    """
    Return the concept index matching the configured NER engine.

      * spacy    -> SynonymIndex   (data/synonyms.json)
      * medspacy -> UMLSConceptIndex (UMLS-backed)

    Both satisfy the same interface, so callers can treat them uniformly.
    """
    if is_medspacy_engine():
        return UMLSConceptIndex()
    return SynonymIndex()


# ── Canonical tokenizer ───────────────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    """
    Shared tokenization contract: strip punctuation, lowercase, split.
    Used by BM25 indexing, synonym expansion, and raw query splitting so
    that query tokens always match index tokens exactly.
    """
    return re.sub(r'[^\w\s]', '', text).lower().split()


class ClinicalTokenizer:
    """
    Thin wrapper around :func:`tokenize` that provides a consistent interface
    for components that prefer object-oriented composition.
    """

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return tokenize(text)


# ── Negation scope dataclass ──────────────────────────────────────────────────

@dataclass
class NegationScope:
    """
    Carries negation detection result with diagnostic context.
    The bool :attr:`is_negated` is the public interface; the other fields
    aid debugging and future trigger-type–aware scoring.
    """
    is_negated: bool
    trigger: str = ""       # matched trigger phrase (empty if not negated)
    trigger_type: str = ""  # "pre_entity" or "clause_boundary_stopped"
    window_text: str = ""   # the lookback window that was actually searched


# ── Synonym index built from synonyms.json ────────────────────────────────────

class SynonymIndex:
    """
    Bidirectional lookup:
      term (any form) -> canonical name
      canonical name  -> list of all synonyms + metadata
    """

    def __init__(self, synonyms_file: Path = SYNONYMS_FILE):
        self._canonical_map: dict[str, dict] = {}   # canonical -> concept dict
        self._term_to_canonical: dict[str, str] = {}

        with open(synonyms_file, encoding="utf-8") as f:
            data = json.load(f)

        for concept in data["concepts"]:
            canonical = concept["canonical"].lower()
            self._canonical_map[canonical] = concept

            # Map canonical itself
            self._term_to_canonical[canonical] = canonical

            # Map every synonym
            for syn in concept.get("synonyms", []):
                self._term_to_canonical[syn.lower()] = canonical

    def get_canonical(self, term: str) -> Optional[str]:
        """Return canonical name for any term variant, or None."""
        return self._term_to_canonical.get(term.lower())

    def get_synonyms(self, canonical: str) -> list[str]:
        """Return all synonym variants for a canonical term."""
        concept = self._canonical_map.get(canonical.lower())
        if concept:
            return [canonical] + concept.get("synonyms", [])
        return [canonical]

    def get_all_terms(self) -> list[str]:
        """All known terms (canonical + synonyms)."""
        return list(self._term_to_canonical.keys())

    def get_concept(self, canonical: str) -> Optional[dict]:
        return self._canonical_map.get(canonical.lower())

    def get_specificity(self, canonical: str) -> str:
        concept = self._canonical_map.get(canonical.lower())
        return concept.get("specificity", "medium") if concept else "medium"

    @property
    def all_canonicals(self) -> list[str]:
        return list(self._canonical_map.keys())


# ── UMLS-backed concept index (medspaCy mode) ─────────────────────────────────

class UMLSConceptIndex:
    """
    Concept-index for the fully-UMLS (medspaCy + QuickUMLS) backend.

    Implements the SAME interface as :class:`SynonymIndex` so it is a drop-in
    for retrieval.py / indexing.py / explainer.py:
        get_canonical, get_synonyms, get_specificity,
        get_concept, get_all_terms, all_canonicals

    When a QuickUMLS database handle is supplied it can enrich canonical names,
    synonyms and specificity from UMLS.  Without one it degrades gracefully to
    surface-form identity with "medium" specificity so the pipeline still runs
    (POC behaviour) — no hard dependency on a live UMLS install at import time.
    """

    def __init__(self, quickumls_db=None):
        self._db = quickumls_db
        self._cui_to_name: dict[str, str] = {}
        self._name_to_cui: dict[str, str] = {}

    def register(self, cui: str, name: str) -> None:
        """Record a CUI -> preferred-name mapping (populated as entities are seen)."""
        if not cui or not name:
            return
        self._cui_to_name.setdefault(cui, name.lower())
        self._name_to_cui.setdefault(name.lower(), cui)

    def get_canonical(self, term: str) -> Optional[str]:
        """Return a canonical key for any surface form (falls back to the term itself)."""
        if not term:
            return None
        key = term.lower()
        if key in self._name_to_cui:
            return self._cui_to_name.get(self._name_to_cui[key], key)
        return key

    def get_synonyms(self, canonical: str) -> list[str]:
        """UMLS synonym expansion would be resolved here when a db is present."""
        return [canonical] if canonical else []

    def get_specificity(self, canonical: str) -> str:
        # Derive from UMLS semtypes when available; default to "medium".
        return "medium"

    def get_concept(self, canonical: str) -> Optional[dict]:
        if not canonical:
            return None
        concept = {"canonical": canonical, "source": "umls"}
        if canonical in self._name_to_cui:
            concept["cui"] = self._name_to_cui[canonical]
        return concept

    def get_all_terms(self) -> list[str]:
        return list(self._name_to_cui.keys())

    @property
    def all_canonicals(self) -> list[str]:
        return list(self._cui_to_name.values())


# ── spaCy pipeline setup ───────────────────────────────────────────────────────

def build_entity_patterns(synonym_index: SynonymIndex) -> list[dict]:
    """
    Build spaCy EntityRuler patterns from the synonym dictionary.
    Each term gets labelled as CLINICAL_ENTITY.
    """
    patterns = []
    for term, canonical in synonym_index._term_to_canonical.items():
        concept = synonym_index.get_concept(canonical)
        category = concept.get("category", "general") if concept else "general"
        label = f"CLINICAL_{category.upper()}"

        # Multi-word patterns need token-level matching
        tokens = term.split()
        if len(tokens) == 1:
            patterns.append({"label": label, "pattern": term})
        else:
            patterns.append({
                "label": label,
                "pattern": [{"LOWER": t} for t in tokens]
            })

    return patterns


def load_nlp(concept_index, engine: Optional[str] = None) -> Language:
    """
    Load a NER pipeline.

    ``engine`` selects the backend explicitly; when omitted it falls back to
    config.NER_ENGINE.  Passing it explicitly (as the test suite does) decouples
    a caller from the global default so each test exercises the engine it was
    written for, regardless of what the production default is.

      * "spacy"    — spaCy model + clinical EntityRuler (original behaviour).
      * "medspacy" — medspaCy pipeline with the QuickUMLS NER component enabled.

    ``concept_index`` is a SynonymIndex (spacy mode) or UMLSConceptIndex
    (medspacy mode); it is only used to build the spaCy EntityRuler patterns.
    """
    engine = (engine or NER_ENGINE).lower()
    if engine == "medspacy":
        return _load_medspacy_pipeline()

    # ── spaCy + clinical EntityRuler ─────────────────────────────────────────
    nlp = spacy.load(SPACY_MODEL, disable=["parser"])  # parser not needed, saves memory

    # Insert clinical EntityRuler before the built-in NER
    # (phrase_matcher_attr="LOWER" makes matching case-insensitive)
    ruler_config = {"phrase_matcher_attr": "LOWER", "overwrite_ents": True}
    ruler = nlp.add_pipe("entity_ruler", config=ruler_config, before="ner")
    ruler.add_patterns(build_entity_patterns(concept_index))

    return nlp


def _load_medspacy_pipeline() -> Language:
    """
    Build a medspaCy pipeline with the QuickUMLS NER component enabled.

    Imports are lazy so that the (heavier) medspacy/quickumls stack is only
    required when NER_ENGINE == "medspacy".  The exact pipe name and load()
    signature follow the official medspaCy QuickUMLS notebook; if a given
    medspacy release differs, adjust this single function.
    """
    try:
        import medspacy  # lazy import — only needed in medspacy mode
    except ImportError as exc:
        raise RuntimeError(
            "NER_ENGINE='medspacy' is selected but the 'medspacy' package is not "
            "installed.\n"
            "  Fix:      pip install -r requirements.txt\n"
            "  Or use the default engine instead:\n"
            "            set CLINICAL_NER_ENGINE=spacy   (or edit src/config.py)"
        ) from exc

    try:
        from medspacy.util import DEFAULT_PIPE_NAMES
        pipes = set(DEFAULT_PIPE_NAMES)
    except Exception:
        pipes = set()
    pipes.add("medspacy_quickumls")   # enables the SpacyQuickUMLS NER component

    return medspacy.load(enable=pipes)


def _pipeline_is_medspacy(nlp) -> bool:
    """
    Detect whether a loaded pipeline uses the QuickUMLS NER component by
    inspecting its pipe names.  This lets extract_entities interpret entities
    according to the ACTUAL pipeline rather than the global config flag, so a
    caller (e.g. the test suite) can pin either engine independently of the
    production default.
    """
    try:
        return any("quickumls" in name.lower() for name in nlp.pipe_names)
    except Exception:
        return False


# ── Entity extraction ─────────────────────────────────────────────────────────

def _check_negation(span, doc) -> NegationScope:
    """
    Window-based negation with clause-boundary stop and word-boundary matching.

    1. Start with a 6-token lookback window.
    2. Shrink the window: if a clause boundary (, . ; ! ? :) appears inside
       the window, the window starts *after* that punctuation.  This prevents
       negation triggers from a prior clause (e.g. "no stroke,") from bleeding
       into the next clause ("confirmed heart failure").
    3. Match triggers with word-boundary regex so that "no" inside "noted"
       does not fire a false positive.
    """
    window_start = max(0, span.start - 6)

    # Narrow window at the nearest clause boundary
    for i in range(span.start - 1, window_start - 1, -1):
        if doc[i].text in {',', '.', ';', '!', '?', ':'}:
            window_start = i + 1
            break

    window_text = doc[window_start:span.start].text.lower()

    for trigger in NEGATION_TRIGGERS:
        pattern = r'\b' + re.escape(trigger) + r'\b'
        if re.search(pattern, window_text):
            return NegationScope(
                is_negated=True,
                trigger=trigger,
                trigger_type="pre_entity",
                window_text=window_text,
            )

    return NegationScope(is_negated=False, window_text=window_text)


def is_negated(span, doc) -> bool:
    """
    Return True if the entity span is negated by a preceding trigger.
    Uses :func:`_check_negation` internally; callers that need diagnostic
    context should call that function directly.
    """
    return _check_negation(span, doc).is_negated


def extract_entities(
    text: str,
    nlp: Language,
    concept_index,
) -> list[dict]:
    """
    Run the configured NLP pipeline on text and return a list of entity dicts:
      {
        "text":        original matched text,
        "canonical":   canonical key (synonym dict or UMLS),
        "label":       engine entity label,
        "start":       char offset start,
        "end":         char offset end,
        "negated":     bool,
        "specificity": "high" | "medium" | "low",
        "cui":         UMLS CUI (medspacy mode only, optional),
      }

    ``concept_index`` is a SynonymIndex (spacy mode) or UMLSConceptIndex
    (medspacy mode).  Both backends yield the same dict contract, so the
    downstream retrieval / indexing / explainer code is engine-agnostic.
    """
    doc = nlp(text)
    entities: list[dict] = []
    seen_canonicals: set[str] = set()

    # Interpret entities according to the ACTUAL pipeline, not the global flag,
    # so a spacy pipeline is always read as spacy and vice-versa.
    medspacy_mode = _pipeline_is_medspacy(nlp)

    for ent in doc.ents:
        if medspacy_mode:
            canonical, negated, cui = _resolve_medspacy_ent(ent, concept_index)
            if canonical is None:
                continue
        else:
            if not ent.label_.startswith("CLINICAL_"):
                continue
            canonical = concept_index.get_canonical(ent.text)
            if canonical is None:
                continue
            negated = is_negated(ent, doc)
            cui = None

        # Deduplicate by canonical name per document
        if canonical in seen_canonicals:
            continue
        seen_canonicals.add(canonical)

        specificity = concept_index.get_specificity(canonical)

        entity = {
            "text":        ent.text,
            "canonical":   canonical,
            "label":       ent.label_,
            "start":       ent.start_char,
            "end":         ent.end_char,
            "negated":     negated,
            "specificity": specificity,
        }
        if cui:
            entity["cui"] = cui
            # Keep the UMLS concept index in sync for later lookups.
            if hasattr(concept_index, "register"):
                concept_index.register(cui, ent.text)

        entities.append(entity)

    return entities


def _resolve_medspacy_ent(ent, concept_index):
    """
    Resolve a medspaCy/QuickUMLS span to (canonical, negated, cui).

    Uses the native QuickUMLS match data attached to the span:
      ent._.umls_matches -> [ {cui, similarity, semtypes}, ... ]
      ent._.is_negated   -> bool
    Returns (None, False, None) when the span carries no UMLS match.
    """
    try:
        matches = ent._.umls_matches
    except AttributeError:
        return None, False, None
    if not matches:
        return None, False, None

    top = matches[0]
    cui = getattr(top, "cui", None)

    # Prefer a UMLS-resolved canonical name; fall back to the surface form.
    canonical = concept_index.get_canonical(ent.text) or ent.text.lower()
    negated = bool(getattr(ent._, "is_negated", False))
    return canonical, negated, cui


# ── Query expansion ───────────────────────────────────────────────────────────

def expand_query(
    entities: list[dict],
    synonym_index: SynonymIndex,
    include_negated: bool = False,
) -> list[str]:
    """
    Given extracted entities, return an expanded list of individual tokens
    derived from all synonym variants, for BM25 search.

    Multi-word synonyms (e.g. "myocardial infarction") are split using the
    shared :func:`tokenize` function so that each word is a separate BM25
    query token.  This ensures they match against individual corpus tokens
    rather than being treated as a single unsplittable string.

    Negated entities are excluded by default.
    """
    expanded: list[str] = []
    for ent in entities:
        if ent["negated"] and not include_negated:
            continue
        synonyms = synonym_index.get_synonyms(ent["canonical"])
        for syn in synonyms:
            expanded.extend(tokenize(syn))
    return list(set(expanded))


# ── Section segmentation ──────────────────────────────────────────────────────

# Ordered list of section header patterns (case-insensitive)
_SECTION_PATTERNS = [
    (r"chief complaint",             "chief complaint"),
    (r"history of present illness",  "history of present illness"),
    (r"past medical history",        "past medical history"),
    (r"medications?(?: on admission)?", "medications"),
    (r"physical examination",        "physical examination"),
    (r"laboratory(?: (?:results?|and imaging))?", "laboratory"),
    (r"imaging",                     "laboratory"),
    (r"assessment(?: and plan)?",    "assessment and plan"),
    (r"discharge instructions?",     "discharge instructions"),
    (r"follow.?up",                  "discharge instructions"),
]

_SECTION_RE = re.compile(
    r"={3,}\s*(" + "|".join(p for p, _ in _SECTION_PATTERNS) + r")\s*={3,}",
    re.IGNORECASE,
)


def segment_sections(text: str) -> list[dict]:
    """
    Split document text into labelled sections.
    Returns list of dicts: {"label": str, "text": str, "bonus": float}
    """
    sections: list[dict] = []
    last_label = "default"
    last_end = 0

    for m in _SECTION_RE.finditer(text):
        # Save the text accumulated before this header
        preceding = text[last_end:m.start()].strip()
        if preceding:
            bonus = SECTION_BONUSES.get(last_label, SECTION_BONUSES["default"])
            sections.append({"label": last_label, "text": preceding, "bonus": bonus})

        # Identify new section label
        header_text = m.group(1).lower()
        matched_label = "default"
        for pattern, label in _SECTION_PATTERNS:
            if re.search(pattern, header_text, re.IGNORECASE):
                matched_label = label
                break

        last_label = matched_label
        last_end = m.end()

    # Append the final section
    tail = text[last_end:].strip()
    if tail:
        bonus = SECTION_BONUSES.get(last_label, SECTION_BONUSES["default"])
        sections.append({"label": last_label, "text": tail, "bonus": bonus})

    # If no sections were found (plain text), treat whole doc as one section
    if not sections:
        sections.append({"label": "default", "text": text.strip(),
                          "bonus": SECTION_BONUSES["default"]})

    return sections


def determine_query_tier(entities: list[dict]) -> str:
    """
    Classify the query to select the right retrieval threshold.
    Returns "clinical", "partial", or "non_clinical".
    """
    if not entities:
        return "non_clinical"

    active = [e for e in entities if not e["negated"]]
    if not active:
        return "non_clinical"

    high_spec = any(e["specificity"] == "high" for e in active)
    return "clinical" if high_spec else "partial"
