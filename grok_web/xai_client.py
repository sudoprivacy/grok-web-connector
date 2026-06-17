"""xAI REST API client for Grok Imagine.

Parallel backend to GrokClient — same result types, no browser required.
Useful for A/B moderation comparison and serverless deployments.

    from grok_web import get_api_client

    async with get_api_client() as client:
        result = await client.create_image({"prompt": "a cat", "model": "grok-imagine-image"})
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from .auth import load_api_key
from .exceptions import (
    GrokAPIError,
    GrokAuthError,
    GrokConfigError,
    GrokNotFoundError,
    GrokRateLimitError,
)
from .models import ImageEditResult, ImageGenerationResult, VideoGenerationResult
from .schema import API_EDIT_KEYS, API_IMAGE_KEYS, API_VIDEO_KEYS, validate_params

logger = logging.getLogger(__name__)

XAI_API_BASE = "https://api.x.ai/v1"


class XAIClient:
    """xAI REST API client for Grok Imagine image and video generation.

    Use when you want to generate images/videos via xAI's official API
    without a browser. Requires an XAI_API_KEY (from https://console.x.ai).

    API key resolution: constructor param → $XAI_API_KEY env var →
    ``xai_api_key`` field in ~/.grok-config.json.

    Returns the same result types as GrokClient (browser-based), so callers
    can swap backends transparently for A/B comparison.
    """

    def __init__(
        self,
        api_key: str | None = None,
        config_path: Path | str | None = None,
    ):
        resolved = api_key or load_api_key(config_path)
        if not resolved:
            raise GrokConfigError(
                "No xAI API key found. Provide api_key=, set $XAI_API_KEY, "
                'or add "xai_api_key" to ~/.grok-config.json.'
            )
        self._api_key = resolved
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> XAIClient:
        self._http = httpx.AsyncClient(
            base_url=XAI_API_BASE,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout=600.0),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # -----------------------------------------------------------------
    # Internal HTTP
    # -----------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Make an authenticated request to the xAI API.

        Failure:
            401/403 → GrokAuthError (bad or expired API key).
            429 → GrokRateLimitError (rate limit hit, retry later).
            404 → GrokNotFoundError.
            Other 4xx/5xx → GrokAPIError with status_code.
        """
        if not self._http:
            raise GrokConfigError("XAIClient not entered as async context manager")

        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:
            kwargs["timeout"] = timeout

        resp = await self._http.request(method, path, **kwargs)

        if resp.status_code in (401, 403):
            raise GrokAuthError(f"xAI API auth failed ({resp.status_code}): {resp.text}")
        if resp.status_code == 429:
            raise GrokRateLimitError(f"xAI API rate limit hit: {resp.text}")
        if resp.status_code == 404:
            raise GrokNotFoundError(f"xAI API resource not found: {path}")
        if resp.status_code >= 400:
            raise GrokAPIError(
                f"xAI API error ({resp.status_code}): {resp.text}",
                status_code=resp.status_code,
            )

        return resp.json()

    # -----------------------------------------------------------------
    # Image generation
    # -----------------------------------------------------------------

    async def create_image(self, params: dict) -> ImageGenerationResult:
        """Generate images via xAI Imagine API.

        Use when: you want text-to-image generation without a browser.
        Returns the same ImageGenerationResult as GrokClient.create_image.

        Args:
            params: Dict with keys from API_IMAGE_KEYS.
                Required: prompt, model.

        Failure:
            Missing model → GrokConfigError.
            Moderation block → images list will be empty.
        """
        p = validate_params(params, API_IMAGE_KEYS)

        model = p.get("model")
        prompt = p.get("prompt")
        if not model:
            raise GrokConfigError("'model' is required for API image generation")
        if not prompt:
            raise GrokConfigError("'prompt' is required for image generation")

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": p.get("output_count", 4),
        }
        if p.get("aspect_ratio"):
            body["aspect_ratio"] = p["aspect_ratio"]
        if p.get("response_format"):
            body["response_format"] = p["response_format"]

        timeout = p.get("timeout", 300)
        data = await self._request("POST", "/images/generations", json=body, timeout=float(timeout))

        images = []
        for item in data.get("data", []):
            url = item.get("url") or item.get("b64_json", "")
            images.append(
                {
                    "image_id": None,
                    "image_url": url,
                    "moderated": False,
                    "r_rated": False,
                    "progress": 100,
                    "post_id": None,
                }
            )

        return ImageGenerationResult(
            prompt=prompt,
            images=images,
            conversation_id=None,
        )

    # -----------------------------------------------------------------
    # Image editing
    # -----------------------------------------------------------------

    async def edit_image(self, params: dict) -> ImageEditResult:
        """Edit an image via xAI Imagine API.

        Use when: you have a source image URL and want to edit it without
        a browser. The first entry in 'images' is the source; additional
        entries are references (up to 3 total).

        Args:
            params: Dict with keys from API_EDIT_KEYS.
                Required: prompt, model, images (list with source URL first).

        Failure:
            Missing model or images → GrokConfigError.
        """
        p = validate_params(params, API_EDIT_KEYS)

        model = p.get("model")
        prompt = p.get("prompt")
        image_list = p.get("images", [])
        if not model:
            raise GrokConfigError("'model' is required for API image editing")
        if not prompt:
            raise GrokConfigError("'prompt' is required for image editing")
        if not image_list:
            raise GrokConfigError("'images' with at least one source URL is required")

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "image": {"url": image_list[0], "type": "image_url"},
        }
        if p.get("response_format"):
            body["response_format"] = p["response_format"]

        timeout = p.get("timeout", 300)
        data = await self._request("POST", "/images/edits", json=body, timeout=float(timeout))

        images = []
        for item in data.get("data", []):
            url = item.get("url") or item.get("b64_json", "")
            images.append(
                {
                    "image_id": None,
                    "post_id": None,
                    "image_url": url,
                    "moderated": False,
                    "r_rated": False,
                    "progress": 100,
                }
            )

        return ImageEditResult(
            post_id=image_list[0],
            edit_prompt=prompt,
            images=images,
            conversation_id=None,
        )

    # -----------------------------------------------------------------
    # Video generation
    # -----------------------------------------------------------------

    async def create_video(self, params: dict) -> VideoGenerationResult:
        """Generate video via xAI Imagine API.

        Use when: you want text-to-video or image-to-video generation
        without a browser. Submits the job and polls until complete or timeout.

        Args:
            params: Dict with keys from API_VIDEO_KEYS.
                Required: prompt, model.
                Optional: images (first entry used as source frame URL),
                duration (int seconds or string like '6s', max 15).

        Failure:
            Timeout → returns result with in_progress=True; resume via
            a new create_video call or wait manually.
        """
        p = validate_params(params, API_VIDEO_KEYS)

        model = p.get("model")
        prompt = p.get("prompt")
        if not model:
            raise GrokConfigError("'model' is required for API video generation")
        if not prompt:
            raise GrokConfigError("'prompt' is required for video generation")

        # Parse duration: accept "6s", "10s" strings or int
        duration_raw = p.get("duration", "10s")
        if isinstance(duration_raw, str):
            duration = int(duration_raw.rstrip("s"))
        else:
            duration = int(duration_raw)

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "duration": duration,
        }
        # img2vid: use first image as source frame
        image_list = p.get("images", [])
        if image_list:
            body["image_url"] = image_list[0]

        timeout = float(p.get("timeout", 600))
        poll_interval = float(p.get("poll_interval", 5.0))

        # Submit
        submit_data = await self._request(
            "POST",
            "/videos/generations",
            json=body,
            timeout=timeout,
        )
        request_id = submit_data.get("request_id") or submit_data.get("id", "")

        # Poll
        elapsed = 0.0
        status = "pending"

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            poll_data = await self._request("GET", f"/videos/{request_id}", timeout=30.0)
            status = poll_data.get("status", "pending")

            if status in ("done", "failed", "expired"):
                break

        moderated = status == "failed"
        progress = 100 if status == "done" else 0

        return VideoGenerationResult(
            video_id=request_id,
            source_post_id=None,
            parent_post_id=request_id,
            moderated=moderated,
            progress=progress,
            mode="api",
            model_name=model,
            image_reference=image_list[0] if image_list else None,
            conversation_id=None,
            statsig_id=None,
            duration_s=duration if status == "done" else None,
            is_persisted=status == "done",
        )
