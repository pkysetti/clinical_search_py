# Clinical Document Relevancy & Search — POC

A proof-of-concept system for high-precision retrieval of clinical EHR documents with full explainability. No databases required — everything persists to local files on disk.

---

## What it does

- Accepts a clinical query (e.g. *"type 2 diabetes with peripheral neuropathy"*)
- Understands synonyms: searching `"MI"` finds documents containing `"myocardial infarction"`
- Returns only documents above a relevancy threshold — unrelated queries return zero results
- Explains **why** each document was retrieved: matched concepts, key passage, score breakdown, synonym matches, and negation warnings

---

## Tech stack (all file-based, no servers)

| Component | Technology |
|---|---|
| Clinical NLP / NER | spaCy `en_core_web_sm` + custom EntityRuler |
| Synonym expansion | Built-in `data/synonyms.json` dictionary |
| Dense embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`) |
| Vector index | ChromaDB (persisted to `data/index/chroma/`) |
| Sparse search | `rank-bm25` (pickled to `data/index/bm25/`) |
| Fusion | Reciprocal Rank Fusion (RRF) |

---

## Project layout

```
clinical_search_poc/
├── README.md
├── requirements.txt
├── pytest.ini              ← test configuration
├── setup_check.py          ← verify environment
├── index_documents.py      ← step 1: build the index
├── search.py               ← step 2: run searches
├── src/
│   ├── config.py           ← all settings & thresholds
│   ├── nlp.py              ← NER, negation, synonyms, section splitting
│   ├── indexing.py         ← ChromaDB + BM25 indexing
│   ├── retrieval.py        ← hybrid retrieval + scoring
│   └── explainer.py        ← explanation generation
├── tests/
│   ├── conftest.py         ← shared session-scoped fixtures (ML models, mini corpus)
│   ├── test_nlp.py         ← NER, negation, synonym expansion, section segmentation
│   ├── test_sparse.py      ← BM25 index build, search, and synonym expansion
│   ├── test_dense.py       ← SentenceTransformer embeddings, ChromaDB vector store
│   ├── test_query_processor.py ← query tier, entity extraction, BM25 tokens
│   ├── test_hybrid_retrieval.py ← RRF fusion, composite scoring, threshold filtering
│   └── test_e2e.py         ← full pipeline: index → query → ranked results
└── data/
    ├── synonyms.json        ← 35 clinical concept synonym groups
    └── documents/           ← your .txt EHR documents go here
        ├── doc_001_diabetes_neuropathy.txt
        ├── doc_002_heart_failure_afib.txt
        ├── doc_003_copd_pneumonia.txt
        ├── doc_004_sepsis_uti.txt
        ├── doc_005_stroke_hypertension.txt
        └── doc_006_postop_dvt.txt
```

---

## Setup — macOS

### Prerequisites
- Python 3.10 or 3.11 (recommended). Check with: `python3 --version`
- If not installed: https://www.python.org/downloads/

### Steps

```bash
# 1. Open Terminal and navigate to the project folder
cd /path/to/clinical_search_poc

# 2. Create a virtual environment
python3 -m venv .venv

# 3. Activate it
source .venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Download the spaCy language model
python -m spacy download en_core_web_sm

# 6. Verify everything is ready
python setup_check.py

# 7. Build the search index (first time only, downloads ~90 MB embedding model)
python index_documents.py

# 8. Run a search
python search.py "type 2 diabetes with peripheral neuropathy"

# Or launch interactive mode
python search.py
```

---

## Setup — Windows

### Prerequisites
- Python 3.10 or 3.11 — download from https://www.python.org/downloads/
- During installation: **check "Add Python to PATH"**
- Recommended: use **Windows Terminal** or **PowerShell** (not CMD)

### Steps

```powershell
# 1. Open PowerShell and navigate to the project folder
cd C:\path\to\clinical_search_poc

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
.venv\Scripts\Activate.ps1

#    If you see a permissions error, first run:
#    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# 4. Install dependencies
pip install -r requirements.txt

# 5. Download the spaCy language model
python -m spacy download en_core_web_sm

# 6. Verify everything is ready
python setup_check.py

# 7. Build the search index
python index_documents.py

# 8. Run a search
python search.py "type 2 diabetes with peripheral neuropathy"

# Or launch interactive mode
python search.py
```

### Windows-only: sqlite3 issue
ChromaDB requires sqlite3 ≥ 3.35. Some Windows Python builds ship an older version.  
**If you see:** `RuntimeError: Your system has an unsupported version of sqlite3`

Fix:
```powershell
pip install pysqlite3-binary
```
Then open `index_documents.py` and `search.py` and uncomment these three lines near the top:
```python
import sys, pysqlite3
sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
```

---

## Testing

The test suite covers every layer of the pipeline with 185 tests split across unit, integration, and end-to-end levels.

### Run all tests

```bash
# Activate the virtual environment first
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\Activate.ps1       # Windows

pytest
```

### Common test commands

```bash
# Run with verbose output (show each test name)
pytest -v

# Run only fast unit tests — no ML model loading (< 1 second)
pytest -m unit

# Run tests that require ML models (spaCy, SentenceTransformer)
pytest -m slow

# Run end-to-end pipeline tests
pytest -m e2e

# Run a single test file
pytest tests/test_nlp.py
pytest tests/test_sparse.py
pytest tests/test_dense.py
pytest tests/test_query_processor.py
pytest tests/test_hybrid_retrieval.py
pytest tests/test_e2e.py

# Run a specific test class
pytest tests/test_nlp.py::TestNegationDetection

# Run a specific test by name (substring match)
pytest -k "test_abbreviation_MI"

# Stop at the first failure
pytest -x

# Show full assertion diffs on failure
pytest -v --tb=long

# Re-run only the tests that failed last time
pytest --lf

# Run tests in parallel (requires pytest-xdist)
pip install pytest-xdist
pytest -n auto
```

### Test markers

| Marker | Description |
|--------|-------------|
| `unit` | Pure Python, no ML models — runs in milliseconds |
| `slow` | Requires spaCy pipeline and/or SentenceTransformer |
| `e2e`  | Full pipeline test: indexing → query → ranked results |

### What each test file covers

| File | Component | Key invariants |
|------|-----------|----------------|
| `test_nlp.py` | NER, negation, synonym expansion, section segmentation | Abbreviations resolve correctly; negation doesn't bleed across clause boundaries |
| `test_sparse.py` | BM25 index build and search | Synonym-expanded query outperforms raw abbreviation; normalised score ≤ 1 |
| `test_dense.py` | Embedder and ChromaDB vector store | 384-dim unit-normalised vectors; identical text ranked first |
| `test_query_processor.py` | Query processing pipeline | Tier classification; negated terms excluded from BM25 tokens |
| `test_hybrid_retrieval.py` | RRF fusion and composite scoring | `clinical < partial < non_clinical` threshold ordering; section bonus affects ranking |
| `test_e2e.py` | Full pipeline | Deterministic top-document per clinical query; plain-text docs retrievable |

---

## Development workflow

### Typical change cycle

```bash
# 1. Make changes to src/

# 2. Run unit tests immediately (no model loading, instant feedback)
pytest -m unit -v

# 3. Run the full suite before committing
pytest

# 4. If you change synonyms.json, rebuild the index
python index_documents.py

# 5. Sanity-check a live search
python search.py "type 2 diabetes with peripheral neuropathy"
```

### Modifying the NLP layer (`src/nlp.py`)

After any change to negation logic, synonym expansion, or entity extraction:
```bash
pytest tests/test_nlp.py tests/test_query_processor.py -v
```

### Modifying the indexing layer (`src/indexing.py`)

After any change to BM25 tokenization or ChromaDB storage:
```bash
pytest tests/test_sparse.py tests/test_dense.py -v
# Rebuild the production index
python index_documents.py
```

### Modifying retrieval or scoring (`src/retrieval.py`)

```bash
pytest tests/test_hybrid_retrieval.py tests/test_e2e.py -v
```

### Checking test coverage

```bash
pip install pytest-cov
pytest --cov=src --cov-report=term-missing
```

### Linting

```bash
pip install ruff
ruff check src/ tests/
```

---

## Usage examples

```bash
# Clinical query — will retrieve relevant documents
python search.py "MI"
python search.py "heart failure and shortness of breath"
python search.py "sepsis UTI"
python search.py "DVT post surgery"
python search.py "COPD exacerbation pneumonia"
python search.py "HbA1c diabetes insulin"

# Abbreviation expansion test (MI → myocardial infarction)
python search.py "MI"

# Non-clinical query — should return zero results (correct behaviour)
python search.py "quarterly budget forecast"
python search.py "project timeline"
```

---

## Adding your own documents

1. Save your EHR document as a plain `.txt` file (UTF-8 encoding)
2. Drop it into `data/documents/`
3. Re-run: `python index_documents.py`
4. Search immediately

Documents are expected to use section headers wrapped in `===`, for example:
```
=== CHIEF COMPLAINT ===
=== HISTORY OF PRESENT ILLNESS ===
=== ASSESSMENT AND PLAN ===
```
Plain text without headers also works — it is treated as a single section.

---

## Understanding the output

```
════════════════════════════════════════════════════════════
  RESULT #1  —  doc_001_diabetes_neuropathy.txt
════════════════════════════════════════════════════════════
  Overall Relevancy Score : 0.7231  (threshold: 0.28  |  margin: +0.4431)

  Score Breakdown:
    Clinical Concept Overlap (40%) : 0.8571   ← strong concept match
    Semantic Similarity      (30%) : 0.6843   ← embedding similarity
    BM25 Term Score          (20%) : 0.9120   ← keyword match
    Section Relevance Bonus  (10%) : 1.0000   ← hit in Assessment/Plan

  Matched Clinical Concepts (2):
    • peripheral neuropathy
    • type 2 diabetes mellitus

  Synonym Matches (query term → document term):
    • "peripheral neuropathy"  →  "diabetic polyneuropathy"

  Best Matching Section : [ASSESSMENT AND PLAN]
  Retrieval Path        : dense + sparse

  Key Passage:
  ────────────────────────────────────────────────────────────
  Diabetic peripheral neuropathy - severe, bilateral lower
  extremities. Initiated gabapentin for neuropathic pain...
  ────────────────────────────────────────────────────────────
```

**Score components explained:**
- **CCO (40%)** — how many clinical concepts from the query appear in the document, weighted by specificity
- **SCS (30%)** — semantic vector similarity (catches paraphrases and related concepts)
- **BTS (20%)** — BM25 keyword match score (exact term hits)
- **SRB (10%)** — bonus for matches in high-value sections (Assessment/Plan scores highest)

**Negation warnings** appear when a matched concept is explicitly negated in the document (e.g. *"no prior MI"*), so you are never misled by absence-of-disease mentions.

---

## Tuning thresholds

Edit `src/config.py`:

```python
THRESHOLDS = {
    "clinical":     0.28,   # query has ≥1 recognised clinical entity
    "partial":      0.35,   # only low-specificity entities (pain, fever)
    "non_clinical": 0.55,   # no clinical entities found
}
```

- **Raise** a threshold to get fewer, more precise results
- **Lower** it to get broader recall
- The `non_clinical` threshold is intentionally high to suppress irrelevant queries

---

## Upgrading to a clinical embedding model (optional)

The default model (`all-MiniLM-L6-v2`) is fast and general-purpose.  
For better medical text understanding, switch to a clinical model:

In `src/config.py`:
```python
EMBEDDING_MODEL = "pritamdeka/S-PubMedBert-MS-MARCO"
```

Then re-run `python index_documents.py` to rebuild the vector index with the new model.

---

## Expanding the synonym dictionary

Edit `data/synonyms.json`. Each entry follows this schema:
```json
{
  "canonical": "atrial fibrillation",
  "synonyms": ["AF", "AFib", "A-fib", "irregular heartbeat"],
  "icd10": "I48",
  "category": "cardiovascular",
  "specificity": "high"
}
```
`specificity` can be `"high"` (rare/specific), `"medium"`, or `"low"` (very common terms like "pain").  
After editing, re-run `python index_documents.py` to rebuild the index.
