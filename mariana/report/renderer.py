"""
mariana/report/renderer.py

PDF rendering via Jinja2 template + WeasyPrint.

Design decisions:
  • The template is loaded from a configurable directory so the caller can
    override the default without patching this module.
  • Jinja2's ``FileSystemLoader`` is used (not ``PackageLoader``) so the
    template directory can be located outside a Python package, e.g. a Docker
    volume.
  • WeasyPrint is imported lazily inside ``render_pdf`` to allow the module to
    be imported in environments where WeasyPrint's native library dependencies
    are not installed — only the actual render call will fail.
  • Any Jinja2 or WeasyPrint error is converted to a ``ReportRenderError`` with
    a descriptive message so callers can log it cleanly.

Template expectations (variables that MUST be present in report_data):
  title_en, title_zh, executive_summary_en, executive_summary_zh,
  sections (list), conclusion_en, conclusion_zh, disclaimer_en, disclaimer_zh,
  generated_at (datetime), task_topic, total_cost_usd, total_sources,
  total_findings.

Optional but rendered when present:
  sections[*].word_count_en
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Default template filename expected inside template_dir.
_DEFAULT_TEMPLATE_NAME: str = "report.html.j2"

# H-07 fix: anchor any caller-supplied template_dir to a trusted base so a
# caller can't escape via ``..`` or a symlink and load arbitrary Jinja2
# templates from the filesystem.  We use the package root (parent of the
# ``report`` package) as the project root; this matches the bundled
# templates directory layout.
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReportRenderError(Exception):
    """Raised when Jinja2 rendering or WeasyPrint conversion fails.

    Attributes
    ----------
    stage:
        ``"template"`` for Jinja2 errors; ``"pdf"`` for WeasyPrint errors.
    cause:
        The underlying exception.
    """

    def __init__(self, stage: str, message: str, cause: Exception | None = None) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.cause = cause


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_datetime(dt: datetime | None) -> str:
    """Return a human-friendly UTC datetime string, or empty string if None."""
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _prepare_context(report_data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalise and enrich the raw report_data dict before template rendering.

    Adds derived display fields without mutating the caller's dict.
    """
    ctx = dict(report_data)

    # Ensure generated_at is always a formatted string for the template.
    raw_dt = ctx.get("generated_at")
    if isinstance(raw_dt, datetime):
        ctx["generated_at_str"] = _format_datetime(raw_dt)
    elif isinstance(raw_dt, str):
        ctx["generated_at_str"] = raw_dt
    else:
        # BUG-027 fix: use datetime.now(timezone.utc) to produce a timezone-aware
        # datetime, consistent with the rest of the codebase.
        ctx["generated_at_str"] = _format_datetime(datetime.now(timezone.utc))

    # Format cost to 4 decimal places.
    ctx["total_cost_usd_str"] = f"${ctx.get('total_cost_usd', 0.0):.4f}"

    # Provide safe defaults for optional template variables.
    ctx.setdefault("total_sources", 0)
    ctx.setdefault("total_findings", 0)
    ctx.setdefault("sections", [])

    return ctx


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_pdf(
    report_data: dict[str, Any],
    template_dir: str,
    output_path: str,
    template_name: str = _DEFAULT_TEMPLATE_NAME,
) -> str:
    """
    Render *report_data* using a Jinja2 HTML template and save a PDF.

    Parameters
    ----------
    report_data:
        Dictionary of template variables (see module docstring for required
        keys).
    template_dir:
        Absolute or relative path to the directory containing
        ``template_name``.
    output_path:
        Absolute path where the output PDF should be written.  Parent
        directories must exist (or caller must create them beforehand).
    template_name:
        Filename of the Jinja2 template to use.  Defaults to
        ``report.html.j2``.

    Returns
    -------
    str
        The absolute path of the written PDF (same as ``output_path``).

    Raises
    ------
    ReportRenderError
        If Jinja2 fails to render the template or WeasyPrint cannot convert
        the HTML to PDF.
    FileNotFoundError
        If ``template_dir`` does not exist or ``template_name`` is absent.
    """
    from jinja2 import (  # noqa: PLC0415
        Environment,
        FileSystemLoader,
        TemplateNotFound,
        TemplateError,
        select_autoescape,
    )

    template_dir_path = Path(template_dir).resolve()
    if not template_dir_path.is_dir():
        raise FileNotFoundError(
            f"Template directory does not exist: {template_dir_path}"
        )

    # H-07 fix: reject template directories outside the project root so a
    # caller can't pass ``..`` or a symlink and have Jinja2 load untrusted
    # templates from anywhere on the filesystem.
    if not template_dir_path.is_relative_to(_PROJECT_ROOT):
        raise ValueError(
            f"template_dir must be under project root {_PROJECT_ROOT}; "
            f"refusing to load templates from {template_dir_path}"
        )

    # ── Step 1: render HTML ──────────────────────────────────────────────────
    logger.info(
        "render_html_template",
        template_dir=str(template_dir_path),
        template_name=template_name,
    )

    env = Environment(
        loader=FileSystemLoader(str(template_dir_path)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    # Add custom Jinja2 filters.
    env.filters["format_datetime"] = _format_datetime

    try:
        template = env.get_template(template_name)
    except TemplateNotFound as exc:
        raise FileNotFoundError(
            f"Template '{template_name}' not found in {template_dir_path}"
        ) from exc

    ctx = _prepare_context(report_data)

    try:
        html_content = template.render(**ctx)
    except TemplateError as exc:
        logger.error("jinja2_template_render_failed", error=str(exc))
        raise ReportRenderError(
            stage="template",
            message=str(exc),
            cause=exc,
        ) from exc

    logger.debug("html_rendered", char_count=len(html_content))

    # ── Step 2: convert HTML → PDF ───────────────────────────────────────────
    logger.info("converting_html_to_pdf", output_path=output_path)

    try:
        from weasyprint import HTML as WeasyHTML  # noqa: PLC0415
    except ImportError as exc:
        raise ReportRenderError(
            stage="pdf",
            message="WeasyPrint is not installed. Install it with: pip install weasyprint",
            cause=exc,
        ) from exc

    try:
        WeasyHTML(string=html_content, base_url=str(template_dir_path)).write_pdf(
            output_path
        )
    except Exception as exc:
        logger.error("weasyprint_pdf_conversion_failed", error=str(exc))
        raise ReportRenderError(
            stage="pdf",
            message=str(exc),
            cause=exc,
        ) from exc

    output_size = Path(output_path).stat().st_size
    logger.info(
        "pdf_written",
        output_path=output_path,
        size_bytes=output_size,
    )

    return output_path
