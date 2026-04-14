# Mariana Computer — Fix AI Layer + REST API + Capabilities

**Date**: 2026-04-14  
**Scope**: BUG-06 fix, REST API creation, requirements update, docker-compose update

---

## TASK A — Fix BUG-06: `spawn_model` call signature mismatch

**File modified**: `mariana/orchestrator/event_loop.py`

### Problem
Every handler function (`handle_init`, `handle_search`, `handle_evaluate`,
`handle_deepen`, `handle_tribunal`, `handle_skeptic`, `handle_report`) was
calling `spawn_model` with the wrong signature:

```python
# WRONG — before fix
ai_session = await spawn_model(
    task_id=task.id,          # not a parameter
    task_type=...,
    branch_id=None,
    prompt_context={...},     # wrong param name
    config=config,
)
# Missing: context (renamed), output_schema (required), db, cost_tracker
# Wrong: treated return value as single AISession instead of tuple
```

The actual `spawn_model` signature (from `mariana/ai/session.py:587`) is:

```python
async def spawn_model(
    task_type: TaskType,
    context: dict[str, Any],
    output_schema: type[BaseModel],
    max_tokens: int = 4096,
    use_batch: bool = False,
    branch_id: str | None = None,
    db: Any = None,
    cost_tracker: Any = None,
    config: AppConfig | None = None,
) -> tuple[BaseModel, AISession]:
```

### Changes Made

1. **Added module-level import** at the top of `event_loop.py`:
   ```python
   from mariana.ai.session import spawn_model
   from mariana.data.models import (
       EvidenceExtractionOutput,
       EvaluationOutput,
       HypothesisGenerationOutput,
       ReportDraftOutput,
       SkepticQuestionsOutput,
       TribunalArgumentOutput,
       TribunalVerdictOutput,
       ...
   )
   ```

2. **Removed all local `from mariana.ai.session import spawn_model` imports**
   that existed inside each handler function.

3. **Fixed `handle_init`** — uses `HypothesisGenerationOutput` as schema; 
   unpacks `(_, ai_session)`.

4. **Fixed `handle_search`** — uses `EvidenceExtractionOutput`; proper `context` 
   dict with `task_id`, `hypothesis_id`, `sources_already_searched`, 
   `budget_remaining`.

5. **Fixed `handle_evaluate`** — uses `EvaluationOutput`; unpacks as 
   `(eval_output, ai_session)` and reads score directly from 
   `eval_output.score` instead of a redundant DB query. Also eliminated the 
   `* 10.0` score multiplication bug (BUG-14) implicitly by reading the 
   schema's `score` field directly (already in [0,1] range).

6. **Fixed `handle_deepen`** — uses `EvidenceExtractionOutput`; passes mode 
   `"DEEPEN"` in context.

7. **Fixed `handle_tribunal`** — split into two loops:
   - Argument stages (PLAINTIFF, DEFENDANT, REBUTTAL, COUNTER): use 
     `TribunalArgumentOutput`
   - Judge stage (JUDGE): uses `TribunalVerdictOutput`

8. **Fixed `handle_skeptic`** — uses `SkepticQuestionsOutput`.

9. **Fixed `handle_report`** (also resolves BUG-09):
   - Changed `from mariana.report.generator import compile_report` →
     `from mariana.report.generator import generate_report`
   - `generate_report` takes the full task object + pre-fetched lists 
     (confirmed findings, all sources, failed hypotheses) rather than just 
     `task_id`.
   - `handle_report` now fetches those lists from the DB before delegating 
     to `generate_report`, which internally handles both AI passes and PDF 
     rendering.

### Verification
```
$ python -c "from mariana.orchestrator.event_loop import run, handle_init, \
    handle_search, handle_evaluate, handle_deepen, handle_tribunal, \
    handle_skeptic, handle_report; print('OK')"
OK
```

All 7 `spawn_model` calls use tuple unpacking and the correct parameter names.
No `compile_report` references remain.

---

## TASK B — REST API layer

**File created**: `mariana/api.py` (992 lines)

### Architecture
- **Framework**: FastAPI with `asynccontextmanager` lifespan
- **Startup**: loads `AppConfig` → creates asyncpg pool → runs `init_schema` 
  → connects Redis
- **CORS**: fully open (`allow_origins=["*"]`) for development; tighten for 
  production
- **SSE**: `sse-starlette` used for real-time log streaming

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | `{"status": "ok", "version": "0.1.0"}` |
| GET | `/api/config` | Sanitised AppConfig (no API keys) |
| POST | `/api/investigations` | Start investigation — drops `.task.json` in inbox |
| GET | `/api/investigations` | Paginated task list with status filter |
| GET | `/api/investigations/{task_id}` | Single task detail |
| POST | `/api/investigations/{task_id}/kill` | Kill running task |
| GET | `/api/investigations/{task_id}/branches` | List branches |
| GET | `/api/investigations/{task_id}/findings` | List findings (evidence) |
| GET | `/api/investigations/{task_id}/cost` | Cost breakdown per-model + per-branch |
| GET | `/api/investigations/{task_id}/logs` | SSE real-time log stream |
| GET | `/api/investigations/{task_id}/report` | Download PDF report |
| GET | `/api/investigations/{task_id}/report/docx` | Download DOCX report (future) |
| GET | `/api/connectors` | List connectors and health status |
| POST | `/api/shutdown` | Graceful shutdown |

### Key design decisions
- **Daemon-mode submission**: POST `/api/investigations` writes a JSON file to 
  `config.inbox_dir` so the offline orchestrator daemon picks it up. Returns 
  202 Accepted immediately.
- **SSE logs**: Uses Redis pub/sub on channel `logs:<task_id>` when Redis is 
  available; falls back to DB polling (2s interval) when Redis is unavailable.
- **Kill signal**: Updates DB status to `HALTED` and publishes `kill:<task_id}` 
  to Redis for the orchestrator to detect.
- **Cost breakdown**: Queries `ai_sessions` table for per-model and per-branch 
  aggregates.
- **Report download**: Streams the file from disk as `application/pdf`.
- **Connector health**: Reports API key presence + Redis/DB connectivity without 
  performing live HTTP probes (fast, no external dependencies at call time).

### Verification
```
$ python -c "from mariana.api import app; print([r.path for r in app.routes])"
['/api/health', '/api/config', '/api/investigations', '/api/investigations',
 '/api/investigations/{task_id}', '/api/investigations/{task_id}/kill',
 '/api/investigations/{task_id}/branches', '/api/investigations/{task_id}/findings',
 '/api/investigations/{task_id}/cost', '/api/investigations/{task_id}/logs',
 '/api/investigations/{task_id}/report', '/api/investigations/{task_id}/report/docx',
 '/api/connectors', '/api/shutdown']
```

---

## TASK C — Missing capabilities in requirements.txt

**File modified**: `requirements.txt`

### Changes

| Package | Version | Purpose |
|---------|---------|---------|
| `uvicorn[standard]` | 0.34.0 | Updated from bare `uvicorn` to include websocket/h11 extras |
| `python-multipart` | 0.0.20 | Required by FastAPI for form data parsing |
| `sse-starlette` | 2.2.1 | Server-Sent Events support for log streaming endpoint |
| `python-docx` | 1.1.2 | DOCX report generation (future capability) |

The following were already present and retained unchanged:
`weasyprint==63.1`, `fastapi==0.115.6`, `asyncpg==0.30.0`, `redis[hiredis]==5.2.1`,
`pydantic==2.10.3`, `httpx==0.28.1`, `structlog==24.4.0`, `jinja2==3.1.4`,
`python-dotenv==1.0.1`, `tenacity==9.0.0`, `tiktoken==0.8.0`

---

## docker-compose.yml update

**File modified**: `docker-compose.yml`

Added `mariana-api` service:

```yaml
mariana-api:
  build: .
  command: ["python", "-m", "uvicorn", "mariana.api:app", "--host", "0.0.0.0", "--port", "8080"]
  ports:
    - "8080:8080"
  env_file: .env
  depends_on:
    - postgresql
    - redis
  volumes:
    - mariana-data:/data/mariana
  restart: unless-stopped
```

Also added `mariana-data` to the named volumes block (was missing, required 
for inbox / reports path sharing between orchestrator and API containers).

---

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `mariana/orchestrator/event_loop.py` | Modified | BUG-06 + BUG-09 fixes |
| `mariana/api.py` | Created | Full REST API backend |
| `requirements.txt` | Modified | Added 4 missing packages |
| `docker-compose.yml` | Modified | Added `mariana-api` service + `mariana-data` volume |
| `FIX_AI_API_SUMMARY.md` | Created | This document |
