"""
test_dense.py — SentenceTransformer embeddings → ChromaDB HNSW vector index.

PASS / FAIL GUIDE
─────────────────────────────────────────────────────────────────────────────
Test                                         Expected today    Root cause
─────────────────────────────────────────────────────────────────────────────
Embedder: vector shape, normalisation        PASS              standard ST API
VectorStore: add/query/count                 PASS              ChromaDB OK
VectorStore: semantic ranking                PASS              real embeddings
DocumentIndexer: valid embedding stored      *** FAILS ***     See BUG below
DocumentIndexer: sections indexed in BM25   PASS              BM25 unaffected

BUG in src/indexing.py — DocumentIndexer.index_document()
──────────────────────────────────────────────────────────
Line (approximately 118):
    self.vector_store.add_section(
        ...
        embedding = section_meta["section_id"],   ← WRONG (hex string)
        ...
    )

The local variable `embedding` (a List[float] from Embedder.encode_one) is
computed immediately above but is never used.  Instead, the section_id hex
string is passed as the embedding, causing ChromaDB to raise:
    ValueError / TypeError: embeddings must be floats

Fix: change `embedding = section_meta["section_id"]` to `embedding = embedding`.

The test `test_indexer_stores_valid_float_embedding` will FAIL today because
index_document() raises before inserting anything.  All other dense tests use
VectorStore directly (bypassing the bug) and should PASS.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.indexing import Embedder, VectorStore, BM25Index
from tests.conftest import make_test_indexer


# ══════════════════════════════════════════════════════════════════════════════
# Embedder
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
class TestEmbedder:

    def test_encode_one_returns_list_of_floats(self, embedder):
        vec = embedder.encode_one("Patient has diabetes.")
        assert isinstance(vec, list)
        assert all(isinstance(v, float) for v in vec)

    def test_embedding_is_unit_normalised(self, embedder):
        import math
        vec = embedder.encode_one("Clinical text for normalisation check.")
        norm = math.sqrt(sum(v * v for v in vec))
        assert norm == pytest.approx(1.0, abs=1e-5), (
            "Embedder uses normalize_embeddings=True so output must be L2-normalised"
        )

    def test_embedding_dimension_is_384(self, embedder):
        """all-MiniLM-L6-v2 produces 384-dimensional embeddings."""
        vec = embedder.encode_one("Test sentence.")
        assert len(vec) == 384, (
            f"all-MiniLM-L6-v2 should produce 384-dim embeddings, got {len(vec)}"
        )

    def test_similar_clinical_texts_high_cosine(self, embedder):
        """Semantically close clinical phrases should have high dot-product."""
        import numpy as np
        e1 = np.array(embedder.encode_one("type 2 diabetes mellitus treatment"))
        e2 = np.array(embedder.encode_one("diabetic patient glucose control"))
        sim = float(e1 @ e2)  # both are unit vectors → dot = cosine similarity
        assert sim > 0.5, (
            f"Related clinical phrases should have cosine similarity > 0.5, got {sim:.4f}"
        )

    def test_unrelated_texts_lower_cosine(self, embedder):
        import numpy as np
        e1 = np.array(embedder.encode_one("type 2 diabetes mellitus"))
        e2 = np.array(embedder.encode_one("quarterly budget forecast for Q3"))
        sim = float(e1 @ e2)
        assert sim < 0.5, (
            f"Clinical vs financial text should have cosine similarity < 0.5, got {sim:.4f}"
        )

    def test_batch_encode_consistent_with_single(self, embedder):
        import numpy as np
        texts = ["Patient has diabetes.", "Heart failure exacerbation."]
        batch = embedder.encode(texts)
        single_0 = embedder.encode_one(texts[0])
        single_1 = embedder.encode_one(texts[1])

        sim_0 = float(np.array(batch[0]) @ np.array(single_0))
        sim_1 = float(np.array(batch[1]) @ np.array(single_1))
        assert sim_0 > 0.999, "Batch and single encoding should be numerically identical"
        assert sim_1 > 0.999


# ══════════════════════════════════════════════════════════════════════════════
# VectorStore (ChromaDB)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
class TestVectorStore:

    def test_add_and_count(self, tmp_path, embedder):
        vs = VectorStore(persist_dir=tmp_path / "chroma")
        assert vs.count() == 0
        emb = embedder.encode_one("Diabetic patient with neuropathy.")
        vs.add_section(
            section_id="sec_001",
            embedding=emb,
            document_id="doc_test",
            document_path="/test/doc.txt",
            section_label="assessment and plan",
            section_text="Diabetic patient with neuropathy.",
            section_bonus=1.0,
            canonical_entities="type 2 diabetes mellitus, peripheral neuropathy",
            negated_entities="",
        )
        assert vs.count() == 1

    def test_query_returns_list(self, tmp_path, embedder):
        vs = VectorStore(persist_dir=tmp_path / "chroma2")
        emb = embedder.encode_one("Heart failure.")
        vs.add_section("s1", emb, "d1", "/p.txt", "default", "Heart failure.", 1.0, "heart failure", "")
        results = vs.query(emb, n_results=1)
        assert isinstance(results, list)
        assert len(results) == 1

    def test_query_result_has_required_keys(self, tmp_path, embedder):
        vs = VectorStore(persist_dir=tmp_path / "chroma3")
        emb = embedder.encode_one("COPD exacerbation.")
        vs.add_section("s1", emb, "d1", "/p.txt", "assessment and plan", "COPD exacerbation.", 1.0, "chronic obstructive pulmonary disease", "")
        results = vs.query(emb, n_results=1)
        r = results[0]
        required = ["section_id", "document_id", "document_path", "section_label",
                    "section_text", "section_bonus", "cosine_similarity"]
        for key in required:
            assert key in r, f"Missing key '{key}' in VectorStore result"

    def test_cosine_similarity_in_0_1_range(self, tmp_path, embedder):
        """
        With normalised embeddings the cosine similarity of any two text vectors
        is ≥ 0 (texts share positive semantic space), so similarity ∈ [0, 1].
        Note: the codebase computes  sim = 1 - dist/2  which slightly inflates
        scores; see formula note in VectorStore.query().
        """
        vs = VectorStore(persist_dir=tmp_path / "chroma4")
        e1 = embedder.encode_one("Diabetes type 2 with neuropathy.")
        e2 = embedder.encode_one("Quarterly budget report Q3.")
        vs.add_section("s1", e1, "d1", "/p.txt", "default", "Diabetes type 2.", 1.0, "", "")
        results = vs.query(e2, n_results=1)
        assert len(results) == 1
        sim = results[0]["cosine_similarity"]
        assert 0.0 <= sim <= 1.0, f"cosine_similarity {sim} is out of [0, 1]"

    def test_identical_query_has_highest_similarity(self, tmp_path, embedder):
        vs = VectorStore(persist_dir=tmp_path / "chroma5")
        texts = [
            "Sepsis secondary to urinary tract infection.",
            "Heart failure with atrial fibrillation.",
            "COPD exacerbation with pneumonia.",
        ]
        for i, t in enumerate(texts):
            vs.add_section(f"s{i}", embedder.encode_one(t), f"d{i}", "/p.txt", "default", t, 1.0, "", "")

        query_emb = embedder.encode_one(texts[0])
        results = vs.query(query_emb, n_results=3)
        top = results[0]
        assert top["document_id"] == "d0", (
            "The document whose text was used as the query should be the top result"
        )

    def test_most_semantically_similar_doc_ranked_first(self, tmp_path, embedder):
        vs = VectorStore(persist_dir=tmp_path / "chroma6")
        # Two docs: one about diabetes, one about cardiology
        e_diabetes = embedder.encode_one(
            "Type 2 diabetes mellitus with peripheral neuropathy and HbA1c 9.8%."
        )
        e_cardiac  = embedder.encode_one(
            "Atrial fibrillation with rapid ventricular response. Rate control."
        )
        vs.add_section("s_diab", e_diabetes, "doc_diabetes", "/d.txt", "default",
                       "Type 2 diabetes mellitus.", 1.0, "", "")
        vs.add_section("s_card", e_cardiac, "doc_cardiac", "/c.txt", "default",
                       "Atrial fibrillation.", 1.0, "", "")

        query = embedder.encode_one("patient with T2DM and diabetic neuropathy")
        results = vs.query(query, n_results=2)
        assert results[0]["document_id"] == "doc_diabetes", (
            "Diabetes query should rank the diabetes document above the cardiac document"
        )

    def test_upsert_does_not_duplicate(self, tmp_path, embedder):
        vs = VectorStore(persist_dir=tmp_path / "chroma7")
        emb = embedder.encode_one("Stroke patient.")
        vs.add_section("s1", emb, "d1", "/p.txt", "default", "Stroke patient.", 1.0, "", "")
        vs.add_section("s1", emb, "d1", "/p.txt", "default", "Stroke patient.", 1.0, "", "")  # re-upsert
        assert vs.count() == 1, "Upserting the same section_id must not create duplicate entries"


# ══════════════════════════════════════════════════════════════════════════════
# DocumentIndexer — integration tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDocumentIndexer:

    @pytest.mark.slow
    def test_indexer_stores_valid_float_embedding(
        self, tmp_path, synonym_index, nlp_pipeline, embedder
    ):
        """
        DocumentIndexer.index_document() must store valid float embeddings in
        ChromaDB so that semantic search returns the indexed document.

        src/indexing.py line ~279 correctly passes `embedding = embedding`
        (the local List[float] variable).  This test guards against regression
        where the hex section_id string might accidentally be passed instead.
        """
        doc_path = tmp_path / "diabetes_test.txt"
        doc_path.write_text(
            "Patient has type 2 diabetes mellitus with peripheral neuropathy."
        )

        indexer = make_test_indexer(tmp_path, synonym_index, nlp_pipeline, embedder)
        count = indexer.index_document(doc_path)

        assert count >= 1, "At least one section should be indexed"

        query_emb = embedder.encode_one("diabetic neuropathy type 2 diabetes")
        results = indexer.vector_store.query(query_emb, n_results=1)

        assert len(results) == 1, "Dense search should return the indexed document"
        assert results[0]["document_id"] == "diabetes_test"
        assert results[0]["cosine_similarity"] > 0.3, (
            "Cosine similarity between related clinical texts must be > 0.3"
        )

    @pytest.mark.slow
    def test_indexer_populates_bm25_correctly(
        self, tmp_path, synonym_index, nlp_pipeline, embedder
    ):
        """
        After indexing ≥ 3 documents, BM25 search for terms that appear in only
        one document should return that document as the top result.

        IMPORTANT: BM25Okapi IDF = log((N - df + 0.5) / (df + 0.5)).
        With N=1 or N=2 and df=1, IDF ≤ 0 → all scores ≤ 0 → no hits returned.
        Three documents are required for the target term (df=1) to yield a
        positive IDF and thus a positive BM25 score.
        This is expected BM25Okapi behaviour, not a bug.
        """
        docs = {
            "sepsis_doc.txt": (
                "Sepsis secondary to urinary tract infection. "
                "WBC 18000 with left shift. Broad-spectrum antibiotics."
            ),
            "diabetes_doc.txt": (
                "Type 2 diabetes mellitus, poorly controlled. "
                "HbA1c 9.8%. Metformin and insulin adjusted."
            ),
            "stroke_doc.txt": (
                "Ischemic stroke, left MCA territory. "
                "Antiplatelet therapy and statin initiated."
            ),
        }
        for name, content in docs.items():
            (tmp_path / name).write_text(content)

        indexer = make_test_indexer(tmp_path, synonym_index, nlp_pipeline, embedder)
        for name in docs:
            indexer.index_document(tmp_path / name)

        results = indexer.bm25_index.search(["sepsis", "urinary"], n_results=5)
        assert len(results) > 0, (
            "With 3 documents (N=3, df=1 for 'sepsis'), BM25 IDF is positive "
            "and search should return the sepsis document."
        )
        assert results[0]["document_id"] == "sepsis_doc", (
            "The sepsis document should rank first for tokens 'sepsis' and 'urinary'"
        )

    @pytest.mark.slow
    def test_structured_doc_produces_multiple_sections_in_bm25(
        self, tmp_path, synonym_index, nlp_pipeline, embedder
    ):
        """
        A document with N sections should index N BM25 entries
        (near-empty sections < 20 chars are skipped).
        """
        doc_path = tmp_path / "multi_section.txt"
        doc_path.write_text(
            "PATIENT: Test | MRN: 000\n"
            "\n=== CHIEF COMPLAINT ===\n"
            "Shortness of breath for 3 days.\n"
            "\n=== ASSESSMENT AND PLAN ===\n"
            "1. COPD exacerbation. 2. Hypertension.\n"
            "\n=== DISCHARGE INSTRUCTIONS ===\n"
            "Follow up in 2 weeks.\n"
        )
        indexer = make_test_indexer(tmp_path, synonym_index, nlp_pipeline, embedder)
        indexer.index_document(doc_path)

        # Count of BM25 sections should reflect the doc structure
        assert len(indexer.bm25_index._sections) >= 2, (
            "A structured document with 3 sections should produce ≥ 2 BM25 entries "
            "(near-empty sections are skipped)"
        )

    @pytest.mark.slow
    def test_plain_text_doc_produces_one_section_in_bm25(
        self, tmp_path, synonym_index, nlp_pipeline, embedder
    ):
        """
        A plain-text document (no === headers) should produce exactly 1 BM25 section
        labelled 'default'.  This mirrors how doc_021 and doc_022 are handled.
        """
        doc_path = tmp_path / "plain_test.txt"
        doc_path.write_text(
            "Sarah K. is a 33-year-old with moderate persistent asthma and allergic "
            "rhinitis. She presents with increased wheezing and reduced peak flow. "
            "Stepped up to higher-dose fluticasone-salmeterol."
        )
        indexer = make_test_indexer(tmp_path, synonym_index, nlp_pipeline, embedder)
        indexer.index_document(doc_path)

        sections = indexer.bm25_index._sections
        assert len(sections) == 1
        assert sections[0]["section_label"] == "default", (
            "Plain-text documents without section headers should be stored under 'default'"
        )
