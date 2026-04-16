# Mariana Learning Loop + User-Driven Flow — Design Document

## 1. User-Driven Flow (AI Obeys Human Directives)

### Problem
Currently, the state machine has hardcoded kill conditions:
- `SKEPTIC_CRITICAL_OPEN → HALT` kills investigations before reports
- `BRANCH_SCORE_LOW → KILL_BRANCH` kills promising branches the user wants kept
- No way for user to say "just make the report even if quality isn't perfect"

### Solution: User Directive System

**New TransitionTriggers:**
- `FORCE_REPORT` — user demands report regardless of quality gate
- `USER_OVERRIDE` — generic override for any user directive

**New fields in `StartInvestigationRequest`:**
- `force_report_on_halt: bool` — if true, generate report instead of halting
- `skip_skeptic: bool` — skip the skeptic gate entirely
- `skip_tribunal: bool` — skip adversarial tribunal
- `max_depth_override: int` — override max research depth
- `user_directives: dict` — freeform directives dict

**State machine changes:**
- `SKEPTIC + CRITICAL_OPEN`: if `force_report_on_halt=True` → REPORT instead of HALT
- `CHECKPOINT + CONSECUTIVE_DR_FLAGS_3`: if `force_report_on_halt=True` → REPORT instead of HALT
- `TRIBUNAL + DESTROYED`: if `force_report_on_halt=True` → REPORT instead of PIVOT/HALT
- All transitions respect `user_flow_instructions` in prompt context

### Implementation
1. Add `user_directives` to `ResearchSessionData`
2. Modify `_apply_guards()` to check directives before hard-halting
3. Modify `_trigger_for_skeptic()` to check directives
4. Thread directives through `task.metadata`

---

## 2. Learning Loop

### Architecture
```
User Feedback → learning_events table → Pattern Extraction → learning_insights
                                                              ↓
Investigation Start → Query learning_insights → Inject into prompts
```

### DB Schema

**`learning_events`** — raw feedback from users
```sql
CREATE TABLE learning_events (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    task_id UUID REFERENCES research_tasks(id),
    event_type TEXT NOT NULL,  -- 'rating', 'feedback', 'correction', 'preference'
    category TEXT,             -- 'report_quality', 'search_depth', 'branch_decision', etc.
    content JSONB NOT NULL,    -- structured feedback payload
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**`investigation_outcomes`** — automated outcome tracking
```sql
CREATE TABLE investigation_outcomes (
    id UUID PRIMARY KEY,
    task_id UUID REFERENCES research_tasks(id) UNIQUE,
    user_id UUID NOT NULL,
    topic TEXT NOT NULL,
    quality_tier TEXT,
    total_cost_usd FLOAT,
    total_ai_calls INT,
    duration_seconds INT,
    final_state TEXT,
    report_generated BOOLEAN DEFAULT FALSE,
    user_rating INT,          -- 1-5 stars
    user_feedback TEXT,
    hypotheses_count INT,
    findings_count INT,
    killed_branches_count INT,
    tribunal_verdicts JSONB,
    skeptic_pass BOOLEAN,
    patterns JSONB,           -- extracted patterns for this investigation
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**`learning_insights`** — extracted patterns across investigations
```sql
CREATE TABLE learning_insights (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    insight_type TEXT NOT NULL,    -- 'topic_preference', 'depth_preference', etc.
    insight_key TEXT NOT NULL,
    insight_value JSONB NOT NULL,
    confidence FLOAT DEFAULT 0.5,
    sample_count INT DEFAULT 1,
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, insight_type, insight_key)
);
```

### Learning Event Types
- `rating` — user rates investigation 1-5 stars
- `feedback` — free-text feedback on investigation
- `correction` — "this finding is wrong", "this hypothesis was better"
- `preference` — "I prefer deeper research", "less aggressive branch killing"

### Pattern Extraction
After each investigation completes:
1. Record `investigation_outcomes` automatically
2. If user provides rating/feedback → store in `learning_events`
3. Periodically extract patterns:
   - Topics user cares about most (high ratings)
   - Preferred depth (budget usage vs satisfaction)
   - Branch kill sensitivity (user corrects kills → lower kill threshold)
   - Report format preferences

### Context Injection
At investigation start (in `handle_init`):
1. Query `learning_insights` for this user
2. Build a "learning context" string
3. Inject into every `spawn_model` call via context dict

### API Endpoints
- `POST /api/feedback` — submit feedback for an investigation
- `GET /api/feedback/{task_id}` — get feedback for a specific investigation
- `GET /api/learning/insights` — get user's learning insights
- `GET /api/learning/context` — get the learning context string for prompts
