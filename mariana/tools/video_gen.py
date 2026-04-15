"""Video generation via Google Veo 3.1 API."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger(__name__)


async def generate_video(
    prompt: str,
    api_key: str,
    output_path: Path,
    duration_seconds: int = 10,
    timeout: float = 600.0,
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
        async with httpx.AsyncClient(timeout=timeout) as client:
            dl_resp = await client.get(
                video_part["fileData"]["fileUri"],
                headers={"x-goog-api-key": api_key},
            )
            dl_resp.raise_for_status()
            video_bytes = dl_resp.content
    else:
        raise ValueError("No video data in Veo 3.1 response")

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
