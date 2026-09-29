"""
Retrieval module.

Responsibilities:
  - Process a query through the NLP pipeline
  - Run dense (vector) and sparse (BM25) retrieval
  - Fuse results with Reciprocal Rank Fusion (RRF)
  - Compute composite relevancy score per document
  - Enforce thresholds and return ranked results
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .config import (
    TOP_K_DENSE, TOP_K_SPARSE, TOP_K_FINAL,
    SCORE_WEIGHTS, THRESHOLDS, EXACT_MATCH_BTS_MIN,
)
from .nlp import (
    SynonymIndex, Language,
    extract_entities, expand_query, determine_query_tier, tokenize,
)
from .indexing import Embedder, VectorStore, BM25Index


# ── Query token set ───────────────────────────────────────────────────────────

@dataclass
class QueryTokenSet:
    """
    Separates BM25 query tokens into positive (to match) and negative
    (to suppress) sets.

    Positive tokens come from active synonym expansion plus filtered raw
    query words.  Negative tokens are the individual surface-form tokens
    of negated entities — used to prevent raw query splitting from
    reintroducing negated terms like "stroke" into the BM25 query after
    :func:`~nlp.expand_query` has correctly excluded them.
    """
    positive_tokens: list[str] = field(default_factory=list)
    negative_tokens: list[str] = field(default_factory=list)

    @property
    def bm25_tokens(self) -> list[str]:
        """Deduplicated tokens safe to pass to BM25 search."""
        return list(set(self.positive_tokens))


# ── Reciprocal Rank Fusion ────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = 60,
) -> dict[str, float]:
    """
    Standard RRF formula: score(d) = sum_list( 1 / (k + rank(d, list)) )
    Returns {section_id: rrf_score}.
    """
    scores: dict[str, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, section_id in enumerate(ranked, start=1):
            scores[section_id] += 1.0 / (k + rank)
    return dict(scores)


# ── Clinical Concept Overlap score ───────────────────────────────────────────

def concept_overlap_score(
    query_canonicals: set[str],
    doc_canonicals: set[str],
    synonym_index: SynonymIndex,
) -> float:
    """
    Weighted Jaccard-style concept overlap.
    Rare/specific concepts count more than common ones.
    Returns float 0-1.
    """
    if not query_canonicals or not doc_canonicals:
        return 0.0

    specificity_weights = {"high": 3.0, "medium": 1.5, "low": 0.5}
    intersection_score = 0.0
    union_score = 0.0
    all_concepts = query_canonicals | doc_canonicals

    for concept in all_concepts:
        w = specificity_weights.get(synonym_index.get_specificity(concept), 1.0)
        if concept in query_canonicals and concept in doc_canonicals:
            intersection_score += w
        union_score += w

    return intersection_score / union_score if union_score > 0 else 0.0


# ── Query processor ───────────────────────────────────────────────────────────

class QueryProcessor:
    def __init__(self, nlp: Language, synonym_index: SynonymIndex, embedder: Embedder):
        self.nlp = nlp
        self.synonym_index = synonym_index
        self.embedder = embedder

    def process(self, query_text: str) -> dict:
        """
        Full query processing pipeline.
        Returns a dict with everything needed for retrieval.
        """
        entities = extract_entities(query_text, self.nlp, self.synonym_index)
        tier = determine_query_tier(entities)
        threshold = THRESHOLDS[tier]

        active_entities = [e for e in entities if not e["negated"]]
        active_canonicals = set(e["canonical"] for e in active_entities)

        # Build the set of individual surface-form tokens for negated concepts
        # so we can prevent raw query splitting from re-introducing them.
        negated_entities = [e for e in entities if e["negated"]]
        negated_token_set: set[str] = set()
        for ent in negated_entities:
            for syn in self.synonym_index.get_synonyms(ent["canonical"]):
                negated_token_set.update(tokenize(syn))

        token_set = QueryTokenSet(
            positive_tokens=expand_query(entities, self.synonym_index),
            negative_tokens=list(negated_token_set),
        )

        # Add raw query tokens for terms not in the synonym dictionary,
        # but exclude any token that belongs to a negated entity's surface forms.
        raw_tokens = [t for t in tokenize(query_text) if t not in negated_token_set]
        token_set.positive_tokens = list(
            set(token_set.positive_tokens + raw_tokens)
        )

        # Query embedding
        embedding = self.embedder.encode_one(query_text)

        return {
            "query_text":         query_text,
            "entities":           entities,
            "active_canonicals":  active_canonicals,
            "tier":               tier,
            "threshold":          threshold,
            "bm25_tokens":        token_set.bm25_tokens,
            "embedding":          embedding,
        }


# ── Hybrid Retriever ──────────────────────────────────────────────────────────

class HybridRetriever:
    def __init__(
        self,
        vector_store: VectorStore,
        bm25_index: BM25Index,
        synonym_index: SynonymIndex,
        embedder: Embedder,
    ):
        self.vector_store = vector_store
        self.bm25_index = bm25_index
        self.synonym_index = synonym_index
        self.embedder = embedder

    def retrieve(self, processed_query: dict) -> list[dict]:
        """
        1. Dense retrieval from ChromaDB
        2. Sparse retrieval from BM25
        3. RRF fusion
        4. Composite scoring
        5. Threshold filtering
        6. Return top-k results (document-level, not section-level)
        """
        # ── Dense retrieval ──────────────────────────────────────────────────
        dense_hits = self.vector_store.query(
            processed_query["embedding"], n_results=TOP_K_DENSE
        )
        dense_section_ids = [h["section_id"] for h in dense_hits]
        dense_lookup = {h["section_id"]: h for h in dense_hits}

        # ── Sparse retrieval ─────────────────────────────────────────────────
        sparse_hits = self.bm25_index.search(
            processed_query["bm25_tokens"], n_results=TOP_K_SPARSE
        )
        sparse_section_ids = [h["section_id"] for h in sparse_hits]
        sparse_lookup = {h["section_id"]: h for h in sparse_hits}

        # ── Merge all section metadata ───────────────────────────────────────
        all_sections: dict[str, dict] = {}
        for h in dense_hits + sparse_hits:
            sid = h["section_id"]
            if sid not in all_sections:
                all_sections[sid] = h

        # ── RRF fusion ───────────────────────────────────────────────────────
        rrf_scores = reciprocal_rank_fusion([dense_section_ids, sparse_section_ids])

        # ── Aggregate to document level ───────────────────────────────────────
        # A document may have multiple sections; take the best section per doc.
        doc_best: dict[str, dict] = {}

        for sid, rrf_score in rrf_scores.items():
            section = all_sections.get(sid)
            if section is None:
                continue

            doc_id = section["document_id"]
            doc_canonicals = set(
                c.strip()
                for c in section.get("canonical_entities", "").split(",")
                if c.strip()
            )
            negated_canonicals = set(
                c.strip()
                for c in section.get("negated_entities", "").split(",")
                if c.strip()
            )

            # Compute individual score components
            cco = concept_overlap_score(
                processed_query["active_canonicals"],
                doc_canonicals,
                self.synonym_index,
            )
            scs = section.get("cosine_similarity", 0.0)
            bts = sparse_lookup.get(sid, {}).get("bm25_score_norm", 0.0)
            srb = section.get("section_bonus", 0.5)

            composite = (
                SCORE_WEIGHTS["cco"] * cco +
                SCORE_WEIGHTS["scs"] * scs +
                SCORE_WEIGHTS["bts"] * bts +
                SCORE_WEIGHTS["srb"] * srb
            )

            # Keep highest-scoring section per document
            if doc_id not in doc_best or composite > doc_best[doc_id]["composite"]:
                doc_best[doc_id] = {
                    "document_id":       doc_id,
                    "document_path":     section["document_path"],
                    "best_section":      section,
                    "composite":         composite,
                    "scores": {
                        "cco": round(cco, 4),
                        "scs": round(scs, 4),
                        "bts": round(bts, 4),
                        "srb": round(srb, 4),
                    },
                    "rrf_score":          rrf_score,
                    "doc_canonicals":     doc_canonicals,
                    "negated_canonicals": negated_canonicals,
                    "retrieval_paths":    self._retrieval_paths(sid, dense_lookup, sparse_lookup),
                }

        # ── Threshold filtering ───────────────────────────────────────────────
        threshold = processed_query["threshold"]
        passing = []
        for doc in doc_best.values():
            if doc["composite"] >= threshold:
                doc["exact_match_override"] = False
                passing.append(doc)
            elif doc["scores"]["bts"] >= EXACT_MATCH_BTS_MIN:
                doc["exact_match_override"] = True
                passing.append(doc)

        # ── Sort and return top-k ─────────────────────────────────────────────
        passing.sort(key=lambda d: d["composite"], reverse=True)
        return passing[:TOP_K_FINAL]

    @staticmethod
    def _retrieval_paths(
        section_id: str,
        dense_lookup: dict,
        sparse_lookup: dict,
    ) -> str:
        paths = []
        if section_id in dense_lookup:
            paths.append("dense")
        if section_id in sparse_lookup:
            paths.append("sparse")
        return " + ".join(paths) if paths else "unknown"
