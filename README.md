# Mariana Computer

Autonomous AI investigative research agent for financial markets. Operates like a team of investigative journalists and forensic accountants — generates hypotheses, gathers evidence from SEC filings, market data, and options flow, stress-tests findings through an adversarial tribunal, and produces publication-grade research reports.

## Architecture

```
Layer 1 (Python Orchestrator) — Deterministic state machine, never calls LLM directly
  ├── State Machine: INIT → SEARCH → EVALUATE → DEEPEN/KILL → TRIBUNAL → SKEPTIC → REPORT
  ├── Branch Manager: Budget allocation ($5 initial → $20 grant → $50 grant → $75 cap)
  ├── Cost Tracker: Per-branch and per-task budget enforcement
  └── Diminishing Returns: Automatic pivot/halt on stale research

Layer 2 (AI Sessions) — Ephemeral, stateless, max 40K tokens each
  ├── Model Router: Opus for strategy, DeepSeek for extraction, Sonnet for drafts
  ├── Prompt Builder: 3-block cache-optimized structure
  └── Output Parser: Structured JSON → Pydantic validation

Layer 3 (Data Connectors)
  ├── Polygon.io — Stock data, financials, news, options
  ├── Unusual Whales — Options flow, dark pool, congressional/insider trades
  ├── SEC EDGAR — Filings, company facts, full-text search
  └── FRED — Federal Reserve economic data

Layer 4 (Verification)
  ├── Adversarial Tribunal — 5 AI sessions (plaintiff → defendant → rebuttal → counter → judge)
  └── Skeptic Gauntlet — 20 hard questions, Layer 1 classification
```

## Quick Start

### Deploy to Hetzner Server

```bash
# From your MacBook — requires SSH key auth to root@77.42.3.206
chmod +x scripts/deploy.sh
bash scripts/deploy.sh
```

The script handles: system hardening, Docker install, Tailscale, code upload, .env creation, Docker build, and health checks.

### Manual Setup

```bash
ssh root@77.42.3.206

# Copy .env.example to .env, fill in API keys
cp .env.example .env
vi .env

# Build and start
docker compose build
docker compose up -d

# Verify
docker compose exec mariana-orchestrator python -m mariana.main --dry-run --topic "test" --budget 5
```

### Run an Investigation

```bash
# SSH into server
ssh deploy@77.42.3.206
cd ~/mariana

# Launch investigation
docker compose exec mariana-orchestrator python -m mariana.main \
  --topic "Investigate Super Micro Computer (SMCI) accounting practices and recent auditor changes" \
  --budget 50

# Monitor
docker compose logs -f mariana-orchestrator

# Check status
docker compose exec mariana-orchestrator python -m mariana.main --status

# Kill a task
docker compose exec mariana-orchestrator python -m mariana.main --kill-task <task_id>
```

### Daemon Mode

Drop `.task.json` files into `/data/mariana/inbox/`:

```json
{
  "topic": "Investigate XYZ Corp insider trading patterns",
  "budget": 100
}
```

The daemon picks up tasks automatically and runs them sequentially.

## Project Structure

```
mariana/
├── mariana/
│   ├── ai/                    # Layer 2: AI session management
│   │   ├── router.py          # Model routing (15 task types → models)
│   │   ├── prompt_builder.py  # 3-block prompt construction
│   │   ├── output_parser.py   # JSON extraction + Pydantic validation
│   │   ├── session.py         # spawn_model() — single AI entry point
│   │   └── prompts/           # 14 task-specific prompt templates
│   ├── connectors/            # Layer 3: Data connectors
│   │   ├── polygon_connector.py
│   │   ├── unusual_whales_connector.py
│   │   ├── sec_edgar_connector.py
│   │   └── fred_connector.py
│   ├── data/                  # Foundation
│   │   ├── models.py          # 30+ Pydantic models
│   │   ├── db.py              # AsyncPG database layer
│   │   └── cache.py           # Redis URL cache + query dedup
│   ├── orchestrator/          # Layer 1: Deterministic control
│   │   ├── state_machine.py   # 10-state machine, 17 triggers
│   │   ├── event_loop.py      # Main research loop
│   │   ├── branch_manager.py  # Budget grant/kill rules
│   │   ├── cost_tracker.py    # Real-time cost enforcement
│   │   ├── checkpoint.py      # Crash recovery
│   │   └── diminishing_returns.py
│   ├── tribunal/              # Layer 4: Verification
│   │   ├── adversarial.py     # 5-session tribunal
│   │   └── skeptic.py         # 20-question gauntlet
│   ├── report/                # Output
│   │   ├── generator.py       # AI-powered report writing
│   │   ├── renderer.py        # WeasyPrint PDF rendering
│   │   └── templates/report.html.j2
│   ├── browser/               # Placeholder for Playwright pool
│   ├── config.py              # All tuneable parameters
│   └── main.py                # Entry point (single/daemon modes)
├── data/                      # Persistent data (Docker volume)
├── docker-compose.yml         # PostgreSQL + Redis + Orchestrator
├── Dockerfile
├── requirements.txt
└── scripts/deploy.sh          # One-command deploy to Hetzner
```

## Model Routing

| Task | Model | Rationale |
|------|-------|-----------|
| Hypothesis Generation | Claude Opus 4 | Strategic reasoning |
| Evidence Extraction | DeepSeek V3 | Cheap mechanical parsing |
| Evaluation | Claude Opus 4 | Critical scoring decisions |
| Tribunal (all 5 sessions) | Claude Opus 4 | Adversarial quality |
| Skeptic Questions | Claude Opus 4 | Must find real weaknesses |
| Report Draft | Claude Sonnet 4 | Good writing, lower cost |
| Report Final Edit | Claude Opus 4 | Polish and insight |
| Watchdog | Claude Haiku 4.5 | Cheap circularity check |
| Compression | Claude Sonnet 4 | Synthesis quality |

## Budget Rules

- Initial allocation per hypothesis: $5.00
- Score ≥ 7 → grant $20 more
- Score ≥ 8 after second grant → grant $50 more
- Score < 4 → KILL immediately
- Score 4-6 with no progress → KILL after retry
- Hard cap per branch: $75
- Hard cap per task: $400

## Reports

Output at: `/data/mariana/reports/<task_id>/report.pdf`

Styled as institutional research reports (Muddy Waters / Hindenburg style). Bilingual EN/ZH with executive summary, evidence sections, and source citations.
