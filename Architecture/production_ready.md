# Production-Ready Clinical Search Architecture

## Option B — Open Source Clinical Stack

This document explains the recommended production-ready open source architecture for Clinical Relevance Search.

The architecture combines clinical embeddings, medical named entity recognition, vector search, keyword search, and re-ranking to produce accurate, explainable clinical search results.

---

## 1. Architecture Summary

**Recommended production stack:**

```text
GatorTron embeddings
+ medspaCy
+ QuickUMLS
+ Qdrant Cloud
+ OpenSearch
+ bge-reranker
```

At a high level:

```text
Clinical Reports
     │
     ▼
Section Segmentation
     │
     ▼
NER: medspaCy + QuickUMLS
     │
     ├── Detects clinical concepts
     ├── Links concepts to UMLS CUIs
     ├── Handles synonyms
     └── Detects negation
     │
     ▼
Embedding: GatorTron
     │
     ├── Converts each section into a vector
     ├── Captures clinical meaning
     └── Stores vectors in Qdrant
     │
     ▼
Indexes
     ├── Qdrant vector index
     └── OpenSearch keyword/BM25 index
```

The main idea is simple:

```text
NER understands the clinical concepts.
Embeddings understand the meaning of the text.
OpenSearch handles exact keyword and abbreviation matching.
Qdrant retrieves semantically similar clinical sections.
A reranker improves the final ordering of results.
```

---

## 2. Why This Stack Is Production-Ready

The current proof-of-concept stack works for small datasets, but it has major scale and clinical coverage limits.

This production architecture improves the system in five major ways:

| Area | POC Stack | Production Stack |
|---|---|---|
| Embeddings | all-MiniLM-L6-v2 | GatorTron |
| Clinical vocabulary | 35 curated concepts | UMLS-linked concepts |
| NER | spaCy EntityRuler | medspaCy + QuickUMLS |
| Vector database | ChromaDB local | Qdrant Cloud |
| Keyword search | BM25 pickle file | OpenSearch |
| Re-ranking | None | bge-reranker |
| Scale | POC only | Production-capable |

The biggest improvements are:

```text
1. Clinical language understanding is much better.
2. Medical synonyms are handled through UMLS instead of a small hand-built list.
3. Vector search is moved to a scalable vector database.
4. Keyword search is moved to a production search engine.
5. Results can be re-ranked after retrieval for better relevance.
```

---

## 3. Core Components

### 3.1 GatorTron Embeddings

GatorTron is used to convert clinical text into numerical vectors.

Each section of a clinical report becomes a 1024-dimensional embedding vector.

Example section:

```text
Assessment and Plan:
Acute STEMI with dyspnea. Cardiology consulted.
```

GatorTron converts that text into a vector:

```text
[0.021, -0.184, 0.442, ..., 0.077]
```

That vector represents the clinical meaning of the section.

Sections with similar clinical meaning should land close to each other in vector space.

Example:

```text
"STEMI with dyspnea"
```

should be close to:

```text
"acute myocardial infarction with shortness of breath"
```

even though the words are different.

---

### 3.2 medspaCy

medspaCy is used for clinical natural language processing tasks, including:

```text
- clinical sentence splitting
- clinical tokenization
- section detection
- negation detection
- context detection
```

For example, medspaCy helps distinguish between:

```text
Patient has myocardial infarction.
```

and:

```text
No history of myocardial infarction.
```

The second example should not be treated as positive evidence for myocardial infarction.

---

### 3.3 QuickUMLS

QuickUMLS links clinical phrases to UMLS concepts.

Instead of maintaining a small custom synonym list, the system can use standardized medical terminology.

Example:

```text
heart attack
MI
STEMI
acute myocardial infarction
myocardial infarction
```

can all map to the same UMLS concept.

Simplified example:

```text
heart attack ───────────────┐
MI ─────────────────────────┤
STEMI ──────────────────────┤
myocardial infarction ──────┘
              │
              ▼
        UMLS CUI: C0027051
```

This allows the system to match query and document concepts even when the wording is different.

---

### 3.4 Qdrant Cloud

Qdrant stores the embedding vectors created by GatorTron.

It is responsible for vector similarity search.

When a user searches for:

```text
heart attack with shortness of breath
```

the query is also converted into a vector using GatorTron.

Qdrant then finds stored clinical sections whose vectors are closest to the query vector.

This is the semantic retrieval path.

It helps find passages that are clinically similar even when the exact words are different.

---

### 3.5 OpenSearch

OpenSearch stores section text for keyword and BM25 search.

This matters because exact clinical terms, abbreviations, codes, and lab values are important.

Examples:

```text
MI
STEMI
NSTEMI
HbA1c
ICD-10 codes
CPT codes
troponin
metformin
```

Embedding search may understand general meaning, but OpenSearch is better for exact term matching.

For example:

```text
Query:
NSTEMI elevated troponin

OpenSearch can strongly match:
NSTEMI with elevated troponin I
```

---

### 3.6 bge-reranker

The reranker is used after initial retrieval.

The system first retrieves candidate results from Qdrant and OpenSearch.

Then the reranker reviews the top candidates and improves their ordering.

The reranker answers:

```text
Given this query and these candidate passages,
which passages are truly the most relevant?
```

This improves final ranking quality, especially when multiple candidate sections look similar.

---

## 4. Index-Time Flow

Indexing happens before users search.

Each clinical report is processed and stored in multiple ways.

```text
Clinical Report
     │
     ▼
Split into sections
     │
     ▼
For each section:
     │
     ├── Run NER with medspaCy + QuickUMLS
     │       ├── Extract clinical concepts
     │       ├── Link to UMLS CUIs
     │       └── Detect negation
     │
     ├── Create embedding with GatorTron
     │       └── Store vector in Qdrant
     │
     └── Index text in OpenSearch
             └── Enable BM25 keyword search
```

Example report:

```text
Report 123
├── Chief Complaint
├── History of Present Illness
├── Laboratory
├── Assessment and Plan
└── Discharge Instructions
```

Each section is indexed separately.

This allows the system to return a precise section-level match instead of only saying that an entire document matched.

---

## 5. What Gets Stored

For each section, the system stores both semantic and clinical metadata.

Example section:

```text
Assessment and Plan:
Acute STEMI with dyspnea. Cardiology consulted.
```

NER output:

```json
{
  "section": "Assessment and Plan",
  "entities": [
    {
      "text": "STEMI",
      "canonical": "myocardial infarction",
      "cui": "C0027051",
      "type": "Disease or Syndrome",
      "negated": false
    },
    {
      "text": "dyspnea",
      "canonical": "dyspnea",
      "cui": "C0013404",
      "type": "Sign or Symptom",
      "negated": false
    }
  ]
}
```

Qdrant payload example:

```json
{
  "id": "report_123_assessment_plan",
  "vector": [0.021, -0.184, 0.442, "..."],
  "payload": {
    "report_id": "123",
    "section": "Assessment and Plan",
    "text": "Acute STEMI with dyspnea. Cardiology consulted.",
    "entities": ["C0027051", "C0013404"]
  }
}
```

OpenSearch document example:

```json
{
  "id": "report_123_assessment_plan",
  "report_id": "123",
  "section": "Assessment and Plan",
  "text": "Acute STEMI with dyspnea. Cardiology consulted.",
  "concepts": ["myocardial infarction", "dyspnea"],
  "cuis": ["C0027051", "C0013404"]
}
```

---

## 6. Search-Time Flow

When a user submits a query, the system processes it through multiple paths.

Example query:

```text
heart attack with shortness of breath
```

Search flow:

```text
User Query
   │
   ├── NER with medspaCy + QuickUMLS
   │       └── Extract CUIs and detect negation
   │
   ├── GatorTron embedding
   │       └── Create query vector
   │
   └── OpenSearch query expansion
           └── Search exact terms, synonyms, abbreviations, and codes

        │
        ▼

Hybrid Retrieval
   │
   ├── Qdrant returns semantic matches
   └── OpenSearch returns keyword matches

        │
        ▼

RRF Fusion
   │
   └── Merge candidate rankings

        │
        ▼

Composite Scoring
   │
   ├── Clinical Concept Overlap from NER CUIs
   ├── Semantic Similarity from GatorTron vectors
   ├── BM25 score from OpenSearch
   └── Section relevance bonus

        │
        ▼

bge-reranker
   │
   └── Improve final ranking of top candidates

        │
        ▼

Top Results with Explanation
```

---

## 7. Example End-to-End

### User query

```text
heart attack with shortness of breath
```

### Query NER

```text
heart attack → myocardial infarction → C0027051
shortness of breath → dyspnea → C0013404
```

### Query embedding

The query is converted into a GatorTron vector.

```text
Query text
   │
   ▼
GatorTron
   │
   ▼
Query vector
```

### Candidate section

```text
Assessment and Plan:
Acute STEMI with dyspnea. Cardiology consulted.
```

### Document NER

```text
STEMI → myocardial infarction → C0027051
dyspnea → dyspnea → C0013404
```

### Result

The query and document section match strongly because:

```text
1. The clinical concepts overlap.
2. The semantic meaning is similar.
3. The exact clinical terms and synonyms match.
4. The match appears in Assessment and Plan, a high-value section.
```

Example score:

| Signal | Source | Example Value |
|---|---|---:|
| Clinical Concept Overlap | NER CUIs | 1.00 |
| Semantic Similarity | GatorTron + Qdrant | 0.86 |
| BM25 Score | OpenSearch | 0.92 |
| Section Bonus | Assessment and Plan | 1.00 |

Composite score:

```text
0.40 × 1.00
+ 0.30 × 0.86
+ 0.20 × 0.92
+ 0.10 × 1.00
= 0.942
```

This would be returned as a strong result.

---

## 8. Why Embeddings and NER Are Both Needed

### Embeddings answer this question

```text
Does this passage mean something similar to the query?
```

Embeddings are useful for matching paraphrases.

Example:

```text
Query:
heart attack with shortness of breath

Document:
STEMI with dyspnea
```

Even with different words, embeddings can recognize that the meaning is similar.

---

### NER answers this question

```text
Are the same clinical concepts explicitly present?
```

NER is useful for matching normalized medical concepts.

Example:

```text
heart attack
MI
STEMI
myocardial infarction
```

These can all resolve to the same concept:

```text
C0027051
```

NER also handles negation.

Example:

```text
No history of MI.
```

This should not count as positive evidence for myocardial infarction.

---

### OpenSearch answers this question

```text
Do the exact terms, abbreviations, codes, or lab values match?
```

This is useful for terms like:

```text
HbA1c
NSTEMI
ICD-10 codes
troponin
medication names
```

---

### Reranking answers this question

```text
Among the retrieved candidates, which ones are most relevant in context?
```

This helps improve final result ordering after hybrid retrieval.

---

## 9. Negation Handling

Negation handling is critical in clinical search.

These two sentences are clinically very different:

```text
Patient has a history of myocardial infarction.
```

```text
Patient has no history of myocardial infarction.
```

The production stack uses medspaCy context and negation detection to avoid false positives.

Example:

```text
No evidence of pulmonary embolism.
```

Detected concept:

```json
{
  "text": "pulmonary embolism",
  "canonical": "pulmonary embolism",
  "negated": true
}
```

A negated concept can still be shown in the explanation, but it should not increase the positive clinical concept overlap score.

---

## 10. Production Cost Estimate for 1 Million Reports

Assumptions:

| Parameter | Value |
|---|---:|
| Reports | 1,000,000 |
| Average sections per report | 8 |
| Total sections | 8,000,000 |
| Embedding dimension | 1024 |
| Query volume | 10,000 queries/day |

One-time indexing estimate:

| Component | Detail | Cost |
|---|---|---:|
| Embedding | GatorTron on AWS g5.xlarge, about 400 sections/sec | ~$6 |
| NER | medspaCy + QuickUMLS on AWS c6i.8xlarge | ~$1 |
| Qdrant upload | 8M vectors, 1024 dimensions | ~$5 |
| OpenSearch indexing | 3-node AWS OpenSearch indexing job | ~$2 |
| Total | One-time indexing estimate | ~$14 |

Monthly operating estimate:

| Component | Detail | Cost |
|---|---|---:|
| Qdrant Cloud | Vector search for 8M vectors | ~$150/month |
| AWS OpenSearch | BM25 keyword search | ~$200/month |
| bge-reranker GPU | Approximate usage for 10K queries/day | ~$10/month |
| API server | Application/API layer | ~$30/month |
| Total | Estimated monthly operating cost | ~$390/month |

These are rough planning estimates. Actual cost depends on traffic, cloud region, cluster sizing, redundancy, storage configuration, and latency requirements.

---

## 11. Recommended Result Explanation Format

Each result should include a human-readable explanation.

Example:

```text
Result: Report 123 — Assessment and Plan

Why this matched:
- Query concept "heart attack" matched document concept "STEMI"
- Both map to myocardial infarction, UMLS CUI C0027051
- Query concept "shortness of breath" matched document concept "dyspnea"
- Both map to UMLS CUI C0013404
- Match occurred in Assessment and Plan, a high-value clinical section
- No negation was detected for the matched concepts

Score:
- Clinical Concept Overlap: 1.00
- Semantic Similarity: 0.86
- BM25 Score: 0.92
- Section Bonus: 1.00
- Final Score: 0.942
```

This makes the system explainable and easier to validate with clinical users.

---

## 12. Implementation Notes

### Indexing services

A production implementation should separate indexing into dedicated jobs:

```text
1. Document ingestion job
2. Section segmentation job
3. NER extraction job
4. Embedding generation job
5. Qdrant upsert job
6. OpenSearch indexing job
```

### Search services

The query API should perform:

```text
1. Query NER
2. Query embedding
3. Qdrant vector search
4. OpenSearch BM25 search
5. RRF fusion
6. Composite scoring
7. Reranking
8. Explanation generation
```

### Metadata to persist

Each section should persist:

```text
- report ID
- section ID
- section name
- section text
- embedding vector
- UMLS CUIs
- canonical concept names
- negation status
- source document metadata
- created timestamp
- indexing version
```

### Versioning

The system should track versions for:

```text
- embedding model
- NER model
- UMLS release
- section segmentation logic
- scoring weights
- reranker model
```

This is important because changing any of these may require re-indexing or score recalibration.

---

## 13. Recommended Production Path

Recommended implementation order:

```text
1. Add medspaCy + QuickUMLS for clinical concept extraction.
2. Replace MiniLM embeddings with GatorTron embeddings.
3. Move vector storage from ChromaDB to Qdrant.
4. Move BM25 from local pickle to OpenSearch.
5. Add RRF fusion between Qdrant and OpenSearch results.
6. Add composite scoring using NER, embedding similarity, BM25, and section bonus.
7. Add bge-reranker for top candidate reranking.
8. Add explanations, logging, metrics, and evaluation.
```

---

## 14. Summary

Option B is the recommended production-ready open source architecture because it combines:

```text
GatorTron
```

for clinical semantic understanding,

```text
medspaCy + QuickUMLS
```

for clinical concept extraction and UMLS normalization,

```text
Qdrant
```

for scalable vector search,

```text
OpenSearch
```

for production-grade keyword and BM25 retrieval,

and:

```text
bge-reranker
```

for better final ranking.

The key design principle is:

```text
Retrieve broadly using semantic and keyword search.
Score carefully using clinical concepts and section context.
Rerank the best candidates for final relevance.
Explain every result in clinical terms.
```

This provides a strong path from proof of concept to enterprise-grade clinical search.
