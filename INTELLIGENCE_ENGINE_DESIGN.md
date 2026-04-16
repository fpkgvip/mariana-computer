# Mariana Intelligence Engine — Architecture Design

## Overview

13 new analytical intelligence systems that transform Mariana from a "search-and-summarize" tool
into a true research analyst. These systems operate on structured data (claims, sources, evidence)
rather than raw text, enabling rigorous reasoning.

## New Module Structure

```
mariana/orchestrator/intelligence/
├── __init__.py
├── credibility.py          # 3. Source Credibility Scoring Engine
├── contradictions.py       # 4. Contradiction Detection & Resolution
├── replanner.py            # 5. Adaptive Query Decomposition & Replanning
├── evidence_ledger.py      # 6. Claim Extraction & Evidence Ledger
├── confidence.py           # 7. Confidence Calibration Layer
├── hypothesis_engine.py    # 8. Hypothesis Generation & Testing (Bayesian)
├── gap_detector.py         # 9. Gap Detection & Proactive Follow-Up
├── temporal.py             # 10. Temporal Reasoning Engine
├── perspectives.py         # 11. Multi-Perspective Synthesis
├── diversity.py            # 12. Source Diversity Enforcer
├── retrieval.py            # 13. Retrieval Strategy Selector
├── auditor.py              # 14. Reasoning Chain Auditor
└── executive_summary.py    # 15. Executive Summary Generator
```

## Database Schema Additions

### Table: claims
The atomic unit of knowledge. Every finding is decomposed into claims.

```sql
CREATE TABLE IF NOT EXISTS claims (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    finding_id      TEXT REFERENCES findings(id) ON DELETE SET NULL,
    hypothesis_id   TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
    subject         TEXT NOT NULL,           -- Entity (e.g., "Apple")
    predicate       TEXT NOT NULL,           -- Relationship (e.g., "revenue_was")
    object          TEXT NOT NULL,           -- Value (e.g., "$394B in FY2023")
    claim_text      TEXT NOT NULL,           -- Human-readable claim
    source_ids      JSONB NOT NULL DEFAULT '[]',
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    credibility_score DOUBLE PRECISION,      -- From credibility engine
    corroboration_count INTEGER NOT NULL DEFAULT 0,
    contradiction_ids JSONB NOT NULL DEFAULT '[]',  -- IDs of contradicting claims
    temporal_start  TIMESTAMPTZ,            -- When claim became true
    temporal_end    TIMESTAMPTZ,            -- When claim stopped being true (NULL = still true)
    temporal_type   TEXT DEFAULT 'point',    -- point, range, ongoing
    is_resolved     BOOLEAN NOT NULL DEFAULT FALSE,
    resolution_note TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: source_scores
Credibility scores for each source.

```sql
CREATE TABLE IF NOT EXISTS source_scores (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL,
    credibility     DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    relevance       DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    recency         DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    composite_score DOUBLE PRECISION NOT NULL DEFAULT 0.5,  -- credibility * relevance * recency
    domain_authority TEXT DEFAULT 'unknown',   -- academic, government, news, blog, social, official
    publication_type TEXT DEFAULT 'unknown',   -- peer_reviewed, editorial, press_release, blog_post
    cross_ref_density INTEGER NOT NULL DEFAULT 0,
    scoring_rationale TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: contradiction_pairs
Detected contradictions between claims.

```sql
CREATE TABLE IF NOT EXISTS contradiction_pairs (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    claim_a_id      TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    claim_b_id      TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    contradiction_type TEXT NOT NULL DEFAULT 'direct', -- direct, temporal, quantitative, qualitative
    severity        DOUBLE PRECISION NOT NULL DEFAULT 0.5,  -- 0=minor, 1=critical
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',  -- unresolved, resolved_a, resolved_b, both_valid, irreconcilable
    resolution_source_id TEXT,               -- Third source that resolved it
    resolution_note TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: research_plans
Adaptive query plans that evolve as evidence arrives.

```sql
CREATE TABLE IF NOT EXISTS research_plans (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL DEFAULT 1,
    plan_data       JSONB NOT NULL DEFAULT '{}',  -- Full plan structure
    trigger_reason  TEXT,                    -- Why this replan happened
    spawned_branches JSONB DEFAULT '[]',     -- New branches spawned from replan
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: hypothesis_priors
Bayesian tracking for hypothesis testing.

```sql
CREATE TABLE IF NOT EXISTS hypothesis_priors (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    hypothesis_id   TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    prior           DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    posterior       DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    evidence_updates JSONB NOT NULL DEFAULT '[]',  -- [{claim_id, likelihood_ratio, posterior_after}]
    last_updated    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(task_id, hypothesis_id)
);
```

### Table: gap_analyses
Gap detection results per investigation.

```sql
CREATE TABLE IF NOT EXISTS gap_analyses (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    gaps            JSONB NOT NULL DEFAULT '[]',     -- [{description, priority, category, follow_up_query}]
    follow_ups_launched JSONB NOT NULL DEFAULT '[]',  -- Branch IDs spawned to fill gaps
    analysis_round  INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: perspective_syntheses
Multi-perspective analysis results.

```sql
CREATE TABLE IF NOT EXISTS perspective_syntheses (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    perspective     TEXT NOT NULL,           -- bull, bear, skeptic, domain_expert
    synthesis_text  TEXT NOT NULL,
    key_arguments   JSONB NOT NULL DEFAULT '[]',
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    cited_claim_ids JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: audit_results
Reasoning chain audit results.

```sql
CREATE TABLE IF NOT EXISTS audit_results (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id         TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    audit_type      TEXT NOT NULL DEFAULT 'full',  -- full, incremental
    issues          JSONB NOT NULL DEFAULT '[]',   -- [{type, severity, description, location, suggestion}]
    passed          BOOLEAN NOT NULL DEFAULT FALSE,
    overall_score   DOUBLE PRECISION NOT NULL DEFAULT 0.0,  -- 0-1
    auditor_notes   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

## Integration Points (Event Loop)

### Where each system hooks in:

1. **After handle_search (evidence extraction)**:
   - Claim Extraction → decompose findings into atomic claims
   - Source Credibility → score each source used
   - Temporal Reasoning → tag claims with timestamps
   - Contradiction Detection → compare new claims vs existing ledger

2. **After handle_evaluate (branch scoring)**:
   - Confidence Calibration → update claim confidences with new corroboration data
   - Hypothesis Testing → Bayesian update posteriors
   - Gap Detection → check what's missing after each eval round
   - Source Diversity → check if evidence is too concentrated
   - Adaptive Replanning → decide if plan needs revision

3. **Before handle_report**:
   - Multi-Perspective Synthesis → generate bull/bear/skeptic/expert views
   - Reasoning Chain Auditor → final quality gate
   - Executive Summary → generate multi-level summaries

4. **During handle_search (before dispatching)**:
   - Retrieval Strategy Selector → choose best retrieval method per branch
   - Source Diversity Enforcer → steer search toward underrepresented source types

## System-by-System Design

### 3. Source Credibility Scoring Engine
- Input: Source URL, domain, content
- LLM call: Classify domain authority + publication type
- Algorithmic: Recency decay (exponential, half-life = 180 days)
- Algorithmic: Cross-reference density from claims table
- Output: SourceScore = credibility × relevance × recency

### 4. Contradiction Detection
- Input: All claims for a task
- LLM call: NLI-style pairwise comparison (batched — only compare claims with overlapping subjects)
- Output: contradiction_pairs records, contradiction matrix for synthesis

### 5. Adaptive Replanning
- Input: Current research plan, evidence collected so far, gap analysis
- Trigger: Every N evaluation cycles (configurable, default 3)
- LLM call: Evaluate plan effectiveness, propose modifications
- Output: Updated research_plans record, optionally spawn new branches

### 6. Claim Extraction & Evidence Ledger
- Input: Finding record (raw text)
- LLM call: Decompose into (Subject, Predicate, Object) triples
- Output: Multiple claim records per finding

### 7. Confidence Calibration
- Pure algorithmic (no LLM call):
  confidence = (source_credibility * 0.3) + (corroboration_ratio * 0.3) + (recency * 0.2) + (consistency * 0.2)
- Where consistency = 1 - (contradiction_count / total_related_claims)
- Runs after every claim insert/update

### 8. Hypothesis Testing (Bayesian)
- On task init: generate 3-5 competing hypotheses with prior = 1/n
- After each claim: compute likelihood ratio P(evidence|H) / P(evidence|¬H)
- Update posterior via Bayes: P(H|E) ∝ P(E|H) * P(H)
- LLM call: estimate likelihood ratios (since we can't compute them analytically)
- Report winning hypothesis with full evidence chain

### 9. Gap Detection
- Input: Evidence ledger, research plan, hypothesis priors
- LLM call: Given what we know, what's missing?
- Output: Prioritized list of gaps, auto-launch follow-up branches for critical gaps

### 10. Temporal Reasoning
- Part of claim extraction: LLM extracts temporal metadata (when was this true?)
- Algorithmic: Detect temporal conflicts (same subject, different values, overlapping times)
- Algorithmic: Prefer recent data when user query is present-tense, historical for trend queries

### 11. Multi-Perspective Synthesis
- Run 4 parallel LLM calls with different system prompts (bull/bear/skeptic/expert)
- Each reads the full evidence ledger + claims
- Meta-synthesizer merges into balanced report with explicit disagreement sections

### 12. Source Diversity Enforcer
- Algorithmic: Track source_type distribution per task
- If any single domain > 40% of sources, flag for diversification
- If any source_type category > 60%, flag
- Inject diversity constraints into search prompts

### 13. Retrieval Strategy Selector
- Input: Query type, topic domain, existing evidence
- LLM call (lightweight, Haiku): classify → {web_search, academic_search, sec_filing, government_data, financial_api, news_archive}
- Route to appropriate connector/adapter

### 14. Reasoning Chain Auditor
- Input: Full evidence chain from claims → synthesis → conclusions
- LLM call (high-quality, Opus): Check for logical fallacies, unsupported jumps, circular reasoning
- Output: Pass/fail + list of issues
- If fail: route back to gap detection for remediation

### 15. Executive Summary Generator
- Input: Full evidence ledger
- 4 LLM calls at different compression levels:
  - One-liner: single most important insight
  - Paragraph: top 3-5 insights
  - Page: comprehensive summary with citations
  - Full: everything with detailed citations (this is the existing report)

## Cost Management

Each system's LLM calls use the task's cost tracker. Model selection:
- Claim extraction: Haiku (high volume, simple task)
- Credibility classification: Haiku
- Contradiction NLI: Sonnet (needs nuance)
- Replanning: Sonnet
- Gap detection: Sonnet
- Perspective synthesis: Opus (4 parallel calls, needs depth)
- Auditor: Opus (quality gate, needs rigor)
- Executive summaries: Sonnet (4 compression levels)
- Bayesian likelihood estimation: Sonnet
- Retrieval strategy: Haiku (simple classification)

## State Machine Integration

New states/triggers are NOT needed. The 13 systems are injected as sub-steps within existing states:
- SEARCH → + claim extraction, credibility scoring, temporal tagging, contradiction check
- EVALUATE → + confidence calibration, Bayesian update, gap detection, diversity check, replanning
- REPORT → + multi-perspective synthesis, audit, executive summary
- INIT → + hypothesis priors initialization, retrieval strategy selection

The systems are activated via a feature flag in task metadata: `intelligence_engine: true`.
Default: enabled for all new investigations.
