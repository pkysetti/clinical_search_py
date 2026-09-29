"""
Configuration for the Clinical Document Search POC.
All paths use pathlib.Path for cross-platform compatibility (macOS + Windows).
"""

from pathlib import Path

# ── Directory layout ──────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent.parent
DATA_DIR       = BASE_DIR / "data"
DOCUMENTS_DIR  = DATA_DIR / "documents"
INDEX_DIR      = DATA_DIR / "index"
CHROMA_DIR     = INDEX_DIR / "chroma"
BM25_DIR       = INDEX_DIR / "bm25"
SYNONYMS_FILE  = DATA_DIR / "synonyms.json"

# Create index directories on import so they always exist
CHROMA_DIR.mkdir(parents=True, exist_ok=True)
BM25_DIR.mkdir(parents=True, exist_ok=True)

# ── Embedding model ───────────────────────────────────────────────────────────
# Default: fast general model (~90 MB, no license required).
# For better clinical accuracy swap to: "pritamdeka/S-PubMedBert-MS-MARCO"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# ── spaCy model ───────────────────────────────────────────────────────────────
# Install with: python -m spacy download en_core_web_sm
SPACY_MODEL = "en_core_web_sm"

# ── Retrieval settings ────────────────────────────────────────────────────────
TOP_K_DENSE  = 20   # Candidates from vector search per query
TOP_K_SPARSE = 20   # Candidates from BM25 search per query
TOP_K_FINAL  = 10   # Results returned to user after scoring

# ── Composite score weights (must sum to 1.0) ─────────────────────────────────
# CCO = Clinical Concept Overlap  (40%)
# SCS = Semantic Cosine Similarity (30%)
# BTS = BM25 Term Score           (20%)
# SRB = Section Relevance Bonus   (10%)
SCORE_WEIGHTS = {
    "cco": 0.40,
    "scs": 0.30,
    "bts": 0.20,
    "srb": 0.10,
}

# ── Relevancy thresholds ─────────────────────────────────────────────────────
# Tuned for all-MiniLM-L6-v2 on clinical text.
# If you switch to a clinical embedding model, raise these by ~0.15.
THRESHOLDS = {
    "clinical":     0.28,   # Query has ≥1 recognised clinical entity
    "partial":      0.35,   # Query has only low-specificity entities (pain, fever, etc.)
    "non_clinical": 0.55,   # No clinical entities found in query
}

# ── Exact-match override ──────────────────────────────────────────────────────
# If a document's normalised BM25 score meets this threshold, it is returned
# even when the composite score falls below the tier threshold.  This ensures
# that queries whose terms are absent from the synonym dictionary (e.g. raw
# anatomical words, lifestyle terms) can still surface documents that contain
# the query word verbatim.  Set to 1.1 to disable.
EXACT_MATCH_BTS_MIN = 0.80

# ── Section labels & their scoring bonus ─────────────────────────────────────
SECTION_BONUSES = {
    "assessment and plan": 1.0,
    "chief complaint":     0.95,
    "history of present illness": 0.85,
    "laboratory":          0.80,
    "physical examination": 0.75,
    "medications":         0.70,
    "past medical history": 0.65,
    "discharge instructions": 0.40,   # boilerplate-heavy
    "default":             0.60,
}

# ── Negation trigger words ────────────────────────────────────────────────────
NEGATION_TRIGGERS = {
    "no", "not", "denies", "denied", "without", "absence of",
    "no history of", "no evidence of", "rules out", "ruled out",
    "negative for", "no prior", "unremarkable for", "never",
}
