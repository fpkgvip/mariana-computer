"""Image generation via LLM Gateway (OpenAI-compatible ``/images/generations``).

The LLM Gateway exposes a unified image-generation surface that proxies to
Google's Gemini image models (and several others).  We route through the
gateway so a single ``LLM_GATEWAY_API_KEY`` covers both LLM inference and
media generation — no per-provider key sprawl.

Gateway docs: https://docs.llmgateway.io/features/image-generation

Request
-------
    POST {base}/images/generations
    Authorization: Bearer <LLM_GATEWAY_API_KEY>
    Content-Type: application/json
    {
        "model":   "gemini-3.1-flash-image-preview",
        "prompt":  "...",
        "n":       1,
        "size":    "1024x1024"          (optional; not all models honour)
    }

Response
--------
    { "data": [ { "b64_json": "<base64 PNG>" } ] }
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger(__name__)


# Default model.  Override per-call via ``model=`` or the ``AGENT_IMAGE_MODEL``
# env var.  Gateway resolves ``"auto"`` to ``gemini-3-pro-image-preview``.
DEFAULT_IMAGE_MODEL = os.getenv(
    "AGENT_IMAGE_MODEL",
    "gemini-3.1-flash-image-preview",
)


def _gateway_base_url() -> str:
    base = os.getenv("LLM_GATEWAY_BASE_URL", "https://api.llmgateway.io/v1").rstrip("/")
    return base


async def generate_image(
    prompt: str,
    api_key: str,
    output_path: Path,
    size: str = "1024x1024",
    timeout: float = 180.0,
    data_root: Path | None = None,
    model: str | None = None,
) -> Path:
    """Generate an image through the LLM Gateway and save to disk.

    Parameters
    ----------
    prompt:
        Text description of the desired image.
    api_key:
        LLM Gateway API key (``LLM_GATEWAY_API_KEY``).
    output_path:
        Where to write the resulting PNG file.
    size:
        Image dimensions hint (``"1024x1024"`` by default).  Some gateway
        image models accept ``size`` only in friendly buckets like ``"1K"``,
        ``"2K"``, ``"4K"`` — we therefore send ``size`` only if it looks
        like a classic ``WxH`` string the gateway currently accepts, and
        silently omit unrecognised values so the provider picks a default.
    timeout:
        HTTP request timeout in seconds.
    data_root:
        Optional sandbox root; *output_path* must resolve inside it.
    model:
        Override model id; defaults to ``$AGENT_IMAGE_MODEL`` or
        ``"gemini-3.1-flash-image-preview"``.
    """
    if not api_key:
        raise ValueError("generate_image: LLM_GATEWAY_API_KEY is empty")

    model_id = (model or DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL
    url = f"{_gateway_base_url()}/images/generations"

    payload: dict[str, object] = {
        "model": model_id,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }
    # Gemini image models reject classic ``size="1024x1024"`` — they want
    # ``image_size="1K"`` etc., which the gateway accepts but with a
    # different name than OpenAI's spec.  Rather than hard-code per-model
    # quirks we forward ``size`` only when it clearly matches the OpenAI
    # ``WxH`` form *and* the caller explicitly overrode the default.
    # Default path: omit ``size`` entirely and let the provider pick.
    _is_default = (size == "1024x1024") or not size
    if (not _is_default) and isinstance(size, str) and "x" in size:
        w, _, h = size.partition("x")
        if w.isdigit() and h.isdigit():
            payload["size"] = size

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            # Surface provider error verbatim so the agent's fix loop can read it.
            raise RuntimeError(
                f"LLM Gateway image generation failed: "
                f"status={resp.status_code} body={resp.text[:2000]}"
            )
        data = resp.json()

    # Gateway is OpenAI-compatible: data.data[0].b64_json
    try:
        img_b64: str = data["data"][0]["b64_json"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"LLM Gateway image response malformed: {str(data)[:500]}"
        ) from exc

    img_bytes = base64.b64decode(img_b64)

    # H-04 defence-in-depth: keep output inside the declared sandbox root.
    if data_root is not None:
        resolved_root = Path(data_root).resolve()
        resolved_out = Path(output_path).resolve()
        if not resolved_out.is_relative_to(resolved_root):
            raise ValueError(
                f"Refusing to write image outside data_root: {output_path!r}"
            )
        output_path = resolved_out

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(img_bytes)

    logger.info(
        "image_generated",
        prompt=prompt[:80],
        size=size,
        model=model_id,
        path=str(output_path),
        bytes=len(img_bytes),
    )
    return output_path
