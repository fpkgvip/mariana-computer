"""
mariana/ai/prompt_builder.py

Builds the message list sent to every LLM call.

Architecture — three-block prompt structure
-------------------------------------------
Block 1  (static system instructions, ~8 K tokens)
   Comprehensive financial investigative analyst identity + frameworks.
   This block is IDENTICAL across all task types, so it maximises prompt-cache
   hits on Anthropic (cached after first use, billed at 0.10× input price).
   Stored in-module as a string constant (no disk I/O, loaded once at import).

Block 2  (task-specific framework, cached)
   Loaded from  mariana/ai/prompts/{task_type.value.lower()}.txt  at first use
   and stored in a module-level dict.  Falls back to an empty string if the
   file does not exist, so the system degrades gracefully during early
   development when prompt files may not yet be written.

Block 3  (dynamic context, NOT cached)
   Built from the caller-supplied ``context`` dict.  Task-type-specific
   rendering functions translate the dict into a structured natural-language
   section that the model can act on.

Cache-control markers
---------------------
For Claude (Anthropic) models the message payload uses the content-block
format with ``cache_control: {"type": "ephemeral"}`` placed at the end of
Block 1 and Block 2 so the static prefix is eligible for caching.

For OpenAI and DeepSeek models the messages are sent in the standard
``{"role": ..., "content": "..."}`` format because those providers use
automatic (not explicit) caching.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mariana.data.models import ModelID, TaskType
from mariana.config import AppConfig

logger = logging.getLogger(__name__)

# ─── Paths ───────────────────────────────────────────────────────────────────

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# ─── Static system prompt — Block 1 ─────────────────────────────────────────

STATIC_SYSTEM_PROMPT: str = """\
You are Mariana, an elite financial investigative analyst with deep expertise \
in global capital markets, forensic accounting, and investigative research. \
You operate with the rigour of a court-grade expert witness and the creativity \
of a Pulitzer-winning investigative journalist. Every response you produce is \
a structured, evidence-grounded JSON object — nothing else.

═══════════════════════════════════════════════════════════════════════════════
IDENTITY & MISSION
═══════════════════════════════════════════════════════════════════════════════

Your mission is to uncover material financial facts, patterns of fraud or \
misrepresentation, hidden corporate structures, and regulatory violations that \
affect investors, regulators, or the public interest. You pursue every lead \
systematically, weigh evidence objectively, and always communicate the \
strength of your conclusions in proportion to the quality of the underlying \
evidence.

You are not a summariser. You are an investigator. The difference: a \
summariser regurgitates what sources say; you cross-reference, challenge, \
compute, and connect.

═══════════════════════════════════════════════════════════════════════════════
CAPITAL MARKETS EXPERTISE
═══════════════════════════════════════════════════════════════════════════════

Chinese markets
  • A-shares (Shanghai SSE, Shenzhen SZSE), B-shares, H-shares (HKEX Main \
Board and GEM), S-chips (Singapore), ADRs (US-listed Chinese companies)
  • NEEQ (New Third Board): Tier 1 Select Layer, Tier 2 Innovation Layer, \
Tier 3 Base Layer — liquidity, reporting, and trading-suspension rules differ \
materially
  • STAR Market (科创板): market-based IPO pricing, 20 % daily limit, \
mandatory delisting triggers
  • SZSE ChiNext (创业板): high-growth mandate, retail-dominated, elevated \
pump-and-dump risk
  • VIE structures: contractual arrangements, WFOE, operating company; \
key risk is unenforceability under PRC law
  • China Depositary Receipts (CDR): repatriation rules, underwriter lock-ups
  • State-owned enterprise (SOE) vs. private enterprise (POE) governance \
distinctions — SOE boards are largely ceremonial, CPC committee is real control

International markets
  • US: NYSE, NASDAQ, OTC Markets; SEC full-disclosure regime; Rule 10b-5 \
fraud standard; Reg FD; short-seller report conventions
  • HK: SFC oversight; Listing Rules Main Board vs. GEM; H-share vs. red-chip \
vs. P-chip classification; dual-class share regime post-2018
  • Singapore: SGX Mainboard vs. Catalist; mandatory continuous disclosure
  • European exchanges: MiFID II transparency requirements
  • Offshore jurisdictions: Cayman Islands (BVI, CI) SPV mechanics, nominee \
director databases, UBO disclosure gaps

═══════════════════════════════════════════════════════════════════════════════
REGULATORY FRAMEWORK KNOWLEDGE
═══════════════════════════════════════════════════════════════════════════════

China regulators
  • CSRC (中国证券监督管理委员会): enforcement powers, disclosure rules, \
suspension and delisting triggers, administrative penalties
  • PBOC (中国人民银行): monetary policy, FX controls (SAFE), anti-money \
laundering, structured product approval
  • CBIRC (中国银行保险监督管理委员会): bank capital adequacy, insurance \
product rules, shadow banking perimeter
  • SAMR (国家市场监督管理总局): anti-monopoly merger review, competition \
enforcement — especially relevant for Big Tech and healthcare
  • MOFCOM: foreign investment screening (NDRC negative list)
  • SAFE: cross-border capital flow rules, ODI/FDI approval thresholds
  • National Security Review: Cybersecurity Law, Data Security Law, PIPL \
implications for data-intensive companies

US regulators
  • SEC: 10-K/10-Q/8-K/20-F annual report requirements; Form 4 insider \
trading; Schedule 13D/G beneficial ownership; Reg S-K and S-X disclosure \
standards; PCAOB audit inspection regime
  • DOJ / FBI: FCPA enforcement; securities fraud prosecution thresholds
  • FINRA: broker-dealer oversight; TAF/FINRA cross-market surveillance
  • CFTC: derivatives, commodities, crypto (where applicable)
  • FinCEN: BSA / AML, beneficial ownership registry (CTA 2024)
  • OFAC: sanctions screening — SDN list, sector-based sanctions (SSI)

HK regulators
  • SFC: Type 1–9 licence requirements; listing suitability assessment; \
SFC enforcement actions (cold shoulder orders, market misconduct tribunal)
  • HKEX Listing Rules: continuing obligations, notifiable transactions, \
connected transaction requirements
  • FSTB / HKMA: banking, virtual assets (VASP licensing regime, June 2023)

═══════════════════════════════════════════════════════════════════════════════
ACCOUNTING STANDARDS
═══════════════════════════════════════════════════════════════════════════════

You compare across three accounting regimes and flag material differences:

GAAP (US)
  Revenue: ASC 606 five-step model — performance obligations, stand-alone \
selling price allocations
  Leases: ASC 842 right-of-use assets — off-balance-sheet finance lease \
structuring red flag
  Business combinations: ASC 805 purchase price allocation, goodwill \
impairment (qualitative then quantitative), bargain purchases
  Stock-based compensation: ASC 718 grant-date fair value; cliff vs. graded \
vesting; modification accounting

IFRS (International)
  Revenue: IFRS 15 — substantially converged with ASC 606; key difference \
in variable consideration constraint
  Leases: IFRS 16 — virtually all leases on-balance sheet (no operating-lease \
exemption above 12 months); compare leverage ratios carefully
  Goodwill: IAS 36 — impairment-only, no amortisation (vs. private company \
GAAP alternative of amortisation)
  Financial instruments: IFRS 9 ECL model — requires forward-looking provision \
unlike US incurred-loss model; watch for under-provisioning in credit books

CAS (Chinese Accounting Standards)
  Substantially converged with IFRS as of 2022 but key divergences remain:
  — Related-party definition is broader; watch for connected transaction \
carve-outs hidden in notes
  — Revenue from construction contracts (percentage-of-completion) is \
frequently the site of Chinese fraud
  — Impairment of long-lived assets: less rigorous impairment testing in \
practice than IFRS/GAAP; goodwill impairment is a common earnings management \
tool
  — Profit distributions from subsidiaries: PRC withholding tax of 5-10 % on \
dividends to foreign parents — cash trapped in the PRC is a recurrent \
forensic signal

═══════════════════════════════════════════════════════════════════════════════
FRAUD PATTERN LIBRARY & FORENSIC ACCOUNTING
═══════════════════════════════════════════════════════════════════════════════

Revenue fraud
  — Round-trip transactions through affiliates (circular cash flows)
  — Bill-and-hold schemes (physical control not transferred)
  — Channel stuffing: inventory builds at distributors; A/R DSO expansion
  — Fictitious customers: cross-check via SAIC / SAMR business registry \
(Qichacha, Tianyancha) to verify customer existence and ownership
  — Undisclosed related-party sales: match counterparty tax IDs, \
registered addresses, and ultimate beneficial owners

Cost/expense fraud
  — Capitalising operating expenses (inflates assets, suppresses expenses)
  — Depreciation manipulation: extended useful lives, late impairment
  — Undisclosed RPT: management fees, IP royalties, loans to controlling \
shareholders; watch for "other payables" spikes

Cash and banking fraud
  — Pledged cash disclosed only in note footnotes (frozen accounts)
  — Fake bank statements: verify via PBOC credit reference system \
(企业征信) or direct bank confirmation requests
  — The "cash disappearance" pattern: high reported cash + inability to \
pay dividends + high short-term borrowings

Auditor and governance red flags
  — Small, local or unknown auditor for a large company
  — Frequent auditor changes (especially within 12 months before fraud \
disclosure)
  — CFO/controller departures immediately post-audit-signing
  — Non-standard audit opinions: emphasis of matter, going concern
  — Board / audit committee dominated by insiders or nominees of the \
controlling shareholder

═══════════════════════════════════════════════════════════════════════════════
DATA SOURCE RELIABILITY HIERARCHY
═══════════════════════════════════════════════════════════════════════════════

Tier 0 — Primary (highest reliability, cite verbatim):
  CSRC disclosure system (巨潮资讯, cninfo.com.cn)
  SSE/SZSE official filings portals
  HKEX news/results filings
  SEC EDGAR (10-K, 20-F, 8-K, Form 4, Proxy)
  PBOC / SAFE official publications
  NBS (National Bureau of Statistics) statistical releases
  OFAC SDN/SSI lists (sanctions)
  Court records (PACER for US federal courts)

Tier 1 — Official secondary:
  Company annual/interim reports (IR website PDFs)
  Exchange announcements (investor relations pages)
  Regulator enforcement orders

Tier 2 — Verified commercial data:
  Bloomberg, Refinitiv/LSEG, FactSet (subscription terminal data)
  Polygon.io, Unusual Whales (market microstructure)
  WIND (万得, Chinese terminal)
  PitchBook, Preqin (private markets)

Tier 3 — Cross-checked media:
  Caixin, 财新: investigative financial journalism (high reliability)
  South China Morning Post, Reuters, Bloomberg News (copy)
  Seeking Alpha, 雪球 (Xueqiu): analyst commentary — treat as opinion

Tier 4 — Use with caution:
  Anonymous short-seller reports: flag reasoning and verify every claim
  Social media, forums, chat rooms: sentiment signal only
  Undated PDF reports from unknown firms

When citing a source, always state the tier and note any caveats.

═══════════════════════════════════════════════════════════════════════════════
FINANCIAL ANOMALY DETECTION FRAMEWORK
═══════════════════════════════════════════════════════════════════════════════

Apply these analytical lenses to every set of financial data you encounter:

1. Benford's Law — first-digit distribution of financial figures; material \
deviation (p < 0.05 chi-square) is a red flag for fabricated numbers.

2. Days metrics — monitor for unsustainable trends:
   DSO (days sales outstanding) > 90 or rapidly rising
   DIO (days inventory outstanding) > 180 or rising faster than revenue
   DPO (days payable outstanding) > 120 (possible cash hoarding)

3. Cash conversion cycle — if CCC deteriorates while earnings improve, \
revenue may be fictitious.

4. Accruals ratio — (net income − operating cash flow) / average net assets; \
values > 0.10 indicate aggressive accrual accounting.

5. Free cash flow vs. reported earnings — persistent FCF < reported net income \
by > 20 % over 3+ years is a structural red flag.

6. Related-party transaction ratio — RPT revenue / total revenue; \
≥ 30 % triggers automatic enhanced scrutiny.

7. Equity-to-cash ratio — cash as % of total assets; > 60 % with inability \
to pay dividends and high short-term debt is the PRC "cash trap" pattern.

8. Goodwill / total assets — > 40 % for a non-platform business is elevated; \
check for impairment lag.

9. Insider ownership drift — rapid dilution or buyback without stated purpose \
can signal compensation extraction or float manipulation.

10. Short interest and options flow — elevated short interest + out-of-money \
put buying ahead of a negative announcement warrants investigation of \
information leakage.

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT REQUIREMENTS
═══════════════════════════════════════════════════════════════════════════════

CRITICAL: You MUST always respond with a single, valid JSON object.

  • No prose before or after the JSON.
  • No markdown formatting around the JSON — output the raw object only.
  • All string values must be properly escaped.
  • Numbers must be JSON numbers (not strings).
  • Boolean must be true/false (not "yes"/"no").
  • Null fields must be JSON null (not the string "null" or empty string).
  • Dates must be ISO-8601 strings: "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SSZ".
  • Monetary amounts must be in the specified currency without currency symbols.
  • When confidence is requested, express as a float 0.0–1.0, not a percentage.
  • When a score 0–10 is requested, use one decimal place: e.g. 7.4, not 7.

Failure to return valid JSON will cause a system error and waste research \
budget. Prioritise correctness of structure over completeness of content.
"""

# ─── In-memory prompt file cache — Block 2 ───────────────────────────────────

# Maps task_type.value.lower() → file content (or empty string if not found)
_TASK_FRAMEWORK_CACHE: dict[str, str] = {}
_FRAMEWORK_CACHE_LOADED: bool = False


def _load_task_frameworks() -> None:
    """
    Load all task-framework .txt files from the prompts directory into
    ``_TASK_FRAMEWORK_CACHE``.  Called once at first use.

    Missing files produce an empty string entry so the system degrades
    gracefully.
    """
    global _FRAMEWORK_CACHE_LOADED

    for task_type in TaskType:
        key = task_type.value.lower()
        prompt_file = _PROMPTS_DIR / f"{key}.txt"
        if prompt_file.exists():
            try:
                _TASK_FRAMEWORK_CACHE[key] = prompt_file.read_text(encoding="utf-8").strip()
                logger.debug("Loaded task framework prompt: %s", prompt_file)
            except OSError as exc:
                logger.warning("Failed to read prompt file %s: %s", prompt_file, exc)
                _TASK_FRAMEWORK_CACHE[key] = ""
        else:
            logger.debug("No task framework file for %s (expected %s)", key, prompt_file)
            _TASK_FRAMEWORK_CACHE[key] = ""

    _FRAMEWORK_CACHE_LOADED = True


def _get_task_framework(task_type: TaskType) -> str:
    """Return the cached task-specific framework text for *task_type*."""
    if not _FRAMEWORK_CACHE_LOADED:
        _load_task_frameworks()
    return _TASK_FRAMEWORK_CACHE.get(task_type.value.lower(), "")


# ─── Dynamic context builders — Block 3 ──────────────────────────────────────

def _require(context: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Warn (do not raise) on missing keys; return context for chaining."""
    for k in keys:
        if k not in context or context[k] is None:
            logger.warning("build_messages: expected context key '%s' is missing or None.", k)
    return context


def _schema_json(output_schema: type[BaseModel]) -> str:
    """Return a compact, human-readable JSON schema for *output_schema*."""
    try:
        return json.dumps(output_schema.model_json_schema(), indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Could not serialise JSON schema for %s: %s", output_schema.__name__, exc)
        return "{}"


def _build_dynamic_context(task_type: TaskType, context: dict[str, Any]) -> str:
    """
    Route to the appropriate context-builder for *task_type*.

    Each builder extracts the keys documented in the task specification table
    and formats them as a clear, structured block of instructions + data.
    """
    builders: dict[TaskType, Any] = {
        TaskType.HYPOTHESIS_GENERATION: _ctx_hypothesis_generation,
        TaskType.EVIDENCE_EXTRACTION: _ctx_evidence_extraction,
        TaskType.EVALUATION: _ctx_evaluation,
        TaskType.TRANSLATION: _ctx_translation,
        TaskType.SUMMARIZATION: _ctx_summarization,
        TaskType.COMPRESSION: _ctx_compression,
        TaskType.TRIBUNAL_PLAINTIFF: _ctx_tribunal_plaintiff,
        TaskType.TRIBUNAL_DEFENDANT: _ctx_tribunal_defendant,
        TaskType.TRIBUNAL_REBUTTAL: _ctx_tribunal_rebuttal,
        TaskType.TRIBUNAL_COUNTER: _ctx_tribunal_counter,
        TaskType.TRIBUNAL_JUDGE: _ctx_tribunal_judge,
        TaskType.SKEPTIC_QUESTIONS: _ctx_skeptic_questions,
        TaskType.REPORT_DRAFT: _ctx_report_draft,
        TaskType.REPORT_FINAL_EDIT: _ctx_report_final_edit,
        TaskType.WATCHDOG: _ctx_watchdog,
    }
    builder = builders.get(task_type)
    if builder is None:
        logger.error("No dynamic context builder for task_type=%s", task_type.value)
        return f"TASK: {task_type.value}\nCONTEXT:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
    return builder(context)


# ── Individual context builders ───────────────────────────────────────────────

def _ctx_hypothesis_generation(ctx: dict[str, Any]) -> str:
    _require(ctx, "topic", "budget_usd")
    pivot = ctx.get("pivot_context", "")
    pivot_block = f"\nPIVOT CONTEXT (from prior research cycle):\n{pivot}" if pivot else ""
    return (
        f"TASK: HYPOTHESIS_GENERATION\n\n"
        f"Research topic:\n{ctx.get('topic', '[missing]')}\n\n"
        f"Available budget: USD {ctx.get('budget_usd', 0):.2f}"
        f"{pivot_block}\n\n"
        "Generate a ranked set of investigative hypotheses about this topic. "
        "Each hypothesis must be falsifiable, specific, and prioritised by "
        "potential impact. Consider both confirming and disconfirming evidence. "
        "Assign search query suggestions for each hypothesis."
    )


def _ctx_evidence_extraction(ctx: dict[str, Any]) -> str:
    _require(ctx, "hypothesis_statement", "page_content", "source_url")
    momentum = ctx.get("momentum_note", "")
    momentum_block = f"\nMOMENTUM NOTE (prior cycle insight):\n{momentum}" if momentum else ""
    title = ctx.get("page_title", "")
    title_block = f"\nPage title: {title}" if title else ""
    return (
        f"TASK: EVIDENCE_EXTRACTION\n\n"
        f"Hypothesis under investigation:\n{ctx.get('hypothesis_statement', '[missing]')}\n\n"
        f"Source URL: {ctx.get('source_url', '[missing]')}"
        f"{title_block}"
        f"{momentum_block}\n\n"
        "Page content to analyse:\n"
        "---\n"
        f"{ctx.get('page_content', '[missing]')}\n"
        "---\n\n"
        "Extract all evidence relevant to the hypothesis. For each item, "
        "classify as FOR / AGAINST / NEUTRAL, assign confidence 0.0–1.0, "
        "and quote the exact supporting passage from the page content."
    )


def _ctx_evaluation(ctx: dict[str, Any]) -> str:
    _require(ctx, "hypothesis_statement", "compressed_findings", "sources_searched")
    momentum = ctx.get("momentum_note", "")
    budget = ctx.get("budget_remaining")
    extras = ""
    if momentum:
        extras += f"\nMOMENTUM NOTE:\n{momentum}"
    if budget is not None:
        extras += f"\nRemaining research budget: USD {budget:.2f}"
    return (
        f"TASK: EVALUATION\n\n"
        f"Hypothesis:\n{ctx.get('hypothesis_statement', '[missing]')}\n\n"
        f"Sources searched so far: {ctx.get('sources_searched', 0)}\n"
        f"{extras}\n\n"
        "Compressed findings:\n"
        "---\n"
        f"{ctx.get('compressed_findings', '[missing]')}\n"
        "---\n\n"
        "Evaluate the overall credibility of the hypothesis on a 0–10 scale. "
        "Score each piece of evidence by quality tier (Tier 0–4) and weight "
        "its contribution. Provide a new momentum_note (100–200 tokens) "
        "capturing the most important insight to carry forward."
    )


def _ctx_translation(ctx: dict[str, Any]) -> str:
    _require(ctx, "text", "source_language", "target_language")
    domain = ctx.get("domain_context", "")
    domain_block = f"\nDomain context: {domain}" if domain else ""
    return (
        f"TASK: TRANSLATION\n\n"
        f"Source language: {ctx.get('source_language', '[missing]')}\n"
        f"Target language: {ctx.get('target_language', '[missing]')}"
        f"{domain_block}\n\n"
        "Text to translate:\n"
        "---\n"
        f"{ctx.get('text', '[missing]')}\n"
        "---\n\n"
        "Produce a faithful, domain-accurate translation. Preserve "
        "financial/legal terminology, company names, regulatory body names, "
        "and numeric formats. Do not paraphrase or add commentary."
    )


def _ctx_summarization(ctx: dict[str, Any]) -> str:
    _require(ctx, "hypothesis_statement", "findings")
    return (
        f"TASK: SUMMARIZATION\n\n"
        f"Hypothesis:\n{ctx.get('hypothesis_statement', '[missing]')}\n\n"
        "Findings to summarise:\n"
        "---\n"
        f"{ctx.get('findings', '[missing]')}\n"
        "---\n\n"
        "Produce a concise, evidence-grounded summary of the findings as they "
        "relate to the hypothesis. Retain all quantitative data points, source "
        "attribution, and confidence scores. Eliminate redundancy."
    )


def _ctx_compression(ctx: dict[str, Any]) -> str:
    _require(ctx, "hypothesis_id", "hypothesis_statement", "all_findings")
    prior = ctx.get("prior_compression", "")
    prior_block = (
        f"\nPrior compressed state (update/replace):\n{prior}" if prior else ""
    )
    return (
        f"TASK: COMPRESSION\n\n"
        f"Hypothesis ID: {ctx.get('hypothesis_id', '[missing]')}\n"
        f"Hypothesis:\n{ctx.get('hypothesis_statement', '[missing]')}"
        f"{prior_block}\n\n"
        "All findings to compress:\n"
        "---\n"
        f"{ctx.get('all_findings', '[missing]')}\n"
        "---\n\n"
        "Compress all findings into a compact representation that preserves "
        "every material fact, quantitative data point, and source reference. "
        "The compressed output will replace the raw findings in the working "
        "context to fit within the token budget."
    )


def _ctx_tribunal_plaintiff(ctx: dict[str, Any]) -> str:
    _require(ctx, "finding_summary", "supporting_evidence", "sources")
    return (
        f"TASK: TRIBUNAL_PLAINTIFF\n\n"
        "You are the PLAINTIFF in an adversarial tribunal. Your role is to "
        "construct the strongest possible argument that the finding is TRUE "
        "and MATERIAL. You must cite specific evidence and sources.\n\n"
        f"Finding summary:\n{ctx.get('finding_summary', '[missing]')}\n\n"
        f"Supporting evidence:\n{ctx.get('supporting_evidence', '[missing]')}\n\n"
        f"Sources:\n{ctx.get('sources', '[missing]')}\n\n"
        "Build the plaintiff opening argument. Quantify claims wherever "
        "possible. Identify the three strongest pieces of evidence and explain "
        "why they are credible."
    )


def _ctx_tribunal_defendant(ctx: dict[str, Any]) -> str:
    _require(ctx, "finding_summary", "plaintiff_argument")
    return (
        f"TASK: TRIBUNAL_DEFENDANT\n\n"
        "You are the DEFENDANT in an adversarial tribunal. Your role is to "
        "construct the strongest possible counter-argument: challenge "
        "evidence quality, offer alternative explanations, and identify "
        "methodological flaws in the plaintiff's case.\n\n"
        f"Finding summary:\n{ctx.get('finding_summary', '[missing]')}\n\n"
        f"Plaintiff's argument:\n{ctx.get('plaintiff_argument', '[missing]')}\n\n"
        "Build the defendant rebuttal. Steel-man the alternative explanations. "
        "Highlight any data gaps, source reliability concerns, or logical leaps."
    )


def _ctx_tribunal_rebuttal(ctx: dict[str, Any]) -> str:
    _require(ctx, "finding_summary", "defendant_argument", "plaintiff_original")
    return (
        f"TASK: TRIBUNAL_REBUTTAL\n\n"
        "You are the PLAINTIFF responding to the defendant's counter-argument.\n\n"
        f"Finding summary:\n{ctx.get('finding_summary', '[missing]')}\n\n"
        f"Your original argument:\n{ctx.get('plaintiff_original', '[missing]')}\n\n"
        f"Defendant's argument:\n{ctx.get('defendant_argument', '[missing]')}\n\n"
        "Write a focused rebuttal. Address each of the defendant's points "
        "directly. Introduce any additional evidence that strengthens the "
        "original finding. Do not repeat arguments already made."
    )


def _ctx_tribunal_counter(ctx: dict[str, Any]) -> str:
    _require(ctx, "finding_summary", "plaintiff_rebuttal", "defendant_original")
    return (
        f"TASK: TRIBUNAL_COUNTER\n\n"
        "You are the DEFENDANT delivering a final counter-argument.\n\n"
        f"Finding summary:\n{ctx.get('finding_summary', '[missing]')}\n\n"
        f"Your original argument:\n{ctx.get('defendant_original', '[missing]')}\n\n"
        f"Plaintiff's rebuttal:\n{ctx.get('plaintiff_rebuttal', '[missing]')}\n\n"
        "Deliver your counter-argument to the rebuttal. Concede points that "
        "are genuinely well-supported. Maintain challenges that remain valid. "
        "This is your final statement — be precise."
    )


def _ctx_tribunal_judge(ctx: dict[str, Any]) -> str:
    _require(
        ctx,
        "finding_summary",
        "plaintiff_summary",
        "defendant_summary",
        "plaintiff_rebuttal_summary",
        "defendant_counter_summary",
    )
    return (
        f"TASK: TRIBUNAL_JUDGE\n\n"
        "You are the JUDGE in this adversarial tribunal. Review all arguments "
        "and render a verdict.\n\n"
        f"Finding under review:\n{ctx.get('finding_summary', '[missing]')}\n\n"
        f"Plaintiff opening:\n{ctx.get('plaintiff_summary', '[missing]')}\n\n"
        f"Defendant opening:\n{ctx.get('defendant_summary', '[missing]')}\n\n"
        f"Plaintiff rebuttal:\n{ctx.get('plaintiff_rebuttal_summary', '[missing]')}\n\n"
        f"Defendant counter:\n{ctx.get('defendant_counter_summary', '[missing]')}\n\n"
        "Render a verdict: CONFIRMED, WEAKENED, or DESTROYED. Provide your "
        "reasoning, adjusted confidence score (0.0–1.0), and identify any "
        "unanswered questions the adversarial process surfaced."
    )


def _ctx_skeptic_questions(ctx: dict[str, Any]) -> str:
    _require(ctx, "finding_summary", "confidence_score", "tribunal_verdict")
    unanswered = ctx.get("unanswered_questions", "")
    unanswered_block = (
        f"\nUnanswered questions from tribunal:\n{unanswered}" if unanswered else ""
    )
    return (
        f"TASK: SKEPTIC_QUESTIONS\n\n"
        "You are a sceptical expert reviewer stress-testing a research finding "
        "before it is published.\n\n"
        f"Finding summary:\n{ctx.get('finding_summary', '[missing]')}\n\n"
        f"Current confidence score: {ctx.get('confidence_score', '[missing]')}\n"
        f"Tribunal verdict: {ctx.get('tribunal_verdict', '[missing]')}"
        f"{unanswered_block}\n\n"
        "Generate the most important unresolved questions that could undermine "
        "this finding. For each question: classify as RESOLVED / RESEARCHABLE "
        "/ OPEN; assign severity (CRITICAL / MAJOR / MINOR); and assign a "
        "category (DATA_PROVENANCE, ALTERNATIVE_EXPLANATION, METHODOLOGY, "
        "LEGAL_EXPOSURE, TEMPORAL_VALIDITY)."
    )


def _ctx_report_draft(ctx: dict[str, Any]) -> str:
    _require(ctx, "confirmed_findings", "all_sources", "task_topic")
    failed = ctx.get("failed_hypotheses", "")
    failed_block = (
        f"\nFailed/discarded hypotheses (for context, do not include in report):\n{failed}"
        if failed else ""
    )
    return (
        f"TASK: REPORT_DRAFT\n\n"
        f"Research topic:\n{ctx.get('task_topic', '[missing]')}\n\n"
        f"All sources used:\n{ctx.get('all_sources', '[missing]')}"
        f"{failed_block}\n\n"
        "Confirmed findings:\n"
        "---\n"
        f"{ctx.get('confirmed_findings', '[missing]')}\n"
        "---\n\n"
        "Draft a comprehensive investigative report in the structured format "
        "required. Include: Executive Summary, Key Findings (with confidence "
        "scores and source citations), Methodology, Limitations and Caveats, "
        "and Appendix of primary sources. All claims must have source attribution."
    )


def _ctx_report_final_edit(ctx: dict[str, Any]) -> str:
    _require(ctx, "draft", "all_sources")
    return (
        f"TASK: REPORT_FINAL_EDIT\n\n"
        "You are performing a final editorial and analytical review of a draft "
        "investigative report.\n\n"
        f"All sources:\n{ctx.get('all_sources', '[missing]')}\n\n"
        "Draft report:\n"
        "---\n"
        f"{ctx.get('draft', '[missing]')}\n"
        "---\n\n"
        "Improve clarity, tighten arguments, ensure all claims have source "
        "attribution, flag and correct any logical inconsistencies, and polish "
        "the language. Do NOT introduce new claims not supported by the sources "
        "list. Return the complete, edited report."
    )


def _ctx_watchdog(ctx: dict[str, Any]) -> str:
    _require(ctx, "recent_action_summaries")
    branch_id = ctx.get("current_branch_id", "")
    branch_block = f"\nCurrent branch ID: {branch_id}" if branch_id else ""
    return (
        f"TASK: WATCHDOG\n\n"
        "You are a meta-supervisor monitoring a multi-step research process "
        "for signs of circular reasoning, repetitive actions, or diminishing "
        "returns.\n\n"
        f"Recent action summaries:"
        f"{branch_block}\n"
        "---\n"
        f"{ctx.get('recent_action_summaries', '[missing]')}\n"
        "---\n\n"
        "Identify any problematic patterns: circular searches, repeated "
        "hypotheses, stalled progress, or budget waste. Return a verdict "
        "and a recommended action (CONTINUE / SEARCH_DIFFERENT_SOURCES / "
        "PIVOT / HALT) with rationale."
    )


# ─── Cache-control helpers ────────────────────────────────────────────────────

def _is_claude_model(model_id: ModelID) -> bool:
    return model_id.value.startswith("claude-")


# ─── Public API ───────────────────────────────────────────────────────────────

def build_messages(
    task_type: TaskType,
    context: dict[str, Any],
    output_schema: type[BaseModel],
    config: AppConfig,
    model_id: ModelID | None = None,
) -> list[dict[str, Any]]:
    """
    Build the complete message list for an AI call.

    The returned list is in the OpenAI-compatible format:
    ``[{"role": "...", "content": ...}, ...]``

    For Claude models, ``content`` is a list of content blocks with
    ``cache_control`` markers on the two static blocks so they are eligible
    for Anthropic prompt caching.

    For non-Claude models, ``content`` is a plain string (no cache_control),
    because OpenAI and DeepSeek use automatic (implicit) caching.

    Args:
        task_type: Determines which dynamic context builder is used.
        context: Task-specific data dict.  Keys are documented in the module
            docstring table.
        output_schema: Pydantic ``BaseModel`` the model must produce.  Its
            JSON schema is appended to the dynamic context block.
        config: Application configuration (used for any future config-driven
            prompt adjustments).
        model_id: Optional model ID; controls whether cache_control markers
            are emitted.  Defaults to detecting from config if None.

    Returns:
        List of message dicts ready for the ``messages`` parameter of any
        OpenAI-compatible API call.
    """
    # ── Resolve whether to use cache_control format ───────────────────────────
    use_cache_control = model_id is not None and _is_claude_model(model_id)

    # ── Build the three blocks ────────────────────────────────────────────────
    block1_text = STATIC_SYSTEM_PROMPT.strip()
    block2_text = _get_task_framework(task_type).strip()
    block3_text = (
        _build_dynamic_context(task_type, context)
        + "\n\n"
        + "OUTPUT SCHEMA (your response must conform to this JSON schema):\n"
        + _schema_json(output_schema)
        + "\n\nRespond with a single valid JSON object matching the schema above. "
          "No other text."
    )

    if use_cache_control:
        # Anthropic multi-block content format with explicit cache_control.
        content_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": block1_text,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        if block2_text:
            content_blocks.append(
                {
                    "type": "text",
                    "text": block2_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        content_blocks.append(
            {
                "type": "text",
                "text": block3_text,
                # No cache_control — dynamic block must not be cached.
            }
        )
        return [{"role": "user", "content": content_blocks}]

    else:
        # Standard OpenAI / DeepSeek format: system + user messages.
        system_content = block1_text
        if block2_text:
            system_content += "\n\n" + block2_text

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": block3_text},
        ]
