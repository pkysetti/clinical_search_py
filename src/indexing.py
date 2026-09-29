"""
Indexing module.

Responsibilities:
  - Embed text using sentence-transformers (dense vectors)
  - Store documents in ChromaDB (persisted to disk)
  - Build and persist a BM25 index (rank_bm25, pickled to disk)
  - Orchestrate document ingestion end-to-end
"""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi
# Heavy dependencies (chromadb, sentence_transformers) are imported lazily
# inside their respective class __init__ methods so that retrieval.py and
# explainer.py can be imported and unit-tested without the full ML stack.

from .config import (
    CHROMA_DIR, BM25_DIR, EMBEDDING_MODEL,
    DOCUMENTS_DIR,
)
from .nlp import (
    SynonymIndex, build_concept_index, load_nlp,
    extract_entities, expand_query, segment_sections, tokenize,
)


# ── Embedder ──────────────────────────────────────────────────────────────────

class Embedder:
    """Wraps sentence-transformers for consistent encoding."""

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        from sentence_transformers import SentenceTransformer  # lazy import
        print(f"  Loading embedding model: {model_name}")
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]) -> list[list[float]]:
        embeddings = self.model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        return embeddings.tolist()

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]


# ── ChromaDB vector store ─────────────────────────────────────────────────────

class VectorStore:
    """
    Persisted ChromaDB collection.
    One document can be split into multiple sections; each section gets its
    own embedding but stores the parent document ID in metadata.
    """

    def __init__(self, persist_dir: Path = CHROMA_DIR):
        import chromadb  # lazy import
        # chromadb >=0.4 uses PersistentClient
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._col = self._client.get_or_create_collection(
            name="clinical_docs",
            metadata={"hnsw:space": "cosine"},
        )

    def add_section(
        self,
        section_id: str,
        embedding: list[float],
        document_id: str,
        document_path: str,
        section_label: str,
        section_text: str,
        section_bonus: float,
        canonical_entities: str,   # comma-separated
        negated_entities: str,     # comma-separated
    ) -> None:
        """Upsert a single section into the collection."""
        # ChromaDB metadata values must be str / int / float / bool
        self._col.upsert(
            ids=[section_id],
            embeddings=[embedding],
            documents=[section_text],
            metadatas=[{
                "document_id":       document_id,
                "document_path":     document_path,
                "section_label":     section_label,
                "section_bonus":     section_bonus,
                "canonical_entities": canonical_entities,
                "negated_entities":  negated_entities,
            }],
        )

    def query(
        self,
        query_embedding: list[float],
        n_results: int = 20,
    ) -> list[dict]:
        """
        Return top-n sections by cosine similarity.
        Each result dict: {section_id, document_id, document_path,
                           section_label, section_text, section_bonus,
                           canonical_entities, negated_entities, distance}
        """
        results = self._col.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, self._col.count()),
            include=["documents", "metadatas", "distances"],
        )

        hits = []
        ids       = results["ids"][0]
        docs      = results["documents"][0]
        metas     = results["metadatas"][0]
        distances = results["distances"][0]

        for sid, text, meta, dist in zip(ids, docs, metas, distances):
            hits.append({
                "section_id":         sid,
                "document_id":        meta["document_id"],
                "document_path":      meta["document_path"],
                "section_label":      meta["section_label"],
                "section_text":       text,
                "section_bonus":      meta["section_bonus"],
                "canonical_entities": meta.get("canonical_entities", ""),
                "negated_entities":   meta.get("negated_entities", ""),
                # cosine distance → similarity (ChromaDB cosine returns distance 0-2)
                "cosine_similarity":  1.0 - (dist / 2.0),
            })

        return hits

    def count(self) -> int:
        return self._col.count()


# ── BM25 sparse index ─────────────────────────────────────────────────────────

class BM25Index:
    """
    rank_bm25 index persisted as a pickle file.
    Stores tokenised sections alongside the BM25 model so that
    original text can be recovered for display.
    """

    _BM25_FILE = BM25_DIR / "bm25_index.pkl"
    _META_FILE = BM25_DIR / "bm25_meta.json"

    def __init__(self):
        self._bm25: BM25Okapi | None = None
        self._sections: list[dict] = []   # mirrors the Chroma sections

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize using the shared canonical tokenizer (punctuation stripped)."""
        return tokenize(text)

    def build(self, sections: list[dict]) -> None:
        """Build BM25 index from a list of section dicts (must have 'section_text')."""
        self._sections = sections
        corpus = [self._tokenize(s["section_text"]) for s in sections]
        self._bm25 = BM25Okapi(corpus)

    def add_section(self, section: dict) -> None:
        """Incrementally add one section and rebuild. For POC size this is fine."""
        self._sections.append(section)
        corpus = [self._tokenize(s["section_text"]) for s in self._sections]
        self._bm25 = BM25Okapi(corpus)

    def search(self, query_terms: list[str], n_results: int = 20) -> list[dict]:
        """
        Return top-n sections by BM25 score.
        query_terms: list of tokens (already expanded by synonym lookup).
        """
        if self._bm25 is None or not self._sections:
            return []

        scores = self._bm25.get_scores(query_terms)
        max_score = max(scores) if max(scores) > 0 else 1.0

        # Rank and take top-n
        ranked = sorted(
            enumerate(scores), key=lambda x: x[1], reverse=True
        )[:n_results]

        hits = []
        for idx, score in ranked:
            if score <= 0:
                continue
            section = self._sections[idx].copy()
            section["bm25_score"] = float(score)
            section["bm25_score_norm"] = float(score / max_score)   # 0-1
            hits.append(section)

        return hits

    def save(self) -> None:
        """Persist index to disk."""
        with open(self._BM25_FILE, "wb") as f:
            pickle.dump(self._bm25, f)
        with open(self._META_FILE, "w", encoding="utf-8") as f:
            json.dump(self._sections, f, ensure_ascii=False)

    def load(self) -> bool:
        """Load index from disk. Returns True if successful."""
        if not self._BM25_FILE.exists():
            return False
        with open(self._BM25_FILE, "rb") as f:
            self._bm25 = pickle.load(f)
        with open(self._META_FILE, encoding="utf-8") as f:
            self._sections = json.load(f)
        return True


# ── Document Indexer (orchestrator) ──────────────────────────────────────────

class DocumentIndexer:
    """
    Loads documents from DOCUMENTS_DIR, runs the full NLP pipeline,
    and populates both the vector store and BM25 index.
    """

    def __init__(self):
        # Engine-aware concept index: SynonymIndex (spacy) or UMLSConceptIndex (medspacy).
        self.synonym_index = build_concept_index()
        self.nlp = load_nlp(self.synonym_index)
        self.embedder = Embedder()
        self.vector_store = VectorStore()
        self.bm25_index = BM25Index()

    def _doc_id(self, path: Path) -> str:
        return path.stem

    def _section_id(self, doc_id: str, section_label: str, idx: int) -> str:
        raw = f"{doc_id}::{section_label}::{idx}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def index_document(self, path: Path) -> int:
        """
        Index a single text file. Returns number of sections indexed.
        """
        text = path.read_text(encoding="utf-8")
        doc_id = self._doc_id(path)
        doc_path_str = str(path)

        sections = segment_sections(text)
        sections_indexed = 0

        for i, section in enumerate(sections):
            sec_text = section["text"]
            if len(sec_text.strip()) < 20:
                continue   # skip near-empty sections

            # NLP enrichment
            entities = extract_entities(sec_text, self.nlp, self.synonym_index)
            active_canonicals = [e["canonical"] for e in entities if not e["negated"]]
            negated_canonicals = [e["canonical"] for e in entities if e["negated"]]

            # Embed
            embedding = self.embedder.encode_one(sec_text)

            # Build section metadata dict (shared between both indexes)
            section_meta = {
                "section_id":         self._section_id(doc_id, section["label"], i),
                "document_id":        doc_id,
                "document_path":      doc_path_str,
                "section_label":      section["label"],
                "section_text":       sec_text,
                "section_bonus":      section["bonus"],
                "canonical_entities": ", ".join(active_canonicals),
                "negated_entities":   ", ".join(negated_canonicals),
            }

            # Add to vector store
            self.vector_store.add_section(
                section_id        = section_meta["section_id"],
                embedding         = embedding,
                document_id       = section_meta["document_id"],
                document_path     = section_meta["document_path"],
                section_label     = section_meta["section_label"],
                section_text      = section_meta["section_text"],
                section_bonus     = section_meta["section_bonus"],
                canonical_entities= section_meta["canonical_entities"],
                negated_entities  = section_meta["negated_entities"],
            )

            # Add to BM25
            self.bm25_index.add_section(section_meta)
            sections_indexed += 1

        return sections_indexed

    def index_all(self, documents_dir: Path = DOCUMENTS_DIR) -> None:
        """Index every .txt file in the documents directory."""
        files = sorted(documents_dir.glob("*.txt"))
        if not files:
            print(f"No .txt files found in {documents_dir}")
            return

        print(f"\nIndexing {len(files)} documents...\n")
        total_sections = 0

        for path in files:
            print(f"  [{path.name}] ", end="", flush=True)
            count = self.index_document(path)
            total_sections += count
            print(f"{count} sections indexed")

        # Persist BM25 to disk
        self.bm25_index.save()
        print(f"\nDone. Total sections in index: {total_sections}")
        print(f"Vector store sections: {self.vector_store.count()}")
