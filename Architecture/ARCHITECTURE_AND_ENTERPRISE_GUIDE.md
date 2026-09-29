# Clinical Relevance Search — Architecture and Enterprise Guide

A deep-dive into how the search system works, what every configuration setting controls, and a complete roadmap for taking this POC to enterprise-grade production — including model comparisons and cost projections for 1 million clinical reports.

---

## Table of Contents

1. [How Relevance Search Works](#how-relevance-search-works)
2. [Config Reference](#config-reference)
3. [Dependencies](#dependencies)
4. [Current State and Cost](#current-state-and-cost)
5. [Enterprise-Grade Changes](#enterprise-grade-changes)
6. [Model Comparison — Paid vs Open Source](#model-comparison--paid-vs-open-source)
7. [Cost to Run on 1 Million Reports](#cost-to-run-on-1-million-reports)

---

## How Relevance Search Works

The system uses **four signals combined into one composite score** to rank clinical documents against a query. Every document is split into sections before indexing, so the match is precise down to the exact part of a note that is relevant — not just "document matches query" but "the Assessment and Plan section of this note matches your query about peripheral neuropathy."

### Pipeline Overview

```
Documents (.txt)
    │
    ▼
Section Segmentation          ← splits on === HEADER === markers
    │
    ▼
Clinical NER  (spaCy + EntityRuler)
    │  resolves "MI"   → canonical: "myocardial infarction"
    │  resolves "STEMI"→ canonical: "myocardial infarction"
    │  detects negation ("no evidence of MI" → negated entity)
    ▼
Dual Indexing
    ├── Dense : SentenceTransformer embeddings → ChromaDB HNSW vector index
    └── Sparse: BM25Okapi on synonym-expanded tokens → pickle file on disk


Query string
    │
    ▼
Query Processor
    │  1. NER on query text
    │  2. Query tier detection  (clinical / partial / non_clinical)
    │  3. Threshold selection based on tier
    │  4. Synonym expansion for BM25 tokens
    │  5. Embed raw query string for vector search
    ▼
Hybrid Retrieval
    ├── Dense : ChromaDB ANN → top-20 nearest sections (cosine similarity)
    └── Sparse: BM25         → top-20 keyword-matched sections
    │
    ▼
RRF Fusion  (Reciprocal Rank Fusion, k=60)
    │  Merges the two ranked lists without requiring score calibration
    ▼
Document-level Aggregation
    │  Best-scoring section kept per document
    ▼
Composite Scoring  (per document)
    │  40%  Clinical Concept Overlap   (CCO)
    │  30%  Semantic Cosine Similarity (SCS)
    │  20%  BM25 Term Score            (BTS)
    │  10%  Section Relevance Bonus    (SRB)
    ▼
Threshold Filter
    │  Discard documents below tier threshold
    ▼
Top-10 Results with Human-Readable Explanation
```

---

### Step 1 — Section Segmentation

Each `.txt` document is split on `=== SECTION NAME ===` headers into sections such as `Chief Complaint`, `Assessment and Plan`, `Laboratory`, etc. Sections are indexed independently so that a query for "HbA1c" retrieves the specific laboratory section — not an entire 3-page discharge summary — and the explanation tells you exactly which section matched.

---

### Step 2 — Clinical NER and Synonym Resolution

A custom spaCy `EntityRuler` is loaded with 35 clinical concept groups from `data/synonyms.json`. It recognizes all surface variants of a concept and resolves them to a single canonical form:

- Query uses `"heart attack"` → canonical: `myocardial infarction`
- Document contains `"STEMI"` → same canonical: `myocardial infarction`
- They match, even though neither term appears in both places

Each detected entity carries four fields:

| Field | Purpose |
|---|---|
| `canonical` | Normalized form, e.g. `myocardial infarction` |
| `category` | e.g. `cardiovascular`, `respiratory`, `endocrine` |
| `specificity` | `high` (rare/diagnostic), `medium`, or `low` (generic like "pain") |
| `negated` | `True` if preceded within 6 tokens by a negation trigger |

Entity labels in spaCy take the form `CLINICAL_<CATEGORY>` (e.g. `CLINICAL_CARDIOVASCULAR`). Multi-word terms use token-level patterns with `LOWER` matching so casing is ignored.

---

### Step 3 — Dual Indexing

**Dense (Semantic)**

Each section is encoded by `all-MiniLM-L6-v2` into a 384-dimensional L2-normalized vector and stored in ChromaDB's HNSW index with cosine distance. Dense retrieval captures semantic meaning — "shortness of breath" matches "dyspnea" even without shared words. The model is downloaded once from HuggingFace (~90 MB) and runs fully locally; every vector is unit-length so dot product equals cosine similarity.

**Sparse (Keyword)**

Each section is tokenized using synonym-expanded terms and indexed in BM25Okapi. At query time, the query is expanded using the full synonym set for each detected entity — so "MI" becomes `["myocardial infarction", "heart attack", "MI", "AMI", "STEMI", ...]` for BM25 matching. This gives exact clinical abbreviation matching that dense retrieval misses. The BM25 model and section metadata are pickled to disk after each indexing run.

---

### Step 4 — Query Processing and Tier Selection

The query tier determines the minimum score a result must pass before being returned:

| Tier | Condition | Threshold | Rationale |
|---|---|---|---|
| `clinical` | Query has ≥1 high-specificity entity (e.g. "myocardial infarction") | **0.28** | Query is already precise; a lower bar is safe |
| `partial` | Query has only low-specificity entities (e.g. "chest pain") | **0.35** | Needs a stronger document match to avoid generic noise |
| `non_clinical` | No clinical entities detected at all | **0.55** | Very high bar to suppress off-topic queries ("quarterly budget") |

---

### Step 5 — Hybrid Retrieval and RRF Fusion

1. ChromaDB returns the top-20 semantically nearest sections (dense candidates)
2. BM25 returns the top-20 keyword-matched sections (sparse candidates)
3. **Reciprocal Rank Fusion (RRF)** with `k=60` merges the two lists:

   ```
   RRF_score(doc) = Σ  1 / (k + rank_in_list)
   ```

   RRF is parameter-free and requires no calibration between the two retrieval systems. A section appearing near the top of both the dense and sparse lists earns a very high combined score.

4. Multiple sections from the same document are collapsed — the best-scoring section per document is kept.

---

### Step 6 — Composite Scoring (The Heart of Relevance)

```
composite = 0.40 × CCO  +  0.30 × SCS  +  0.20 × BTS  +  0.10 × SRB
```

| Signal | Weight | What it Measures |
|---|---|---|
| **CCO** — Clinical Concept Overlap | 40% | Weighted Jaccard overlap of clinical entities. Specificity weights: `high=3.0`, `medium=1.5`, `low=0.5`. Matching "peripheral neuropathy" contributes 6× more than matching "pain". Negated entities are excluded from both query and document sets. |
| **SCS** — Semantic Cosine Similarity | 30% | Dot product of L2-normalized query and section embeddings stored in ChromaDB. Captures paraphrases and semantically related language. |
| **BTS** — BM25 Term Score | 20% | BM25Okapi score normalized to 0–1 by dividing by the maximum score in the result set. Strong for exact abbreviation and code matching. |
| **SRB** — Section Relevance Bonus | 10% | Fixed weight by section type encoding clinical domain knowledge. `Assessment and Plan = 1.0`, `Chief Complaint = 0.95`, `Laboratory = 0.80`, `Discharge Instructions = 0.40`. |

Documents below the tier threshold are discarded. The top-10 remaining are returned in descending score order.

---

### Step 7 — Explanation Generation

Each result includes:

- **Matched concepts** — clinical entities present in both the query and the document
- **Negation warnings** — query concepts that appear explicitly negated in the document (`"no prior MI"` warns you the document says the patient does NOT have this)
- **Synonym matches** — cases where the query used one surface form but the document used another (`"MI"` matched via `"myocardial infarction"`)
- **Key passage** — first 400 characters of the best-matching section
- **BORDERLINE flag** — if the composite score is within 0.05 of the threshold, alerting you to low-confidence results

---

## Config Reference

`src/config.py` controls every tunable parameter. Most tuning requires only editing this file, not changing any other code.

### Directory Layout

```python
BASE_DIR       = project root (two levels above config.py)
DATA_DIR       = BASE_DIR / "data"
DOCUMENTS_DIR  = DATA_DIR / "documents"    # drop .txt EHR files here
INDEX_DIR      = DATA_DIR / "index"
CHROMA_DIR     = INDEX_DIR / "chroma"      # auto-created on first import
BM25_DIR       = INDEX_DIR / "bm25"        # auto-created on first import
SYNONYMS_FILE  = DATA_DIR / "synonyms.json"
```

### Embedding Model

```python
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
```

Any HuggingFace sentence-transformer model ID can be substituted here. **Changing this requires re-running `index_documents.py`** — the new model produces different vector dimensions and all stored embeddings become stale. Clinical alternatives are in the [Model Comparison](#model-comparison--paid-vs-open-source) section.

### spaCy Model

```python
SPACY_MODEL = "en_core_web_sm"
```

The parser pipeline component is disabled at load time to reduce memory. The custom `EntityRuler` is injected before the built-in NER with `overwrite_ents=True` and `phrase_matcher_attr="LOWER"` for case-insensitive matching. Upgrading to `en_core_sci_lg` (scispaCy) requires `pip install scispacy` and downloading the model separately.

### Retrieval Pool Sizes

```python
TOP_K_DENSE  = 20   # candidates fetched from ChromaDB vector search
TOP_K_SPARSE = 20   # candidates fetched from BM25
TOP_K_FINAL  = 10   # maximum results returned after threshold filter
```

Increasing `TOP_K_DENSE` and `TOP_K_SPARSE` improves recall (more candidates enter the composite scoring step) at the cost of higher latency. Increasing `TOP_K_FINAL` returns more, lower-scored results.

### Composite Score Weights

```python
SCORE_WEIGHTS = {
    "cco": 0.40,   # Clinical Concept Overlap
    "scs": 0.30,   # Semantic Cosine Similarity
    "bts": 0.20,   # BM25 Term Score
    "srb": 0.10,   # Section Relevance Bonus
}
```

Weights must sum to 1.0. If you upgrade to a better medical NER model, raise `"cco"`. If you upgrade to a clinical embedding model, raise `"scs"`. Tune against a labeled evaluation set using NDCG@10 or MAP as the objective.

### Relevancy Thresholds

```python
THRESHOLDS = {
    "clinical":     0.28,
    "partial":      0.35,
    "non_clinical": 0.55,
}
```

Lower values increase recall (more results, some borderline relevant). Higher values increase precision (fewer results, all clearly relevant). The `non_clinical` threshold is intentionally high to suppress queries like "quarterly budget forecast" from returning anything at all.

### Section Relevance Bonuses

```python
SECTION_BONUSES = {
    "assessment and plan":        1.00,
    "chief complaint":            0.95,
    "history of present illness": 0.85,
    "laboratory":                 0.80,
    "physical examination":       0.75,
    "medications":                0.70,
    "past medical history":       0.65,
    "discharge instructions":     0.40,
    "default":                    0.60,
}
```

These encode clinical domain knowledge about where diagnoses live. `Assessment and Plan` scores highest because diagnostic conclusions and treatment decisions are documented there. `Discharge Instructions` scores lowest because it contains mostly boilerplate patient education text. Add new section names here as needed; unrecognized sections fall back to `"default": 0.60`.

### Negation Triggers

```python
NEGATION_TRIGGERS = {
    "no", "not", "denies", "denied", "without",
    "absence of", "no history of", "no evidence of",
    "rules out", "ruled out", "negative for",
    "no prior", "unremarkable for", "never"
}
```

A 6-token window before each entity span is scanned for these patterns. Negated entities are excluded from CCO scoring and flagged in the result explanation. Extend this set to cover additional clinical negation language specific to your document corpus.

---

## Dependencies

```
spacy                  >=3.7.0,<4.0.0    NER pipeline, EntityRuler, tokenization
sentence-transformers  >=2.7.0,<3.0.0    Dense embeddings via all-MiniLM-L6-v2
chromadb               >=0.5.0,<1.0.0    Local persistent vector store with HNSW index
rank-bm25              >=0.2.2            Sparse BM25Okapi keyword retrieval
tqdm                   >=4.66.0           Progress bars (used internally by sentence-transformers)
```

**Additional one-time setup (not in requirements.txt):**

```bash
python -m spacy download en_core_web_sm
```

**Optional — Windows only:**

```bash
pip install pysqlite3-binary   # required if system sqlite3 < 3.35 (ChromaDB dependency)
```

---

## Current State and Cost

### What Works Well

- Hybrid retrieval (semantic + keyword + RRF) handles exact abbreviations and semantic paraphrasing simultaneously
- Section-level indexing gives localized matches with precise passage extraction
- Weighted Jaccard CCO correctly weights rare diagnoses 6× higher than generic symptoms
- Adaptive thresholds prevent non-clinical queries from surfacing clinical noise
- Zero infrastructure dependencies — fully offline, persists to local files

### Current Limitations

| Limitation | Impact |
|---|---|
| `all-MiniLM-L6-v2` trained on general web text | Misses clinical semantic relationships; "cath lab" does not match "cardiac catheterization" |
| `en_core_web_sm` NER has limited medical vocabulary | EntityRuler covers only 35 concepts; unlisted clinical terms are invisible to CCO scoring |
| BM25 fully rebuilds on every new document | O(n) rebuild; impractical beyond ~100K documents |
| ChromaDB local HNSW is single-process and RAM-bound | Cannot serve concurrent queries; needs ~50 GB RAM for 8M sections |
| Synonym knowledge base is hand-curated | Does not cover SNOMED CT (350K+ concepts), UMLS (4M+), or RxNorm |
| No LLM re-ranking step | Results ranked purely by formula with no contextual reasoning |
| No authentication, logging, or audit trail | Not HIPAA-compliant for real patient data |

### Current Cost to Operate

| Resource | Cost |
|---|---|
| All Python packages | Free / open source |
| `all-MiniLM-L6-v2` model (HuggingFace) | Free |
| `en_core_web_sm` (spaCy) | Free |
| ChromaDB and BM25 storage | Free |
| **Total operating cost** | **$0 — hardware only** |

Indexing 6 sample documents on a modern laptop: ~10 seconds. Search latency: ~200–500 ms per query on CPU.

---

## Enterprise-Grade Changes

The changes below are required to move from POC to a production clinical search system. They are ordered by impact.

---

### 1. Replace the Embedding Model (Highest Impact)

`all-MiniLM-L6-v2` was trained on general web text (Wikipedia, Reddit). It does not understand clinical language nuances, hospital abbreviations, or co-occurrence patterns from medical records.

**Change:** Swap `EMBEDDING_MODEL` in `config.py` to a biomedical sentence transformer. One line change, followed by re-indexing. See the [Model Comparison](#model-comparison--paid-vs-open-source) table for candidates.

---

### 2. Replace spaCy NER with a Medical NLP System

The current `en_core_web_sm` + 35-concept EntityRuler covers a tiny fraction of medical vocabulary. SNOMED CT has 350,000+ clinical concepts; UMLS has 4 million+. Anything not in `synonyms.json` is invisible to the CCO scoring signal.

**Options:**

| Option | Coverage | Throughput | Cost |
|---|---|---|---|
| `scispaCy en_core_sci_lg` | PubMed NER, 600K vocabulary | ~2K docs/sec | Free |
| `medspaCy + QuickUMLS` | UMLS 4M+ concepts with CUI linking | ~500 docs/sec | Free (NLM license) |
| `stanza` bio models | i2b2, MIMIC NER, relation extraction | ~500 docs/sec | Free |
| AWS Comprehend Medical | ICD-10, RxNorm, SNOMED CT | Unlimited (API) | $0.01/100 chars |
| Azure Text Analytics for Health | FHIR entities, relations, negation | Unlimited (API) | $0.002/text record |

---

### 3. Replace ChromaDB with a Scalable Vector Database

ChromaDB's local `PersistentClient` uses a file-based HNSW index that is single-process and cannot serve concurrent queries. It will exhaust RAM at ~5M vectors and has no shard distribution.

**Options:**

| Option | Type | Notes |
|---|---|---|
| **Qdrant** | Open source (self-host or cloud) | Best OSS performance; filtering, payload indexing, native hybrid search |
| **Weaviate** | Open source (self-host or cloud) | Native BM25+vector hybrid; HIPAA BAA available |
| **Milvus** | Open source (Zilliz Cloud managed) | Best at 100M+ scale; higher operational complexity |
| **Pinecone** | Managed SaaS | Simplest to operate; serverless tier |
| **pgvector** | PostgreSQL extension | Good if already on PostgreSQL; simplest stack |

---

### 4. Replace BM25 Pickle with a Scalable Sparse Index

`BM25Okapi` rebuilds the full index on every new document and must fit entirely in RAM. This breaks at ~100K documents.

**Change:** Use Elasticsearch or AWS OpenSearch for BM25 at scale. Both support HIPAA-eligible deployments (OpenSearch + VPC + encryption at rest). Alternatively, Qdrant's built-in sparse vector support can replace BM25 entirely, eliminating the need for a separate sparse index system.

---

### 5. Add an LLM Re-Ranking Step

The composite score is a deterministic formula. It cannot understand query intent, context, or subtle clinical relationships the way a language model can.

**Change:** After initial retrieval of top-20 candidates, send a prompt to an LLM to re-rank results:

```
You are a clinical search assistant. Given the query and the following retrieved
document passages, rank them from most to least relevant. Return only ranked IDs.

Query: {query}
Passages: {list of top-20 section texts with IDs}
```

This step runs only at search time (not indexing), so the cost per query is bounded. Using a smaller model (Claude Haiku 4.5, GPT-4o mini) keeps latency under 1 second and cost under $0.005/query.

---

### 6. Replace Hand-Curated Synonyms with Standardized Ontologies

35 concepts in `synonyms.json` cannot cover real clinical data. Real discharge summaries reference thousands of conditions, medications, procedures, and labs.

**Change:**

- **UMLS Metathesaurus** — 4M+ concepts, 200+ source vocabularies; free with NLM license
- **SNOMED CT** — 350K+ clinical concepts with hierarchies; free for US healthcare entities
- **RxNorm** — Drug name normalization; free (NLM)
- **ICD-10-CM** — Diagnosis code mapping; free (CMS)

Use `QuickUMLS` or `medspaCy` to link detected entities to Concept Unique Identifiers (CUIs). This enables cross-ontology matching: "aspirin" matches "acetylsalicylic acid" matches "ASA" via their shared UMLS CUI, without any hand-coded synonym list.

---

### 7. HIPAA and Security Requirements

For any deployment handling real patient data:

- **PHI de-identification** before indexing, or encrypt all stored vectors and metadata at rest
- **Role-based access control** — not all users should retrieve all documents
- **Audit logging** — every query, result set, and user ID must be logged with timestamp (HIPAA requirement)
- **Encryption in transit** (TLS 1.2+) for all API traffic
- **BAA agreements** with all cloud vendors (AWS, Azure, GCP all offer HIPAA-eligible tiers)
- **Zero-trust network** — API behind VPC, no public endpoints

---

### 8. Production Infrastructure Summary

| Component | Current POC | Enterprise |
|---|---|---|
| Vector DB | ChromaDB local file | Qdrant Cloud / Weaviate / Pinecone |
| Sparse index | BM25 pickle file | Elasticsearch / OpenSearch |
| NER | EntityRuler (35 concepts) | medspaCy + UMLS / Azure Health NLP |
| Embeddings | all-MiniLM-L6-v2 (general) | GatorTron / voyage-medical-1 (clinical) |
| API layer | None (CLI only) | FastAPI + async handlers + JWT auth |
| Re-ranking | None | LLM re-ranker (Haiku / bge-reranker) |
| Auth | None | OAuth 2.0 / SAML / Active Directory |
| Monitoring | None | Prometheus + Grafana; query latency + NDCG@10 tracking |
| CI/CD | None | GitHub Actions, Docker, Kubernetes |
| Evaluation | None | Human-labeled query-document pairs + offline eval harness |

---

## Model Comparison — Paid vs Open Source

### Embedding Models

| Model | Type | Dimensions | Clinical Quality | Cost |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` | Open source | 384 | ★★☆☆☆ General web text | Free — current baseline |
| `NeuML/pubmedbert-base-embeddings` | Open source | 768 | ★★★★☆ PubMed-trained | Free |
| `pritamdeka/S-PubMedBERT-MS-MARCO` | Open source | 768 | ★★★★☆ Clinical retrieval fine-tuned | Free |
| `microsoft/BiomedNLP-BiomedBERT-base` | Open source | 768 | ★★★★☆ Biomedicine NLP | Free |
| `UFNLP/gatortron-base` | Open source | 1024 | ★★★★★ Trained on 277M de-identified clinical notes (UF Health + MIMIC) | Free |
| `text-embedding-3-small` | OpenAI (paid) | 1536 | ★★★☆☆ Strong general, limited clinical | $0.02 / 1M tokens |
| `text-embedding-3-large` | OpenAI (paid) | 3072 | ★★★★☆ Strong general | $0.13 / 1M tokens |
| `voyage-medical-1` | Voyage AI (paid) | 1024 | ★★★★★ Purpose-built for medical text retrieval | $0.06 / 1M tokens |
| `cohere embed-english-v3.0` | Cohere (paid) | 1024 | ★★★★☆ Strong general | $0.10 / 1M tokens |

**Best open source:** `UFNLP/gatortron-base` — the only large model trained directly on de-identified clinical notes at scale. Outperforms all general-purpose models on clinical retrieval benchmarks. Free to self-host.

**Best paid:** `voyage-medical-1` — purpose-built for medical/clinical text retrieval. Higher quality than OpenAI text-embedding-3-large at less than half the token price. Recommended when you cannot self-host a GPU.

---

### Medical NER / NLP Models

| Model | Type | Coverage | Throughput | Cost |
|---|---|---|---|---|
| `en_core_web_sm` + EntityRuler | Open source | 35 hand-curated concepts | ~10K docs/sec | Free — current baseline |
| `scispaCy en_core_sci_lg` | Open source | PubMed NER, 600K vocabulary | ~2K docs/sec | Free |
| `medspaCy + QuickUMLS` | Open source | UMLS 4M+ concepts with CUI linking | ~500 docs/sec | Free (NLM UMLS license) |
| `stanza` bio models | Open source | i2b2, MIMIC NER, relation extraction | ~500 docs/sec | Free |
| AWS Comprehend Medical | Managed paid | ICD-10, RxNorm, SNOMED CT | Unlimited (API) | $0.01 / 100 chars |
| Azure Text Analytics for Health | Managed paid | FHIR entities + relations + negation | Unlimited (API) | $0.002 / text record |

**Best open source:** `medspaCy + QuickUMLS` with the UMLS 2024 release. Covers the broadest clinical vocabulary and links entities to CUIs enabling cross-ontology matching. Requires a free NLM UMLS license.

**Best paid:** Azure Text Analytics for Health at $0.002/record. Returns structured FHIR-compatible output, handles negation detection natively, and extracts entity relationships (medication → dosage, diagnosis → severity). The cheapest managed option by a wide margin.

---

### LLM Re-Rankers

| Model | Type | Quality | Cost per Query (top-20 rerank) |
|---|---|---|---|
| `cross-encoder/ms-marco-MiniLM-L-12-v2` | Open source | ★★★☆☆ General | Free (~10ms on GPU) |
| `BAAI/bge-reranker-v2-m3` | Open source | ★★★★☆ Strong general, multilingual | Free (~20ms on GPU) |
| GPT-4o mini | OpenAI (paid) | ★★★★☆ Context-aware | ~$0.001–0.005 / query |
| GPT-4o | OpenAI (paid) | ★★★★★ Highest quality | ~$0.01–0.05 / query |
| Claude Haiku 4.5 | Anthropic (paid) | ★★★★☆ Fast, strong clinical reasoning | ~$0.001–0.003 / query |
| Claude Sonnet 4.6 | Anthropic (paid) | ★★★★★ Best balance of quality and cost | ~$0.005–0.02 / query |

**Best open source:** `BAAI/bge-reranker-v2-m3` — state-of-the-art open source cross-encoder. Multilingual, fast on GPU, strong clinical text performance without fine-tuning.

**Best paid:** Claude Haiku 4.5 — lowest latency and cost among frontier models with strong clinical reasoning ability. Use Claude Sonnet 4.6 or GPT-4o where maximum re-ranking accuracy matters more than cost.

---

## Cost to Run on 1 Million Reports

### Assumptions

| Parameter | Value |
|---|---|
| Number of clinical reports | 1,000,000 |
| Average tokens per report | 2,000 tokens |
| Average characters per report | 8,000 characters |
| Average sections per report | 8 sections × 250 tokens each |
| Total sections to index | 8,000,000 sections |
| Total tokens to embed | 2,000,000,000 (2 billion) |
| Search query volume | 10,000 queries/day (~300,000/month) |

---

### Option A — Current Stack (Local / Free Models, Unchanged)

**Architecture:** all-MiniLM-L6-v2 + en_core_web_sm + ChromaDB local + BM25 pickle

**One-time indexing cost:**

| Component | Detail | Cost |
|---|---|---|
| Embedding (all-MiniLM-L6-v2) | AWS g4dn.xlarge ($0.526/hr). T4 GPU: ~1,500 sections/sec. 8M sections ÷ 1,500/sec ≈ 1.5 hrs | ~$1 |
| BM25 rebuild | Full corpus must stay in RAM during rebuild. 8M sections ≈ 4 GB. Rebuild time 2–4 hrs on large machine | ~$2 |
| ChromaDB HNSW build | 8M × 384-dim vectors. HNSW overhead ≈ 3–4×; needs ~50 GB RAM. AWS r6i.2xlarge ($0.504/hr) for 4 hrs | ~$2 |
| Storage | ~50 GB ChromaDB + ~4 GB BM25 pickle on SSD | ~$5/month |
| **Total indexing (one-time)** | | **~$5** |

**Monthly operating cost (10K queries/day):**

| Component | Cost |
|---|---|
| Compute — ChromaDB + BM25 must stay in RAM for sub-second latency | AWS r6i.2xlarge (64 GB RAM) ≈ $360/month |
| Storage | ~$5/month |
| **Total monthly** | **~$365/month** |

**Hard architectural limits:** The current BM25 `BM25Okapi` rebuild will OOM at 1M documents — the pickle is not streaming. ChromaDB local HNSW cannot serve concurrent queries. **The current stack cannot reach 1M documents in production without changes.**

---

### Option B — Open Source Clinical Stack (Production-Ready)

**Architecture:** GatorTron embeddings + medspaCy + QuickUMLS + Qdrant Cloud + OpenSearch + bge-reranker

**One-time indexing cost:**

| Component | Detail | Cost |
|---|---|---|
| Embedding (GatorTron 1024-dim) | AWS g5.xlarge ($1.006/hr). A10G GPU: ~400 sections/sec. 8M ÷ 400 ≈ 5.5 hrs | ~$6 |
| NER via medspaCy + QuickUMLS | ~500 docs/sec on CPU. 1M docs ÷ 500/sec ≈ 33 min. AWS c6i.8xlarge ($1.36/hr) | ~$1 |
| Qdrant Cloud upload | 8M vectors × 1024-dim × 4 bytes = 32 GB raw. Upload + HNSW index build | ~$5 |
| OpenSearch indexing | AWS OpenSearch 3-node cluster (r6g.large.search, $0.168/node/hr) × 3 × 4 hrs | ~$2 |
| **Total indexing (one-time)** | | **~$14** |

**Monthly operating cost (10K queries/day):**

| Component | Detail | Cost |
|---|---|---|
| Qdrant Cloud | 8M vectors at 1024-dim ≈ 32 GB. 2-node cluster | ~$150/month |
| AWS OpenSearch | 2-node r6g.large.search cluster for BM25 | ~$200/month |
| bge-reranker GPU | AWS g4dn.xlarge spot. 10K queries/day × 200ms = <1 hr/day compute | ~$10/month |
| API/app server | AWS t3.medium | ~$30/month |
| **Total monthly** | | **~$390/month** |

---

### Option C — Paid API Stack (Highest Clinical Quality, Lowest Ops Overhead)

**Architecture:** voyage-medical-1 + Azure Text Analytics for Health + Pinecone + Elastic Cloud + Claude Haiku 4.5

**One-time indexing cost:**

| Component | Detail | Cost |
|---|---|---|
| Embedding (voyage-medical-1) | 2B tokens × $0.06/1M tokens | **$120** |
| Azure Text Analytics for Health | 1M documents × $0.002/text record | **$2,000** |
| Pinecone vector upsert | 8M vectors at 1024-dim. Serverless ingest | ~$1 |
| Elastic Cloud BM25 ingest | One-time index build of 8M sections | ~$50 |
| **Total indexing (one-time)** | | **~$2,171** |

**Monthly operating cost (10K queries/day):**

| Component | Detail | Cost |
|---|---|---|
| Pinecone serverless | 300K queries/month against 8M vector index | ~$30/month |
| Elastic Cloud | 2-node cluster for BM25 hybrid search | ~$150/month |
| Claude Haiku 4.5 re-ranking | 300K queries × 2,000 tokens (top-20 passages) = 600M tokens. Input: $0.80/1M | **$480/month** |
| API/app server | AWS t3.medium | ~$30/month |
| **Total monthly** | | **~$690/month** |

---

### Option D — OpenAI All-In (Simplest Paid Path, Lowest Indexing Cost)

**Architecture:** text-embedding-3-large + GPT-4o mini NER + Pinecone + GPT-4o mini re-ranking

**One-time indexing cost:**

| Component | Detail | Cost |
|---|---|---|
| Embedding (text-embedding-3-large) | 2B tokens × $0.13/1M tokens | **$260** |
| NER via GPT-4o mini (few-shot prompting) | 1M docs × 800 input tokens × $0.15/1M tokens | **$120** |
| Pinecone upsert | 8M vectors at 3072-dim. Standard s1.x1 pod | ~$5 |
| **Total indexing (one-time)** | | **~$385** |

**Monthly operating cost (10K queries/day):**

| Component | Detail | Cost |
|---|---|---|
| Pinecone | s1.x1 pod (3072-dim, 8M vectors) | ~$200/month |
| GPT-4o mini re-ranking | 300K queries × 2,000 tokens × $0.15/1M input tokens | **$90/month** |
| API/app server | AWS t3.medium | ~$30/month |
| **Total monthly** | | **~$320/month** |

---

### Full Comparison Table

| | **Option A** Current Stack | **Option B** OSS Clinical | **Option C** Paid APIs | **Option D** OpenAI All-In |
|---|---|---|---|---|
| Embedding model | all-MiniLM-L6-v2 (general) | GatorTron (clinical notes) | voyage-medical-1 (medical) | text-embedding-3-large |
| NER coverage | 35 concepts | UMLS 4M+ concepts | FHIR entities (Azure) | GPT-4o mini few-shot |
| Vector DB | ChromaDB local | Qdrant Cloud | Pinecone | Pinecone |
| Re-ranker | None | bge-reranker-v2-m3 | Claude Haiku 4.5 | GPT-4o mini |
| **Clinical quality** | ★★☆☆☆ | ★★★★☆ | ★★★★★ | ★★★★☆ |
| **Ops complexity** | High (RAM limits) | Medium | Low | Low |
| **Indexing cost (1M reports)** | ~$5 | ~$14 | ~$2,171 | ~$385 |
| **Monthly cost (10K q/day)** | ~$365/month | ~$390/month | ~$690/month | ~$320/month |
| Scales to 10M reports | No — architectural limits | Yes | Yes | Yes |
| HIPAA eligible | Self-managed only | Self-managed only | Yes (Azure + Pinecone BAA) | Yes (OpenAI BAA) |

---

### Recommendation

**Best clinical quality + long-term cost efficiency:** Use **Option B (OSS Clinical Stack)** — GatorTron embeddings + medspaCy + QuickUMLS + Qdrant + OpenSearch + bge-reranker. This gives the strongest clinical retrieval quality at a flat monthly cost that does not scale with query volume. The $14 one-time indexing cost is negligible.

For highest possible quality, add **Claude Haiku 4.5 re-ranking** on top of Option B (replacing bge-reranker). The additional cost is ~$480/month at 10K queries/day — meaningful for clinical decision support where result quality is high-stakes.

**Fastest path to production:** Use **Option D (OpenAI All-In)**. Lowest indexing cost of the paid options, straightforward SDK integrations, and solid (not best) clinical quality. Good for a pilot with real users before committing to the OSS stack.

**Do not use Option A at scale.** The BM25 pickle and local ChromaDB hit hard architectural limits around 100K documents. They are appropriate for this POC but cannot serve production workloads.