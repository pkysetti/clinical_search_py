"""
Session-scoped fixtures shared across all clinical search test suites.

Expensive fixtures (spaCy model, SentenceTransformer) are loaded ONCE per
pytest session.  The mini-corpus (5 hand-crafted sections) is indexed into a
temporary ChromaDB instance using the CORRECT embedding call — bypassing the
DocumentIndexer embedding bug — so that retrieval tests can work independently
of that bug.

Mini-corpus clinical scenarios
--------------------------------
doc_diabetes   – Type 2 DM + peripheral neuropathy + CKD
doc_heart      – Heart failure + atrial fibrillation + hypertension
doc_pneumonia  – CAP + COPD + hypoxia  (structured doc)
doc_sepsis_uti – Sepsis + UTI  (NO section headers; negated stroke/MI)
doc_stroke     – Ischemic stroke + hypertension
"""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.nlp import SynonymIndex, load_nlp
from src.indexing import Embedder, VectorStore, BM25Index


# ---------------------------------------------------------------------------
# Mini-corpus: manually curated, metadata matches what the indexer would store
# ---------------------------------------------------------------------------

MINI_CORPUS = [
    {
        "document_id":   "doc_diabetes",
        "document_path": "/test/doc_diabetes.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Type 2 diabetes mellitus, poorly controlled. HbA1c 9.8%. "
            "Diabetic peripheral neuropathy, bilateral lower extremities. "
            "Chronic kidney disease Stage 3a."
        ),
        "canonical_entities": (
            "type 2 diabetes mellitus, peripheral neuropathy, "
            "chronic kidney disease, hemoglobin a1c"
        ),
        "negated_entities": "",
    },
    {
        "document_id":   "doc_heart",
        "document_path": "/test/doc_heart.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Heart failure with reduced ejection fraction (HFrEF). "
            "Atrial fibrillation, persistent. "
            "Hypertension, well-controlled with metoprolol."
        ),
        "canonical_entities": "heart failure, atrial fibrillation, hypertension",
        "negated_entities": "",
    },
    {
        "document_id":   "doc_pneumonia",
        "document_path": "/test/doc_pneumonia.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Community acquired pneumonia, right lower lobe. "
            "COPD exacerbation treated with systemic steroids. "
            "SpO2 88% on room air on admission."
        ),
        "canonical_entities": (
            "pneumonia, chronic obstructive pulmonary disease, shortness of breath"
        ),
        "negated_entities": "",
    },
    {
        "document_id":   "doc_sepsis_uti",
        "document_path": "/test/doc_sepsis_uti.txt",
        "section_label": "default",       # plain-text doc – no section headers
        "section_bonus": 0.6,
        "section_text": (
            "Patient presents with sepsis secondary to urinary tract infection. "
            "WBC 18,000 with left shift. Blood cultures positive for E. coli. "
            "No prior history of stroke or myocardial infarction."
        ),
        "canonical_entities": "sepsis, urinary tract infection",
        "negated_entities": "stroke, myocardial infarction",
    },
    {
        "document_id":   "doc_stroke",
        "document_path": "/test/doc_stroke.txt",
        "section_label": "assessment and plan",
        "section_bonus": 1.0,
        "section_text": (
            "Ischemic stroke, left MCA territory, confirmed on MRI brain. "
            "Hypertension uncontrolled at 185/110 mmHg on admission. "
            "Antiplatelet therapy and statin initiated."
        ),
        "canonical_entities": "stroke, hypertension",
        "negated_entities": "",
    },
]


def _section_id(doc_id: str, i: int) -> str:
    return hashlib.md5(f"{doc_id}::section::{i}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def synonym_index():
    return SynonymIndex()


@pytest.fixture(scope="session")
def nlp_pipeline(synonym_index):
    return load_nlp(synonym_index)


@pytest.fixture(scope="session")
def embedder():
    return Embedder()


@pytest.fixture(scope="session")
def mini_sections():
    """Mini corpus sections with section_ids injected."""
    result = []
    for i, s in enumerate(MINI_CORPUS):
        sec = dict(s)
        sec["section_id"] = _section_id(s["document_id"], i)
        result.append(sec)
    return result


@pytest.fixture(scope="session")
def mini_vector_store(tmp_path_factory, embedder, mini_sections):
    """
    ChromaDB populated with correct float embeddings (not the buggy hex-string).
    Each section's text is encoded by the real SentenceTransformer.
    """
    chroma_dir = tmp_path_factory.mktemp("chroma_mini")
    vs = VectorStore(persist_dir=chroma_dir)
    for sec in mini_sections:
        embedding = embedder.encode_one(sec["section_text"])
        vs.add_section(
            section_id=sec["section_id"],
            embedding=embedding,
            document_id=sec["document_id"],
            document_path=sec["document_path"],
            section_label=sec["section_label"],
            section_text=sec["section_text"],
            section_bonus=sec["section_bonus"],
            canonical_entities=sec["canonical_entities"],
            negated_entities=sec["negated_entities"],
        )
    return vs


@pytest.fixture(scope="session")
def mini_bm25_index(mini_sections):
    """BM25 index built in-memory from the mini corpus."""
    idx = BM25Index()
    idx.build(mini_sections)
    return idx


@pytest.fixture(scope="session")
def query_processor(nlp_pipeline, synonym_index, embedder):
    from src.retrieval import QueryProcessor
    return QueryProcessor(nlp_pipeline, synonym_index, embedder)


@pytest.fixture(scope="session")
def hybrid_retriever(mini_vector_store, mini_bm25_index, synonym_index, embedder):
    from src.retrieval import HybridRetriever
    return HybridRetriever(
        mini_vector_store, mini_bm25_index, synonym_index, embedder
    )


# ---------------------------------------------------------------------------
# Helper: build a test DocumentIndexer that writes to a temp path
# ---------------------------------------------------------------------------

def make_test_indexer(tmp_path, synonym_index, nlp_pipeline, embedder):
    """
    Construct a DocumentIndexer with test-isolated storage.
    Uses object.__new__ to skip __init__ model loading (reuse session fixtures)
    and sets a temp ChromaDB directory so production data is never touched.
    """
    from src.indexing import DocumentIndexer

    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir(parents=True, exist_ok=True)

    indexer = object.__new__(DocumentIndexer)
    indexer.synonym_index = synonym_index
    indexer.nlp = nlp_pipeline
    indexer.embedder = embedder
    indexer.vector_store = VectorStore(persist_dir=chroma_dir)
    indexer.bm25_index = BM25Index()
    return indexer
