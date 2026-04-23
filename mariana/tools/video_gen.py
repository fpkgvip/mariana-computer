"""Video generation via LLM Gateway (OpenAI-compatible async videos API).

The LLM Gateway exposes a long-running videos API that proxies to Google Vertex
AI's Veo 3.1 (and, via the Avalanche provider, a managed Veo variant). Flow:

    1. POST  {base}/videos                     → returns { "id": "<video_id>" }
    2. GET   {base}/videos/{id}                → poll until status == "completed"
    3. GET   {base}/videos/{id}/content        → stream raw video bytes
       (or follow the signed URL embedded in the terminal poll body)

We always route through the gateway so a single ``LLM_GATEWAY_API_KEY`` covers
LLM + images + video.

Provider matrix (confirmed end-to-end against production gateway):

* ``avalanche/veo-3.1-fast-generate-preview`` — **default**.  Managed Veo 3.1
  Fast.  Constraints per gateway: fixed 8-second clips; sizes limited to
  ``1920x1080`` / ``1080x1920`` / ``3840x2160`` / ``2160x3840``.
* ``google-vertex/veo-3.1-*`` — requires a project with Vertex AI access that
  the shared ``llmgatewayio`` GCP project does not currently have; returns
  404 "Publisher Model ... not found".  Exposed for future use; callers must
  opt in explicitly via env var or parameter.
* ``openai/sora-2`` / ``sora-2-pro`` — **deactivated** by the gateway (410).

Gateway docs: https://docs.llmgateway.io/features/video-generation
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---- Defaults ---------------------------------------------------------------

# We prefix with ``avalanche/`` because the bare ``veo-3.1-fast-generate-preview``
# routes to Vertex, which the gateway's shared GCP project cannot access.
DEFAULT_VIDEO_MODEL = os.getenv(
    "AGENT_VIDEO_MODEL",
    "avalanche/veo-3.1-fast-generate-preview",
)
DEFAULT_VIDEO_SIZE = os.getenv("AGENT_VIDEO_SIZE", "1920x1080")

# Per-model duration + size constraints.  Keys are provider prefixes as they
# appear in the ``model`` string; values describe the legal input domain.
_AVALANCHE_VEO_DURATIONS: tuple[int, ...] = (8,)  # fixed 8s clips
_AVALANCHE_VEO_SIZES: frozenset[str] = frozenset(
    {"1920x1080", "1080x1920", "3840x2160", "2160x3840"}
)
_VERTEX_VEO_DURATIONS: tuple[int, ...] = (4, 6, 8, 10)
_VERTEX_VEO_SIZES: frozenset[str] = frozenset(
    {"1280x720", "720x1280", "1920x1080", "1080x1920", "3840x2160", "2160x3840"}
)
# Generic fallback used for unknown model strings; superset of both providers
# so we never reject callers who pass a newer model we haven't pinned.
_GENERIC_DURATIONS: tuple[int, ...] = (4, 6, 8, 10)

# Poll cadence — start fast (jobs sometimes finish in <30s for 4s clips), back
# off to avoid hammering the gateway.
_POLL_INTERVALS: tuple[float, ...] = (2.0, 3.0, 5.0, 5.0, 8.0, 10.0, 15.0)
# Hard wall-clock cap for the whole polling loop.
_POLL_WALL_CLOCK_CAP_SEC_DEFAULT = 600.0

# Only these gateway response statuses are terminal.
_TERMINAL_OK = frozenset({"completed", "succeeded"})
_TERMINAL_BAD = frozenset({"failed", "canceled", "cancelled", "expired", "error"})


def _gateway_base_url() -> str:
    return os.getenv("LLM_GATEWAY_BASE_URL", "https://api.llmgateway.io/v1").rstrip("/")


def _model_provider(model_id: str) -> str:
    """Extract the provider prefix (``avalanche``, ``google-vertex`` …) if any."""
    model_id = (model_id or "").strip()
    if "/" in model_id:
        return model_id.split("/", 1)[0].lower()
    return ""


def _allowed_durations(model_id: str) -> tuple[int, ...]:
    prov = _model_provider(model_id)
    if prov == "avalanche":
        return _AVALANCHE_VEO_DURATIONS
    if prov in ("google-vertex", "google", "vertex"):
        return _VERTEX_VEO_DURATIONS
    return _GENERIC_DURATIONS


def _allowed_sizes(model_id: str) -> frozenset[str] | None:
    """Return the legal size set for *model_id*, or ``None`` if unknown (no check)."""
    prov = _model_provider(model_id)
    if prov == "avalanche":
        return _AVALANCHE_VEO_SIZES
    if prov in ("google-vertex", "google", "vertex"):
        return _VERTEX_VEO_SIZES
    return None


def _quantise_seconds(seconds: int, model_id: str) -> int:
    """Snap *seconds* to the closest duration the model supports."""
    allowed = _allowed_durations(model_id)
    s = max(1, int(seconds))
    if s in allowed:
        return s
    # Pick the closest allowed value (ties → smaller, cheaper).
    return min(allowed, key=lambda v: (abs(v - s), v))


def _coerce_size(size: str, model_id: str) -> str:
    """Return a size the model accepts.

    If *size* is not in the model's allowed set, pick the first allowed size
    that shares the same orientation (landscape/portrait); otherwise fall back
    to the first allowed size.  Unknown model → return *size* unchanged.
    """
    allowed = _allowed_sizes(model_id)
    if allowed is None:
        return size
    size = (size or "").strip()
    if size in allowed:
        return size
    # Determine orientation of requested size if parseable.
    orientation: str | None = None
    try:
        w, h = (int(x) for x in size.lower().split("x", 1))
        orientation = "portrait" if h > w else "landscape"
    except Exception:
        orientation = None
    def _area(s: str) -> int:
        try:
            w, h = (int(x) for x in s.split("x", 1))
            return w * h
        except Exception:
            return 0

    if orientation:
        matches: list[str] = []
        for cand in allowed:
            try:
                cw, ch = (int(x) for x in cand.split("x", 1))
            except Exception:
                continue
            cand_orient = "portrait" if ch > cw else "landscape"
            if cand_orient == orientation:
                matches.append(cand)
        if matches:
            # Prefer the smallest area match of the right orientation — cheapest
            # and closest to a "default" user asking for ~720p / 1080p.
            return min(matches, key=_area)
    # Pick the smallest landscape (16:9) as final default.
    for cand in ("1920x1080", "1280x720"):
        if cand in allowed:
            return cand
    return min(allowed, key=_area)


async def _submit_job(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    payload: dict[str, object],
) -> str:
    resp = await client.post(
        f"{base}/videos",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LLM Gateway video submit failed: "
            f"status={resp.status_code} body={resp.text[:2000]}"
        )
    body = resp.json()
    video_id = body.get("id") or body.get("video_id")
    if not isinstance(video_id, str) or not video_id:
        raise RuntimeError(
            f"LLM Gateway video submit response missing id: {str(body)[:500]}"
        )
    return video_id


async def _poll_job(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    video_id: str,
    wall_clock_cap: float,
) -> dict[str, Any]:
    """Poll ``GET /videos/{id}`` until status is terminal or wall-clock cap hits."""
    deadline = asyncio.get_running_loop().time() + wall_clock_cap
    interval_iter = iter(_POLL_INTERVALS)
    last_interval = _POLL_INTERVALS[-1]
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError(
                f"LLM Gateway video polling exceeded {wall_clock_cap:.0f}s "
                f"(video_id={video_id})"
            )
        resp = await client.get(
            f"{base}/videos/{video_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"LLM Gateway video poll failed: "
                f"status={resp.status_code} body={resp.text[:1500]}"
            )
        body = resp.json()
        status = str(body.get("status", "")).lower()
        if status in _TERMINAL_OK:
            return body
        if status in _TERMINAL_BAD:
            err = body.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(
                f"LLM Gateway video job {status}: {msg or str(body)[:500]}"
            )
        try:
            interval = next(interval_iter)
        except StopIteration:
            interval = last_interval
        await asyncio.sleep(min(interval, max(remaining, 0.5)))


def _extract_content_url(status_body: dict[str, Any]) -> str | None:
    """Return the signed content URL embedded in a completed poll body, if any."""
    content = status_body.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "video":
                url = item.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
    return None


async def _download_content(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    video_id: str,
    *,
    signed_url: str | None = None,
) -> bytes:
    """Download the finished video.

    Prefers the signed URL embedded in the completion body (single hop, no
    extra auth). Falls back to the canonical ``GET /videos/{id}/content``
    endpoint with the gateway API key.
    """
    if signed_url:
        resp = await client.get(signed_url)
        if resp.status_code < 400 and resp.content:
            return resp.content
        # Fall through to the canonical endpoint on any failure.
        logger.warning(
            "video_download_signed_failed",
            video_id=video_id,
            status=resp.status_code,
            falling_back=True,
        )

    resp = await client.get(
        f"{base}/videos/{video_id}/content",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LLM Gateway video download failed: "
            f"status={resp.status_code} body={resp.text[:1500]}"
        )
    return resp.content


async def generate_video(
    prompt: str,
    api_key: str,
    output_path: Path,
    duration_seconds: int = 8,
    timeout: float = 900.0,
    data_root: Path | None = None,
    model: str | None = None,
    size: str | None = None,
) -> Path:
    """Generate a video through the LLM Gateway and save it to disk.

    The *duration_seconds* and *size* parameters are snapped to whatever the
    selected model actually supports (Avalanche Veo 3.1 Fast is fixed at 8s
    and 1920x1080 / 1080x1920 / 4K), so callers can pass any reasonable value
    without triggering a 400 from the gateway.

    Parameters
    ----------
    prompt:
        Text description of the desired video content.
    api_key:
        LLM Gateway API key.
    output_path:
        Destination MP4 file path.
    duration_seconds:
        Desired duration; snapped to the closest value the model supports.
    timeout:
        Upper bound on the overall wall-clock (per-request timeout is fixed).
    data_root:
        Optional sandbox root guard.
    model:
        Override model id (default ``avalanche/veo-3.1-fast-generate-preview``).
    size:
        Override video size; coerced to the closest model-supported size with
        matching orientation if needed.
    """
    if not api_key:
        raise ValueError("generate_video: LLM_GATEWAY_API_KEY is empty")

    model_id = (model or DEFAULT_VIDEO_MODEL).strip() or DEFAULT_VIDEO_MODEL
    seconds = _quantise_seconds(duration_seconds, model_id)
    raw_size = (size or DEFAULT_VIDEO_SIZE).strip() or DEFAULT_VIDEO_SIZE
    video_size = _coerce_size(raw_size, model_id)
    base = _gateway_base_url()

    payload: dict[str, object] = {
        "model": model_id,
        "prompt": prompt,
        "seconds": seconds,
        "size": video_size,
    }

    poll_cap = max(60.0, min(float(timeout), _POLL_WALL_CLOCK_CAP_SEC_DEFAULT * 2))
    # Per-request timeout is generous but capped — the polling loop owns the
    # overall wall-clock. Download can be large (25MB+ for 8s 1080p clips).
    request_timeout = httpx.Timeout(180.0, connect=20.0)

    async with httpx.AsyncClient(timeout=request_timeout) as client:
        logger.info(
            "video_submit",
            model=model_id,
            seconds=seconds,
            size=video_size,
            requested_size=raw_size,
            requested_seconds=duration_seconds,
            prompt=prompt[:80],
        )
        video_id = await _submit_job(client, base, api_key, payload)
        logger.info("video_polling", video_id=video_id, cap_sec=poll_cap)
        status_body = await _poll_job(client, base, api_key, video_id, poll_cap)
        signed_url = _extract_content_url(status_body)
        logger.info(
            "video_downloading",
            video_id=video_id,
            has_signed_url=bool(signed_url),
        )
        video_bytes = await _download_content(
            client, base, api_key, video_id, signed_url=signed_url
        )

    if not video_bytes:
        raise RuntimeError(f"LLM Gateway returned empty video bytes (id={video_id})")

    # H-04 defence-in-depth: keep output inside the sandbox root.
    if data_root is not None:
        resolved_root = Path(data_root).resolve()
        resolved_out = Path(output_path).resolve()
        if not resolved_out.is_relative_to(resolved_root):
            raise ValueError(
                f"Refusing to write video outside data_root: {output_path!r}"
            )
        output_path = resolved_out

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(video_bytes)

    logger.info(
        "video_generated",
        prompt=prompt[:80],
        duration_seconds=seconds,
        model=model_id,
        size=video_size,
        video_id=video_id,
        path=str(output_path),
        bytes=len(video_bytes),
    )
    return output_path
