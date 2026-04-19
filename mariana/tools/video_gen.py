"""Video generation via Google Veo 3.1 API."""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger(__name__)

# C-03 fix: only Google-owned hosts may receive the API key or be fetched
# for the video payload. If Veo ever returns a fileUri outside this set we
# refuse rather than leak credentials or follow an SSRF vector.
_ALLOWED_VIDEO_HOSTS: frozenset[str] = frozenset(
    {
        "generativelanguage.googleapis.com",
        "storage.googleapis.com",
    }
)


def _validate_google_fileuri(file_uri: str) -> str:
    """Return *file_uri* if its hostname is on the Google allowlist; raise otherwise.

    Rejects empty, non-HTTPS, and non-Google URIs — this prevents a malicious
    or tampered API response from redirecting us to an attacker-controlled
    host (to which we would otherwise send the API key).
    """
    if not file_uri or not isinstance(file_uri, str):
        raise ValueError("Veo fileUri is empty or malformed")
    parsed = urlparse(file_uri)
    if parsed.scheme != "https":
        raise ValueError(f"Veo fileUri must be HTTPS, got scheme={parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_VIDEO_HOSTS:
        raise ValueError(
            f"Veo fileUri host not in allowlist: host={host!r}; "
            f"refusing to send credentials to untrusted domain"
        )
    return file_uri


async def generate_video(
    prompt: str,
    api_key: str,
    output_path: Path,
    duration_seconds: int = 10,
    timeout: float = 600.0,
    data_root: Path | None = None,
) -> Path:
    """Generate a video using Veo 3.1 and save to disk.

    Parameters
    ----------
    prompt:
        Text description of the desired video content.
    api_key:
        Google AI API key (Veo 3.1 access).
    output_path:
        Where to write the resulting video file.
    duration_seconds:
        Target video duration in seconds (default 10).
    timeout:
        HTTP request timeout in seconds (default 600 — video gen is slow).
    data_root:
        Optional sandbox root. When provided, *output_path* must resolve
        inside this directory (defence-in-depth path-traversal guard, H-04).

    Returns
    -------
    Path
        The *output_path* after the file has been written.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://generativelanguage.googleapis.com/v1beta/models/veo-3.1:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"videoDurationSeconds": duration_seconds},
            },
        )
        resp.raise_for_status()
        data = resp.json()

    video_part = data["candidates"][0]["content"]["parts"][0]

    if "inlineData" in video_part:
        video_bytes = base64.b64decode(video_part["inlineData"]["data"])
    elif "fileData" in video_part:
        file_uri = _validate_google_fileuri(video_part["fileData"]["fileUri"])
        async with httpx.AsyncClient(timeout=timeout) as client:
            dl_resp = await client.get(
                file_uri,
                headers={"x-goog-api-key": api_key},
            )
            dl_resp.raise_for_status()
            video_bytes = dl_resp.content
    else:
        raise ValueError("No video data in Veo 3.1 response")

    # H-04 fix: ensure output_path stays within data_root when provided.
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
        duration_seconds=duration_seconds,
        path=str(output_path),
        bytes=len(video_bytes),
    )
    return output_path
