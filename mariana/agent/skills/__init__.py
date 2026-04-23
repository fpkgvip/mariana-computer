"""Skill library — domain-specific system-prompt snippets injected into the
planner based on lightweight intent classification of the user's goal.

Each skill is a tuple of (name, keywords, guidance).  The planner scores the
goal against the keyword set of each skill and injects the matching guidance
into the system prompt *after* the base prompt and tool manifest.  Non-matching
skills cost nothing.

Principles:
- Skills are additive.  They never replace the base prompt.
- Skills are LLM-agnostic.  No model-specific tokens.
- Skills encode best practices, pitfalls, and preferred tooling — not code.
- A goal may match multiple skills; top-2 by score are injected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    name: str
    keywords: tuple[str, ...]
    guidance: str


# ---------------------------------------------------------------------------
# Skill definitions
# ---------------------------------------------------------------------------


CODING = Skill(
    name="coding",
    keywords=(
        "code", "coding", "program", "script", "implement", "function",
        "class", "module", "library", "api", "cli", "tool", "debug", "refactor",
        "unit test", "pytest", "lint", "typecheck", "compile", "build",
        "python", "rust", "go", "typescript", "javascript", "c++", "java",
    ),
    guidance="""SKILL: Coding (production-grade software engineering)

When the goal involves writing, debugging, or refactoring code:
- Prefer strongly-typed languages with explicit error handling.  When writing
  Python, include type hints on every function signature and at least one
  self-test via `assert` or `pytest`.  When writing Rust/Go/TS/C++, use
  idiomatic error types and avoid panics / unchecked unwraps.
- Start by listing the expected inputs, outputs, edge cases, and failure modes
  in a `think` step BEFORE writing code.
- After writing code, ALWAYS run it (code_exec / bash_exec) with representative
  inputs and verify observable behaviour, not just exit code.
- Cover at minimum: happy path, empty input, malformed input, boundary values.
- If a test fails, inspect stderr in detail before attempting a fix — do not
  guess.  The fix loop has a hard cap of 5 attempts per step.
- Keep files small and focused; prefer multiple files over one giant file when
  the module count exceeds ~3 logical groupings.
""",
)


WEB_DEV = Skill(
    name="web_dev",
    keywords=(
        "website", "web app", "webapp", "frontend", "backend", "react",
        "next.js", "nextjs", "vue", "svelte", "tailwind", "html", "css",
        "http server", "rest api", "graphql", "express", "fastapi", "flask",
        "vite", "webpack", "deploy", "vercel", "netlify", "dockerfile",
    ),
    guidance="""SKILL: Web Development

When the goal involves building web apps, HTTP APIs, or frontends:
- For static sites, write an index.html that renders correctly when opened
  directly from the filesystem.  Inline CSS/JS unless the task demands a
  multi-file structure.
- For APIs, always define: route table, request/response schemas, error codes,
  auth model.  Pick FastAPI for Python, Axum for Rust, Fastify/Hono for Node.
- Wire a health check (`/api/health` returning `{status:"ok"}`) before any
  business logic so you can ping it from bash_exec.
- For frontend frameworks, run `npm install` and `npm run build` — do not
  ship unbuilt source.  Confirm the build succeeds.
- Never hardcode secrets.  Use placeholder env vars and document them.
""",
)


DATA_ANALYSIS = Skill(
    name="data_analysis",
    keywords=(
        "data", "dataset", "csv", "parquet", "dataframe", "pandas", "polars",
        "analyze", "analysis", "statistics", "statistical", "aggregate",
        "groupby", "join", "sql", "query", "visualize", "plot", "chart",
        "histogram", "scatter", "correlation", "regression", "anova",
    ),
    guidance="""SKILL: Data Analysis

When the goal involves exploring, transforming, or summarising data:
- Load the data once, inspect shape / dtypes / nulls / ranges BEFORE any
  aggregation.  Log these as a think step.
- Prefer polars > pandas for anything >100k rows.  Use lazy frames where
  available.
- For stats claims, compute the actual statistic (mean, median, std, p-value,
  effect size) and print it.  Never describe a trend without a number.
- Save intermediate artefacts (cleaned_data.parquet, summary.csv) to the
  workspace so downstream steps can reuse them.
- For visualisations, write charts to /workspace/<user>/<name>.png and include
  the filename in the deliver step so the user can view them.
- Watch for: unit confusion, timezone handling, missing-value semantics,
  survivorship bias.
""",
)


BROWSER_AUTOMATION = Skill(
    name="browser_automation",
    keywords=(
        "browser", "playwright", "selenium", "scrape", "crawl", "extract",
        "navigate", "click", "form", "login", "website", "web page",
        "screenshot", "pdf", "headless", "url", "http", "html",
    ),
    guidance="""SKILL: Browser Automation

When the goal requires fetching content from a live website or driving a
browser:
- Prefer `browser_fetch` for simple GET-then-read scenarios.  Use the full
  Playwright tool only when the page depends on JavaScript rendering or
  requires interaction.
- Set explicit timeouts (20-45s for network-heavy pages).  Use
  wait_for="domcontentloaded" unless you need full load.
- Always save raw HTML / screenshots to the workspace as an artefact before
  attempting parsing — it lets you inspect failures.
- Parse with a real parser (BeautifulSoup, lxml, cheerio) — never regex HTML.
- Respect robots.txt and rate limits.  One request at a time unless the goal
  explicitly requires crawling.
- If a site blocks headless browsers, try a plain HTTP fetch with realistic
  headers first before escalating.
""",
)


DEVOPS = Skill(
    name="devops",
    keywords=(
        "deploy", "docker", "container", "kubernetes", "k8s", "compose",
        "nginx", "systemd", "ci", "cd", "pipeline", "terraform", "ansible",
        "server", "linux", "ssh", "bash", "shell", "cron", "monitor",
    ),
    guidance="""SKILL: DevOps / System Administration

When the goal involves deployment, container, or server operations:
- Start with a dry run.  Never run a destructive command (rm -rf, docker
  system prune, kubectl delete) without dumping the target state first.
- For Docker / compose files, validate with `docker compose config` before
  `up`.  Always add health checks and restart policies.
- For bash scripts, `set -euo pipefail` at the top.  Quote variables.
- Capture logs.  If a service fails to start, run `docker logs <name>` or
  `journalctl -u <name>` and include the tail in your analysis.
- Idempotency: assume the script will run twice.  Use `mkdir -p`, guard
  `docker run --rm` with `docker ps`, and prefer declarative tools.
- NEVER print secrets to stdout.  Redact tokens in any logs you emit.
""",
)


ML = Skill(
    name="ml",
    keywords=(
        "model", "machine learning", "ml", "deep learning", "neural",
        "train", "training", "fine-tune", "finetune", "inference", "predict",
        "classification", "regression", "embedding", "transformer", "pytorch",
        "tensorflow", "scikit", "sklearn", "cnn", "rnn", "lstm", "llm",
    ),
    guidance="""SKILL: Machine Learning / Modeling

When the goal involves training or evaluating a model:
- Split data into train/val/test BEFORE any feature engineering.  Never touch
  the test set during iteration.
- Report the full metric set: accuracy/F1/AUC for classification; MAE/RMSE/R^2
  for regression.  Include a trivial baseline (mean predictor, majority class)
  for context.
- Seed all RNGs (numpy, torch, random) and record the seed.
- For deep learning, print model parameter count, training loss per epoch, and
  validation metric per epoch.
- For inference/deployment, measure latency (p50/p95) on a realistic payload.
- Guard against data leakage: confirm no target column survived into features,
  no future data leaked into past, no duplicate rows straddle splits.
""",
)


CONTENT = Skill(
    name="content",
    keywords=(
        "write", "article", "blog", "post", "essay", "report", "document",
        "memo", "summary", "summarise", "summarize", "translate",
        "email", "letter", "draft", "copy", "caption", "tweet",
    ),
    guidance="""SKILL: Content & Writing

When the goal is producing written prose:
- Identify the audience, length target, and tone from the goal.  Ask the user
  for clarification only if these are genuinely ambiguous AND the task cannot
  proceed without them.
- Structure longer pieces with a clear opening hook, numbered or named
  sections, and a concrete takeaway/close.
- Cite every factual claim with a URL in markdown link form.  Never invent
  citations.  If a source can't be verified, flag it in-line with `[unsourced]`.
- Save the piece to a markdown file in the workspace and include the path in
  deliver.  Large deliverables may also produce a PDF via pandoc or a docx
  via python-docx.
""",
)


RESEARCH = Skill(
    name="research",
    keywords=(
        "research", "investigate", "find out", "compare", "evaluate",
        "survey", "review", "analyse", "analyze", "study", "report",
        "market", "competitor", "landscape", "trend", "whitepaper",
    ),
    guidance="""SKILL: Research & Investigation

When the goal is to gather information from external sources and synthesize:
- Plan the question decomposition in a think step: main question → sub-
  questions → sources that would answer each.
- Use web_search / browser_fetch for primary sources.  Prefer official
  documentation, SEC filings, arxiv, vendor blogs over aggregator summaries.
- Keep a sources.md file in the workspace where you log every URL visited with
  a 1-line takeaway.  This is your evidence trail.
- Resolve conflicting data by checking the most authoritative primary source.
  Never average two contradictory numbers.
- Deliverable should distinguish: established facts, estimates, opinions, and
  unknowns.  Label each section accordingly.
""",
)


FINANCE = Skill(
    name="finance",
    keywords=(
        "stock", "equity", "bond", "options", "futures", "crypto", "forex",
        "yield", "volatility", "sharpe", "alpha", "backtest", "portfolio",
        "hedge", "arbitrage", "var", "cvar", "risk", "financial", "earnings",
        "valuation", "dcf", "macro", "interest rate", "fed", "inflation",
    ),
    guidance="""SKILL: Finance / Quantitative Research

When the goal involves markets, portfolios, trading, or financial modelling:
- Be explicit about the data source, frequency, and lookback window.  Never
  compute a return without specifying the denominator (t-1 close, prior day,
  etc.).
- For backtests: declare the universe, rebalance frequency, transaction cost
  model, position sizing rule, and out-of-sample split up front.  Report
  Sharpe, Sortino, max drawdown, calmar, hit rate, average win/loss.
- Avoid lookahead bias — features at time t must only use data available at
  time t (or earlier).
- For risk metrics, state the confidence level (e.g. 95% 1-day VaR) and the
  method (historical / parametric / Monte Carlo).
- Never recommend a specific security or position size as investment advice;
  frame results as analysis of historical data.
""",
)


SECURITY = Skill(
    name="security",
    keywords=(
        "security", "vulnerability", "exploit", "sandbox", "sanitize",
        "injection", "xss", "csrf", "ssrf", "privilege", "authz", "authn",
        "authenticate", "authorize", "encryption", "tls", "ssl", "hash",
        "password", "token", "jwt", "secret", "audit",
    ),
    guidance="""SKILL: Security Engineering

When the goal touches on auth, data handling, or exposed endpoints:
- Threat-model first.  Write out: who the attacker is, what they can do, what
  they want to steal.  Then design defences for each.
- Never trust user input.  Validate type, length, and allowed set before use.
  Parameterise every SQL query.  Sanitise every filesystem path (no ../, no
  absolute paths, no symlinks pointing outside the sandbox).
- Use constant-time comparison for secrets / tokens / MAC values.
- Prefer well-audited libraries over bespoke crypto.  Never implement your own
  primitive.
- Log auth events (success + failure) with enough context to audit, but never
  log the secret itself.
- When reviewing code, flag every place where a privilege boundary is crossed
  and every place user input reaches a sink (exec, eval, SQL, file open, shell).
""",
)


ALL_SKILLS: tuple[Skill, ...] = (
    CODING, WEB_DEV, DATA_ANALYSIS, BROWSER_AUTOMATION, DEVOPS, ML,
    CONTENT, RESEARCH, FINANCE, SECURITY,
)


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]*")


def _tokenise(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text or "")}


def score_skill(skill: Skill, goal_tokens: set[str], goal_text: str) -> int:
    """Score = single-word keyword hits + 2*multi-word keyword hits."""
    s = 0
    lower = goal_text.lower()
    for kw in skill.keywords:
        if " " in kw or "." in kw or "-" in kw or "+" in kw:
            if kw in lower:
                s += 2
        else:
            if kw in goal_tokens:
                s += 1
    return s


def select_skills(goal: str, user_instructions: str | None = None, top_k: int = 2) -> list[Skill]:
    """Return the top-k matching skills for a goal.  Empty list if nothing matches."""
    text = (goal or "") + "\n" + (user_instructions or "")
    tokens = _tokenise(text)
    scored = [(score_skill(s, tokens, text), s) for s in ALL_SKILLS]
    scored = [(sc, sk) for sc, sk in scored if sc > 0]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [sk for _, sk in scored[:top_k]]


def render_skill_block(skills: list[Skill]) -> str:
    """Render a skills block ready to append to a system prompt."""
    if not skills:
        return ""
    parts = ["", "---", "DOMAIN SKILLS (apply these to the plan):"]
    for s in skills:
        parts.append("")
        parts.append(s.guidance.strip())
    return "\n".join(parts) + "\n"
