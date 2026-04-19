"""Image generation via NanoBanana API."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger(__name__)


async def generate_image(
    prompt: str,
    api_key: str,
    output_path: Path,
    size: str = "1024x1024",
    timeout: float = 120.0,
    data_root: Path | None = None,
) -> Path:
    """Generate an image and save to disk.

    Parameters
    ----------
    prompt:
        Text description of the desired image.
    api_key:
        NanoBanana API key.
    output_path:
        Where to write the resulting PNG/JPEG file.
    size:
        Image dimensions (default ``"1024x1024"``).
    timeout:
        HTTP request timeout in seconds.
    data_root:
        Optional sandbox root. When provided, *output_path* must resolve
        inside this directory (H-04 path-traversal guard).

    Returns
    -------
    Path
        The *output_path* after the file has been written.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.nanobanana.ai/v1/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": prompt,
                "size": size,
                "response_format": "b64_json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    img_b64: str = data["data"][0]["b64_json"]
    img_bytes = base64.b64decode(img_b64)

    # H-04 fix: ensure output_path stays within data_root when provided.
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
        path=str(output_path),
        bytes=len(img_bytes),
    )
    return output_path
