# Clinical Relevance Search — Model Explanation

This document explains the core relevance model used by the Clinical Relevance Search system. It covers how documents are processed, how queries are interpreted, how dense and sparse retrieval work together, and how the final relevance score is calculated.

> Note: The filename intentionally uses `model_explaination.md` to match the requested project filename.

---

## 1. Core Idea

Clinical Relevance Search is a hybrid ranking system. It does not rely on only keyword search or only semantic embeddings. Instead, it combines several independent signals to decide whether a clinical document is relevant to a user query.

The model combines four scoring signals:

| Signal | Weight | Purpose |
|---|---:|---|
| Clinical Concept Overlap (CCO) | 40% | Measures whether the query and document contain the same normalized clinical concepts |
| Semantic Cosine Similarity (SCS) | 30% | Measures whether the query and document section mean similar things |
| BM25 Term Score (BTS) | 20% | Measures exact keyword, abbreviation, and synonym-expanded term matches |
| Section Relevance Bonus (SRB) | 10% | Gives more credit when the match occurs in a clinically important section |

The final composite score is:

```text
composite = 0.40 * CCO + 0.30 * SCS + 0.20 * BTS + 0.10 * SRB
```

The key principle is:

```text
Retrieve broadly using dense + sparse search,
then rank carefully using clinical-aware scoring.
```

Dense retrieval helps find semantic matches. Sparse retrieval helps find exact clinical terms, abbreviations, and codes. Clinical scoring determines which retrieved documents are truly relevant.

---

## 2. High-Level Pipeline

```text
Documents (.txt)
    |
    v
Section Segmentation
    |
    v
Clinical NER + Synonym Resolution
    |
    v
Dual Indexing
    |-----------------------------|
    |                             |
    v                             v
Dense Vector Index           Sparse BM25 Index
Sentence embeddings          Synonym-expanded tokens
ChromaDB / HNSW              BM25Okapi
    |                             |
    |-------------|---------------|
                  v
              Query Time
                  |
                  v
Query Processing
    - Detect clinical entities
    - Resolve synonyms
    - Determine query tier
    - Select threshold
    - Expand query terms
    - Embed raw query
                  |
                  v
Hybrid Retrieval
    - Dense top-k candidates
    - Sparse top-k candidates
                  |
                  v
RRF Fusion
                  |
                  v
Document-Level Aggregation
                  |
                  v
Composite Scoring
                  |
                  v
Threshold Filtering
                  |
                  v
Top Results + Explanation
```

---

## 3. Section Segmentation

Each `.txt` clinical document is split into smaller sections before indexing. The system expects section headers in this style:

```text
=== CHIEF COMPLAINT ===
Patient reports shortness of breath and chest pressure.

=== HISTORY OF PRESENT ILLNESS ===
Symptoms began two hours ago.

=== ASSESSMENT AND PLAN ===
Acute STEMI suspected. Cardiology consulted.

=== DISCHARGE INSTRUCTIONS ===
Follow up with primary care physician.
```

After segmentation, the document becomes:

```text
Document A
|
|-- Chief Complaint
|   `-- shortness of breath, chest pressure
|
|-- History of Present Illness
|   `-- symptoms began two hours ago
|
|-- Assessment and Plan
|   `-- acute STEMI suspected
|
`-- Discharge Instructions
    `-- follow up with primary care
```

This matters because a query may only match one section of a long document. Returning the best section gives the user a precise explanation instead of saying only that the entire document matched.

For example, a query for `HbA1c` should match the `Laboratory` section, not the whole discharge summary.

---

## 4. Clinical NER and Synonym Resolution

The system uses clinical named entity recognition to find medical concepts in both queries and document sections. Each detected phrase is mapped to a canonical concept.

Example synonym group:

```text
heart attack
MI
AMI
STEMI
NSTEMI
        |
        v
myocardial infarction
```

This allows different surface forms to match the same clinical meaning.

Example:

```text
Query:    heart attack
Document: STEMI
```

Both resolve to:

```text
myocardial infarction
```

So they are treated as a clinical concept match even though the exact words are different.

Each detected entity carries metadata:

| Field | Meaning | Example |
|---|---|---|
| `canonical` | Normalized concept name | `myocardial infarction` |
| `category` | Clinical category | `cardiovascular` |
| `specificity` | Concept importance level | `high`, `medium`, or `low` |
| `negated` | Whether the entity is negated in text | `true` or `false` |

---

## 5. Negation Handling

Clinical text often mentions conditions that are explicitly absent.

Example:

```text
No evidence of MI.
```

A keyword system may incorrectly treat this as evidence of myocardial infarction because it sees `MI`.

This system detects the negation trigger before the entity:

```text
No evidence of MI
|-------------| |
negation       entity
```

The entity is interpreted as:

```text
canonical: myocardial infarction
negated: true
```

Negated entities are excluded from positive Clinical Concept Overlap scoring and are surfaced as warnings in the result explanation.

Example warning:

```text
The document mentions myocardial infarction, but it appears negated: "No evidence of MI."
```

This prevents the search engine from ranking documents highly just because they mention a disease in a negative context.

---

## 6. Dual Indexing

Each section is indexed two ways:

1. Dense semantic index
2. Sparse keyword index

These two indexes solve different retrieval problems.

---

### 6.1 Dense Index: Semantic Retrieval

Dense retrieval uses a sentence-transformer embedding model to convert each section into a vector.

Example:

```text
"shortness of breath"
        |
        v
[0.12, -0.08, 0.44, ...]

"dyspnea"
        |
        v
[0.11, -0.07, 0.46, ...]
```

Because these vectors are close together, semantic search can match them even when the words are different.

Conceptually:

```text
Vector Space

                 "dyspnea"
                    *
                  /
                 /
"shortness of breath"
        *


Far away:
"quarterly budget forecast"
        *
```

Dense search is useful for:

- Paraphrases
- Related clinical descriptions
- Similar clinical context
- Queries that use layperson language instead of medical terminology

Example:

```text
Query:    shortness of breath
Document: dyspnea
```

A dense embedding model can recognize that these are semantically close.

---

### 6.2 Sparse Index: BM25 Keyword Retrieval

Sparse retrieval uses BM25 over synonym-expanded tokens. BM25 is strong when exact words, abbreviations, or codes matter.

Example query expansion:

```text
MI
myocardial infarction
heart attack
AMI
STEMI
NSTEMI
```

If the document says:

```text
STEMI noted on EKG.
```

BM25 can match it strongly because `STEMI` is part of the synonym-expanded query.

Sparse search is useful for:

- Clinical abbreviations
- Procedure codes
- Lab names
- Medication names
- Exact diagnosis names
- Rare terms that embedding models may not understand well

---

## 7. Query Processing

At query time, the system performs several steps before retrieval.

Example user query:

```text
heart attack with shortness of breath
```

### 7.1 Detect Clinical Concepts

The query is analyzed by the same clinical NER pipeline used during indexing.

Detected entities:

| Query phrase | Canonical concept | Specificity |
|---|---|---|
| heart attack | myocardial infarction | high |
| shortness of breath | dyspnea | medium |

Internally, the query is understood as:

```text
myocardial infarction + dyspnea
```

---

### 7.2 Determine Query Tier

The query tier controls the minimum score a result must pass.

| Tier | Condition | Threshold | Reason |
|---|---|---:|---|
| `clinical` | Query contains at least one high-specificity entity | 0.28 | Precise clinical queries can use a lower threshold |
| `partial` | Query contains only low-specificity clinical entities | 0.35 | Generic symptoms need a stronger match |
| `non_clinical` | No clinical entities are detected | 0.55 | Suppresses off-topic queries |

Examples:

```text
Query: myocardial infarction
Tier: clinical
Threshold: 0.28
```

```text
Query: chest pain
Tier: partial
Threshold: 0.35
```

```text
Query: quarterly budget forecast
Tier: non_clinical
Threshold: 0.55
```

This adaptive thresholding reduces noisy results for vague or non-clinical queries while keeping recall high for specific clinical searches.

---

## 8. Hybrid Retrieval

After query processing, the system searches both indexes.

For the query:

```text
heart attack with shortness of breath
```

Dense search may return semantically similar sections:

```text
1. Section A: acute coronary syndrome with dyspnea
2. Section B: cardiac catheterization after STEMI
3. Section C: respiratory distress
```

BM25 may return exact or synonym-expanded keyword matches:

```text
1. Section B: STEMI with dyspnea
2. Section D: history of MI
3. Section A: acute coronary syndrome with dyspnea
```

The results are then combined using Reciprocal Rank Fusion.

---

## 9. Reciprocal Rank Fusion (RRF)

RRF combines ranked lists without needing to calibrate the raw scores from the dense and sparse systems.

The formula is:

```text
RRF_score = sum(1 / (k + rank_in_list))
```

The system uses:

```text
k = 60
```

Example:

| Section | Dense Rank | BM25 Rank | Interpretation |
|---|---:|---:|---|
| Section A | 1 | 3 | Strong in both systems |
| Section B | 2 | 1 | Strong in both systems |
| Section C | 3 | N/A | Semantic-only match |
| Section D | N/A | 2 | Keyword-only match |

Visualization:

```text
Dense Results              BM25 Results
-------------              ------------
1. Section A               1. Section B
2. Section B               2. Section D
3. Section C               3. Section A

             |
             v

        RRF Fusion

1. Section B   appears high in both
2. Section A   appears high in both
3. Section D   keyword-only
4. Section C   semantic-only
```

RRF rewards candidates that appear near the top of both lists.

After RRF, multiple sections from the same source document are collapsed so that only the best-scoring section per document is retained.

---

## 10. Composite Scoring

RRF identifies promising candidates. Composite scoring decides how relevant each candidate really is.

The composite score is:

```text
composite = 0.40 * CCO + 0.30 * SCS + 0.20 * BTS + 0.10 * SRB
```

Each signal is normalized to a 0-1 range.

---

### 10.1 Clinical Concept Overlap (CCO)

CCO measures overlap between normalized clinical concepts in the query and document section.

Example:

```text
Query concepts:
{myocardial infarction, dyspnea}

Document concepts:
{myocardial infarction, dyspnea}
```

This is a perfect clinical concept match:

```text
CCO = 1.00
```

CCO uses weighted Jaccard overlap. Higher-specificity concepts receive more weight.

| Specificity | Weight | Example |
|---|---:|---|
| High | 3.0 | myocardial infarction |
| Medium | 1.5 | dyspnea |
| Low | 0.5 | pain |

This means a rare or diagnostic concept matters much more than a generic symptom.

Visualization:

```text
High-specificity concept:
myocardial infarction
############

Low-specificity concept:
pain
##
```

This prevents broad terms like `pain` from dominating the relevance score.

---

### 10.2 Semantic Cosine Similarity (SCS)

SCS measures how close the query embedding is to the document section embedding.

Example:

```text
Query:
heart attack with shortness of breath

Document:
STEMI with dyspnea
```

Even though the exact words differ, the semantic meaning is close.

Example score:

```text
SCS = 0.82
```

Conceptual view:

```text
Query vector             Document vector
     * ---------------------- *
          close together

High cosine similarity
```

Because vectors are L2-normalized, dot product is equivalent to cosine similarity.

---

### 10.3 BM25 Term Score (BTS)

BTS measures exact lexical matching after synonym expansion.

Example expanded query:

```text
heart attack -> myocardial infarction, MI, AMI, STEMI, NSTEMI
shortness of breath -> dyspnea
```

Document text:

```text
Acute STEMI with dyspnea.
```

BM25 sees strong exact matches for `STEMI` and `dyspnea`.

Example score:

```text
BTS = 0.90
```

BTS is especially useful for abbreviations and clinical shorthand that general embedding models may miss.

---

### 10.4 Section Relevance Bonus (SRB)

SRB encodes clinical knowledge about where important information usually appears.

Example bonuses:

| Section | Bonus |
|---|---:|
| Assessment and Plan | 1.00 |
| Chief Complaint | 0.95 |
| History of Present Illness | 0.85 |
| Laboratory | 0.80 |
| Physical Examination | 0.75 |
| Medications | 0.70 |
| Past Medical History | 0.65 |
| Discharge Instructions | 0.40 |
| Default | 0.60 |

A diagnosis in `Assessment and Plan` usually carries more clinical meaning than boilerplate text in `Discharge Instructions`.

Example:

```text
Assessment and Plan:
Acute STEMI with dyspnea.
```

SRB:

```text
1.00
```

Compared with:

```text
Discharge Instructions:
Call your doctor if you develop chest pain.
```

SRB:

```text
0.40
```

---

## 11. Full Scoring Example

Query:

```text
heart attack with shortness of breath
```

Candidate section:

```text
=== ASSESSMENT AND PLAN ===
Acute STEMI with dyspnea. Cardiology consulted for myocardial infarction management.
```

Detected query concepts:

```text
myocardial infarction
dyspnea
```

Detected document concepts:

```text
myocardial infarction
dyspnea
```

Example signal scores:

| Signal | Value | Weight | Contribution |
|---|---:|---:|---:|
| CCO | 1.00 | 0.40 | 0.400 |
| SCS | 0.82 | 0.30 | 0.246 |
| BTS | 0.90 | 0.20 | 0.180 |
| SRB | 1.00 | 0.10 | 0.100 |

Final score:

```text
0.400 + 0.246 + 0.180 + 0.100 = 0.926
```

Composite score:

```text
0.926
```

Because this is above the `clinical` threshold of `0.28`, the document is returned as a strong match.

---

## 12. Why One Result Beats Another

Query:

```text
heart attack with shortness of breath
```

### Document A: Strong Match

```text
=== ASSESSMENT AND PLAN ===
Acute STEMI with dyspnea. Cardiology consulted.
```

| Signal | Score |
|---|---:|
| CCO | 1.00 |
| SCS | 0.82 |
| BTS | 0.90 |
| SRB | 1.00 |
| Final | 0.926 |

Why it ranks highly:

- Matches both clinical concepts
- Uses equivalent clinical terminology
- Contains exact/synonym-expanded terms
- Appears in a high-value clinical section

---

### Document B: Weaker Match

```text
=== DISCHARGE INSTRUCTIONS ===
Return to the ER if you experience chest pain or shortness of breath.
```

| Signal | Score |
|---|---:|
| CCO | 0.25 |
| SCS | 0.50 |
| BTS | 0.45 |
| SRB | 0.40 |
| Final | 0.380 |

Why it ranks lower:

- Mentions a symptom but not the main diagnosis
- Match occurs in discharge instructions
- Text may be generic patient education

This result may still pass the clinical threshold, but it should rank below Document A.

---

### Document C: Negated Match

```text
=== PAST MEDICAL HISTORY ===
No prior MI. Denies shortness of breath.
```

This document contains matching terms:

```text
MI
shortness of breath
```

But both are negated:

```text
No prior MI
Denies shortness of breath
```

The system should reduce positive clinical concept scoring and return a warning if the result appears.

Example explanation:

```text
Negation warning: The document mentions myocardial infarction and dyspnea, but both appear negated.
```

---

## 13. Result Explanation

Each returned result should explain why it matched.

A useful explanation includes:

| Explanation Field | Purpose |
|---|---|
| Matched concepts | Shows clinical concepts shared by query and document |
| Synonym matches | Shows when different terms resolved to the same concept |
| Negation warnings | Warns when a concept appears negated in the document |
| Best matching section | Shows where the match occurred |
| Key passage | Shows the most relevant text snippet |
| Borderline flag | Warns when the score is close to the threshold |

Example:

```text
Result: Document A
Score: 0.926
Best section: Assessment and Plan

Matched concepts:
- myocardial infarction
- dyspnea

Synonym matches:
- Query "heart attack" matched document "STEMI"
- Query "shortness of breath" matched document "dyspnea"

Key passage:
Acute STEMI with dyspnea. Cardiology consulted for myocardial infarction management.
```

---

## 14. Why Hybrid Search Is Better Than Keyword Search Alone

Keyword-only search is good at exact matches but weak at synonyms and paraphrases.

Example keyword problem:

```text
Query: heart attack
Document: STEMI
```

Without synonym handling, keyword search may miss this.

Semantic-only search is good at meaning but can miss rare abbreviations, codes, or specific clinical shorthand.

Example semantic problem:

```text
Query: MI
Document: myocardial infarction
```

A general embedding model may not reliably understand all clinical abbreviations.

Hybrid search combines both strengths:

```text
Keyword search:
- MI
- STEMI
- HbA1c
- CPT codes
- medication names

Semantic search:
- heart attack ~= myocardial infarction
- shortness of breath ~= dyspnea
- high blood sugar ~= hyperglycemia

Clinical scoring:
- concept normalization
- specificity weighting
- negation detection
- section importance
```

---

## 15. Mental Model

Think of the algorithm as a group of reviewers.

```text
Reviewer 1: Do the same clinical concepts appear?
Reviewer 2: Does the passage mean the same thing as the query?
Reviewer 3: Are exact terms, abbreviations, or synonyms present?
Reviewer 4: Did the match occur in an important clinical section?
```

The final score is the weighted judgment of all reviewers.

A result is strong when it satisfies all four:

```text
Same clinical concepts
+ Similar meaning
+ Exact/synonym term match
+ Important clinical section
= High relevance
```

---

## 16. Implementation Notes

### Current POC Strengths

- Fully offline
- No external infrastructure required
- Combines semantic and keyword retrieval
- Uses section-level indexing
- Handles synonyms and abbreviations
- Detects simple negation
- Produces human-readable explanations

### Current POC Limitations

- General-purpose embedding model may miss clinical relationships
- EntityRuler only covers configured synonym groups
- BM25 index is rebuilt as a full pickle file
- Local ChromaDB is not ideal for high-concurrency production use
- No LLM re-ranker
- No HIPAA-grade audit logging, authentication, or access control

### Recommended Enterprise Direction

For production clinical search, the most important upgrades are:

1. Replace the embedding model with a clinical embedding model.
2. Replace hand-curated NER with a medical NLP system such as medspaCy + QuickUMLS, scispaCy, Azure Text Analytics for Health, or AWS Comprehend Medical.
3. Replace local ChromaDB with a scalable vector database such as Qdrant, Weaviate, Pinecone, Milvus, or pgvector.
4. Replace BM25 pickle storage with Elasticsearch, OpenSearch, or a vector database that supports sparse vectors.
5. Add a re-ranking step for the top candidates.
6. Add authentication, audit logging, encryption, and access controls before handling real patient data.

---

## 17. Summary

Clinical Relevance Search works by combining broad retrieval with careful clinical scoring.

The retrieval stage finds candidates using both:

```text
Dense semantic search + Sparse keyword search
```

The ranking stage scores each candidate using:

```text
Clinical concept overlap
+ Semantic similarity
+ BM25 term strength
+ Section importance
```

The final model is designed to answer this question:

```text
Is this document clinically relevant to the user's query, and can we explain why?
```

That explainability is the most important feature. The system does not only return documents; it shows the matched clinical concepts, synonym mappings, section location, key passage, and negation warnings so a user can understand why the result was returned.
