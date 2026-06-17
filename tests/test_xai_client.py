"""Unit tests for XAIClient (xAI REST API backend).

Pure unit tests — no real API calls. All HTTP is mocked via httpx transport.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from grok_web import XAIClient, get_api_client
from grok_web.exceptions import (
    GrokAPIError,
    GrokAuthError,
    GrokConfigError,
    GrokNotFoundError,
    GrokRateLimitError,
)
from grok_web.models import ImageEditResult, ImageGenerationResult, VideoGenerationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """Build a mock transport that returns responses in order."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        return responses[idx]

    return httpx.MockTransport(handler)


def _json_response(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


def _text_response(text: str, status: int = 400) -> httpx.Response:
    return httpx.Response(status, text=text)


@pytest.fixture
def api_key():
    return "xai-test-key-000"


# ---------------------------------------------------------------------------
# Construction & API key resolution
# ---------------------------------------------------------------------------


def test_constructor_accepts_api_key(api_key):
    client = XAIClient(api_key=api_key)
    assert client._api_key == api_key


def test_constructor_raises_without_key():
    with patch("grok_web.xai_client.load_api_key", return_value=None):
        with pytest.raises(GrokConfigError, match="No xAI API key"):
            XAIClient()


def test_constructor_falls_back_to_load_api_key():
    with patch("grok_web.xai_client.load_api_key", return_value="xai-from-config"):
        client = XAIClient()
        assert client._api_key == "xai-from-config"


def test_get_api_client_returns_xai_client(api_key):
    client = get_api_client(api_key=api_key)
    assert isinstance(client, XAIClient)


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_image_success(api_key):
    transport = _mock_transport(
        [
            _json_response(
                {
                    "data": [
                        {"url": "https://assets.grok.com/img1.png"},
                        {"url": "https://assets.grok.com/img2.png"},
                    ]
                }
            ),
        ]
    )

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    result = await client.create_image(
        {
            "prompt": "a cat",
            "model": "grok-imagine-image",
            "output_count": 2,
        }
    )

    assert isinstance(result, ImageGenerationResult)
    assert result.prompt == "a cat"
    assert result.total_count == 2
    assert result.success_count == 2
    assert result.moderated_count == 0
    assert len(result.image_urls) == 2
    assert result.image_urls[0] == "https://assets.grok.com/img1.png"


@pytest.mark.asyncio
async def test_create_image_missing_model(api_key):
    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=_mock_transport([]), base_url="https://api.x.ai/v1")

    with pytest.raises(GrokConfigError, match="model"):
        await client.create_image({"prompt": "a cat"})


@pytest.mark.asyncio
async def test_create_image_missing_prompt(api_key):
    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=_mock_transport([]), base_url="https://api.x.ai/v1")

    with pytest.raises(GrokConfigError, match="prompt"):
        await client.create_image({"model": "grok-imagine-image"})


# ---------------------------------------------------------------------------
# Image editing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_image_success(api_key):
    transport = _mock_transport(
        [
            _json_response(
                {
                    "data": [
                        {"url": "https://assets.grok.com/edited.png"},
                    ]
                }
            ),
        ]
    )

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    result = await client.edit_image(
        {
            "prompt": "add sunglasses",
            "model": "grok-imagine-image-quality",
            "images": ["https://example.com/source.jpg"],
        }
    )

    assert isinstance(result, ImageEditResult)
    assert result.edit_prompt == "add sunglasses"
    assert result.success_count == 1
    assert result.image_urls[0] == "https://assets.grok.com/edited.png"


# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_video_polls_until_done(api_key):
    transport = _mock_transport(
        [
            # Submit
            _json_response({"request_id": "vid-001"}),
            # Poll 1: pending
            _json_response({"status": "pending"}),
            # Poll 2: done
            _json_response(
                {
                    "status": "done",
                    "video": {"url": "https://assets.grok.com/video.mp4"},
                }
            ),
        ]
    )

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    result = await client.create_video(
        {
            "prompt": "a cat dancing",
            "model": "grok-imagine-video",
            "duration": "6s",
            "poll_interval": 0.01,
        }
    )

    assert isinstance(result, VideoGenerationResult)
    assert result.video_id == "vid-001"
    assert result.progress == 100
    assert result.moderated is False
    assert result.is_persisted is True
    assert result.mode == "api"


@pytest.mark.asyncio
async def test_create_video_failed_is_moderated(api_key):
    transport = _mock_transport(
        [
            _json_response({"request_id": "vid-002"}),
            _json_response({"status": "failed"}),
        ]
    )

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    result = await client.create_video(
        {
            "prompt": "test",
            "model": "grok-imagine-video",
            "poll_interval": 0.01,
        }
    )

    assert result.moderated is True
    assert result.progress == 0


@pytest.mark.asyncio
async def test_create_video_timeout_returns_in_progress(api_key):
    transport = _mock_transport(
        [
            _json_response({"request_id": "vid-003"}),
            # Always pending
            _json_response({"status": "pending"}),
            _json_response({"status": "pending"}),
            _json_response({"status": "pending"}),
        ]
    )

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    result = await client.create_video(
        {
            "prompt": "test",
            "model": "grok-imagine-video",
            "timeout": 0.03,
            "poll_interval": 0.01,
        }
    )

    assert result.in_progress is True
    assert result.progress == 0


@pytest.mark.asyncio
async def test_create_video_duration_string_parsed(api_key):
    """Duration '10s' string is parsed to int 10."""
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            requests_seen.append(body)
            return _json_response({"request_id": "vid-004"})
        return _json_response({"status": "done", "video": {"url": "x"}})

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.x.ai/v1",
    )

    await client.create_video(
        {
            "prompt": "test",
            "model": "grok-imagine-video",
            "duration": "10s",
            "poll_interval": 0.01,
        }
    )

    assert requests_seen[0]["duration"] == 10


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_on_401(api_key):
    transport = _mock_transport([_text_response("unauthorized", 401)])

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    with pytest.raises(GrokAuthError):
        await client.create_image({"prompt": "x", "model": "grok-imagine-image"})


@pytest.mark.asyncio
async def test_rate_limit_on_429(api_key):
    transport = _mock_transport([_text_response("rate limited", 429)])

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    with pytest.raises(GrokRateLimitError):
        await client.create_image({"prompt": "x", "model": "grok-imagine-image"})


@pytest.mark.asyncio
async def test_not_found_on_404(api_key):
    transport = _mock_transport([_text_response("not found", 404)])

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    with pytest.raises(GrokNotFoundError):
        await client.create_image({"prompt": "x", "model": "grok-imagine-image"})


@pytest.mark.asyncio
async def test_api_error_on_500(api_key):
    transport = _mock_transport([_text_response("internal error", 500)])

    client = XAIClient(api_key=api_key)
    client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

    with pytest.raises(GrokAPIError) as exc_info:
        await client.create_image({"prompt": "x", "model": "grok-imagine-image"})
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Result type parity
# ---------------------------------------------------------------------------


def test_result_types_are_same_as_browser_client():
    """API and browser clients return the same classes."""
    from grok_web.models import (
        ImageEditResult as BrowserEditResult,
    )
    from grok_web.models import (
        ImageGenerationResult as BrowserImageResult,
    )
    from grok_web.models import (
        VideoGenerationResult as BrowserVideoResult,
    )

    assert ImageGenerationResult is BrowserImageResult
    assert ImageEditResult is BrowserEditResult
    assert VideoGenerationResult is BrowserVideoResult
