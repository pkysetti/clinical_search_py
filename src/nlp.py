"""
Clinical NLP module.

Responsibilities:
  - Load spaCy model with a clinical EntityRuler overlay
  - Extract clinical entities from text
  - Detect negated entities (e.g. "no chest pain")
  - Expand terms using the synonym dictionary
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

from .config import SPACY_MODEL, SYNONYMS_FILE, NEGATION_TRIGGERS, SECTION_BONUSES


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


def load_nlp(synonym_index: SynonymIndex) -> Language:
    """
    Load spaCy model and inject a clinical EntityRuler before the standard NER.
    Returns the configured pipeline.
    """
    nlp = spacy.load(SPACY_MODEL, disable=["parser"])  # parser not needed, saves memory

    # Insert clinical EntityRuler before the built-in NER
    # (phrase_matcher_attr="LOWER" makes matching case-insensitive)
    ruler_config = {"phrase_matcher_attr": "LOWER", "overwrite_ents": True}
    ruler = nlp.add_pipe("entity_ruler", config=ruler_config, before="ner")
    ruler.add_patterns(build_entity_patterns(synonym_index))

    return nlp


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
    synonym_index: SynonymIndex,
) -> list[dict]:
    """
    Run NLP pipeline on text and return a list of entity dicts:
      {
        "text":      original matched text,
        "canonical": canonical name from synonym dictionary,
        "label":     spaCy entity label (CLINICAL_*),
        "start":     char offset start,
        "end":       char offset end,
        "negated":   bool,
        "specificity": "high" | "medium" | "low",
      }
    """
    doc = nlp(text)
    entities = []
    seen_canonicals: set[str] = set()

    for ent in doc.ents:
        if not ent.label_.startswith("CLINICAL_"):
            continue

        canonical = synonym_index.get_canonical(ent.text)
        if canonical is None:
            continue

        # Deduplicate by canonical name per document
        if canonical in seen_canonicals:
            continue
        seen_canonicals.add(canonical)

        negated = is_negated(ent, doc)
        specificity = synonym_index.get_specificity(canonical)

        entities.append({
            "text":        ent.text,
            "canonical":   canonical,
            "label":       ent.label_,
            "start":       ent.start_char,
            "end":         ent.end_char,
            "negated":     negated,
            "specificity": specificity,
        })

    return entities


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
