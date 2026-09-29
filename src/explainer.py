"""
Explainability module.

For every retrieved document, generates a human-readable RetrievalExplanation
that clearly states WHY the document was returned.
"""

from __future__ import annotations

from pathlib import Path

from .config import THRESHOLDS, EXACT_MATCH_BTS_MIN
from .nlp import SynonymIndex


def _find_key_passage(section_text: str, query_tokens: list[str], window: int = 600) -> str:
    """
    Return a passage window centred on the first query-token hit in section_text.
    Falls back to the opening characters when no token is found.
    Adds '…' prefix/suffix when the window does not start/end at a boundary.
    """
    lower = section_text.lower()
    earliest = len(section_text)
    for token in query_tokens:
        if not token:
            continue
        pos = lower.find(token)
        if 0 <= pos < earliest:
            earliest = pos

    if earliest == len(section_text):
        # No token found — semantic match, show opening
        return section_text[:window].strip()

    start = max(0, earliest - window // 3)
    end   = min(len(section_text), start + window)
    passage = section_text[start:end].strip()
    return ("…" if start > 0 else "") + passage + ("…" if end < len(section_text) else "")


class RetrievalExplanation:
    """Structured explanation for a single retrieved document."""

    def __init__(
        self,
        document_id: str,
        document_path: str,
        composite_score: float,
        threshold: float,
        scores: dict,
        query_canonicals: set[str],
        doc_canonicals: set[str],
        negated_canonicals: set[str],
        best_section_label: str,
        best_section_text: str,
        retrieval_paths: str,
        synonym_index: SynonymIndex,
        query_entities: list[dict],
        query_tokens: list[str],
        exact_match_override: bool = False,
    ):
        self.document_id          = document_id
        self.document_path        = document_path
        self.composite_score      = composite_score
        self.threshold            = threshold
        self.scores               = scores
        self.retrieval_paths      = retrieval_paths
        self.best_section_label   = best_section_label
        self.exact_match_override = exact_match_override

        # Compute matched and synonym-matched concepts
        self.matched_concepts  = sorted(query_canonicals & doc_canonicals)
        self.negation_warnings = sorted(query_canonicals & negated_canonicals)

        # Find synonym matches (query term != canonical, but canonical matched)
        self.synonym_matches: list[tuple[str, str]] = []
        for ent in query_entities:
            if ent["negated"]:
                continue
            canonical = ent["canonical"]
            original  = ent["text"].lower()
            if canonical in doc_canonicals and original != canonical:
                self.synonym_matches.append((original, canonical))

        self.key_passage = _find_key_passage(best_section_text, query_tokens)
        self.margin      = round(composite_score - threshold, 4)

    def is_borderline(self) -> bool:
        return not self.exact_match_override and self.margin < 0.05

    def format(self, rank: int) -> str:
        """Render a clean plain-text explanation block."""
        doc_name = Path(self.document_path).name
        border = "─" * 60

        if self.exact_match_override:
            score_line = (
                f"  Overall Relevancy Score : {self.composite_score:.4f}  "
                f"(composite threshold: {self.threshold:.2f}  |  ⚡ EXACT TERM MATCH)"
            )
        else:
            score_line = (
                f"  Overall Relevancy Score : {self.composite_score:.4f}  "
                f"(threshold: {self.threshold:.2f}  |  margin: +{self.margin:.4f})"
                + ("  ⚠ BORDERLINE" if self.is_borderline() else "")
            )

        lines = [
            f"\n{'═' * 60}",
            f"  RESULT #{rank}  —  {doc_name}",
            f"{'═' * 60}",
            score_line,
            "",
            f"  Score Breakdown:",
            f"    Clinical Concept Overlap (40%) : {self.scores['cco']:.4f}",
            f"    Semantic Similarity      (30%) : {self.scores['scs']:.4f}",
            f"    BM25 Term Score          (20%) : {self.scores['bts']:.4f}",
            f"    Section Relevance Bonus  (10%) : {self.scores['srb']:.4f}",
            "",
        ]

        if self.exact_match_override:
            lines.append(
                f"  ⚡ Returned via exact term match  "
                f"(BM25: {self.scores['bts']:.4f} ≥ {EXACT_MATCH_BTS_MIN:.2f} override threshold)"
            )
            lines.append("")

        if self.matched_concepts:
            lines.append(f"  Matched Clinical Concepts ({len(self.matched_concepts)}):")
            for c in self.matched_concepts:
                lines.append(f"    • {c}")
        else:
            lines.append("  Matched Clinical Concepts : (none — retrieved via semantic similarity)")

        if self.synonym_matches:
            lines.append(f"\n  Synonym Matches (query term → document term):")
            for query_term, canonical in self.synonym_matches:
                lines.append(f"    • \"{query_term}\"  →  \"{canonical}\"")

        if self.negation_warnings:
            lines.append(f"\n  ⚠ NEGATION WARNINGS — these concepts appear NEGATED in the document:")
            for c in self.negation_warnings:
                lines.append(f"    • \"{c}\" (e.g. 'no {c}', 'denied {c}')")

        lines.extend([
            f"\n  Best Matching Section : [{self.best_section_label.upper()}]",
            f"  Retrieval Path        : {self.retrieval_paths}",
            f"\n  Key Passage:",
            "  " + border,
        ])
        for chunk in [self.key_passage[i:i+80] for i in range(0, len(self.key_passage), 80)]:
            lines.append(f"  {chunk}")
        lines.append("  " + border)

        return "\n".join(lines)


def build_explanation(
    result: dict,
    processed_query: dict,
    synonym_index: SynonymIndex,
) -> RetrievalExplanation:
    """Factory: create a RetrievalExplanation from a retrieval result dict."""
    return RetrievalExplanation(
        document_id           = result["document_id"],
        document_path         = result["document_path"],
        composite_score       = round(result["composite"], 4),
        threshold             = processed_query["threshold"],
        scores                = result["scores"],
        query_canonicals      = processed_query["active_canonicals"],
        doc_canonicals        = result["doc_canonicals"],
        negated_canonicals    = result["negated_canonicals"],
        best_section_label    = result["best_section"]["section_label"],
        best_section_text     = result["best_section"]["section_text"],
        retrieval_paths       = result["retrieval_paths"],
        synonym_index         = synonym_index,
        query_entities        = processed_query["entities"],
        query_tokens          = processed_query["bm25_tokens"],
        exact_match_override  = result.get("exact_match_override", False),
    )


def format_zero_result(processed_query: dict) -> str:
    """Explain why no documents were returned."""
    tier      = processed_query["tier"]
    threshold = processed_query["threshold"]
    entities  = processed_query["entities"]
    active    = [e for e in entities if not e["negated"]]

    lines = [
        "\n" + "═" * 60,
        "  NO RESULTS RETURNED",
        "═" * 60,
        f"  Query     : \"{processed_query['query_text']}\"",
        f"  Query Tier: {tier.upper()}   (threshold applied: {threshold})",
        "",
    ]

    if not active:
        lines.append("  Reason: No recognised clinical entities were found in this query.")
        if entities:
            negated = [e["canonical"] for e in entities if e["negated"]]
            lines.append(f"          All found entities were negated: {negated}")
        lines.extend([
            "",
            "  Suggestions:",
            "    • Use clinical terminology (diagnoses, symptoms, medications)",
            "    • Check spelling of medical terms",
            "    • Try the canonical name, e.g. 'myocardial infarction' instead of 'heart attack'",
        ])
    else:
        found = [e["canonical"] for e in active]
        lines.append(f"  Recognised entities : {found}")
        lines.append(f"  No document scored above the threshold of {threshold}.")
        lines.extend([
            "",
            "  Suggestions:",
            "    • Try more specific clinical terms",
            "    • Broaden the query (remove over-constraining terms)",
            "    • Verify that relevant documents have been indexed",
        ])

    return "\n".join(lines)
