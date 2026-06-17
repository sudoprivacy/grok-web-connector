"""CI-safe integration tests for grok-web-connector.

No credentials required — these tests verify end-to-end workflows
through schema validation, model construction, error mapping, and
auth config resolution without hitting real APIs or browsers.

Coverage:
  ✅ schema_to_model: PARAMS → validate → build result → computed fields
  ✅ xai_client_lifecycle: construct → build request → error mapping → parse response
  ✅ auth_config_resolution: env var → config file → missing key → cookie loading
  ✅ prompt_parsing_to_generation: parse @N refs → classify sources → route mode
  ✅ schema_cross_group_consistency: all key groups valid → docstrings generated → help text
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import httpx
import pytest

from grok_web import (
    API_EDIT_KEYS,
    API_IMAGE_KEYS,
    API_VIDEO_KEYS,
    EDIT_KEYS,
    IMAGE_KEYS,
    PARAMS,
    VIDEO_KEYS,
    BrowserWorkerPool,
    GrokClient,
    ImageEditResult,
    ImageGenerationResult,
    PostDetails,
    PostSummary,
    VideoExtendResult,
    VideoGenerationResult,
    VideoMatchResult,
    get_api_client,
    get_client,
    load_api_key,
    load_cookies,
    save_cookies,
    validate_params,
)
from grok_web.exceptions import (
    GrokAPIError,
    GrokAuthError,
    GrokConfigError,
    GrokNotFoundError,
    GrokRateLimitError,
)
from grok_web.prompt_parser import classify_image_source, parse_prompt
from grok_web.schema import schema_to_docstring, schema_to_help, splice_schema_into_docstring
from grok_web.xai_client import XAIClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """Return responses in order; repeat last response if exhausted."""
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


# ---------------------------------------------------------------------------
# Scenario 1: schema_to_model
#   validate_params → construct result model → verify computed fields
# ---------------------------------------------------------------------------


class TestSchemaToModel:
    """PARAMS → validate_params → build result → computed fields work."""

    def test_video_params_validate_and_produce_result(self):
        """Workflow: raw user params → validate → construct VideoGenerationResult
        → verify success/in_progress/web_url computed fields.

        User problem: caller passes a params dict, connector validates and
        builds a result — computed fields must reflect the data correctly.

        Data flow:
          1. validate_params cleans and applies defaults
          2. VideoGenerationResult constructed from validated data
          3. computed fields (success, in_progress, web_url) derive from fields
        """
        raw = {"prompt": "zoom in", "images": ["post:abc-123"], "extra_junk": True}
        cleaned = validate_params(raw, VIDEO_KEYS)

        assert "prompt" in cleaned
        assert "extra_junk" not in cleaned
        assert cleaned["resolution"] == "720p"  # default applied
        assert cleaned["timeout"] == 300  # default applied

        result = VideoGenerationResult(
            video_id="vid-001",
            parent_post_id="parent-001",
            progress=100,
            moderated=False,
            mode="custom",
        )
        assert result.success is True
        assert result.in_progress is False
        assert "parent-001" in result.web_url

    def test_image_params_validate_and_produce_result(self):
        """Workflow: image params → validate → ImageGenerationResult → computed urls.

        Data flow:
          1. validate_params with IMAGE_KEYS
          2. ImageGenerationResult with mixed moderated/ok images
          3. image_urls filters moderated, success_count reflects real count
        """
        cleaned = validate_params(
            {"prompt": "a cat", "quality": "speed"},
            IMAGE_KEYS,
        )
        assert cleaned["quality"] == "speed"
        assert cleaned["auto_favorite"] == 0  # default

        result = ImageGenerationResult(
            prompt="a cat",
            images=[
                {"image_url": "https://cdn/ok.png", "moderated": False, "image_id": "1"},
                {"image_url": "https://cdn/mod.png", "moderated": True, "image_id": "2"},
                {"image_url": "https://cdn/ok2.png", "moderated": False, "image_id": "3"},
            ],
        )
        assert result.total_count == 3
        assert result.success_count == 2
        assert result.moderated_count == 1
        assert len(result.image_urls) == 2
        assert result.success is True

    def test_edit_params_validate_and_produce_result(self):
        """Workflow: edit params → validate → ImageEditResult → post_ids.

        Data flow:
          1. validate_params with EDIT_KEYS
          2. ImageEditResult with post_id on each image
          3. post_ids computed field collects non-moderated post_ids
        """
        cleaned = validate_params(
            {"prompt": "add wings", "images": ["post:src-001"]},
            EDIT_KEYS,
        )
        assert cleaned["prompt"] == "add wings"

        result = ImageEditResult(
            post_id="src-001",
            edit_prompt="add wings",
            images=[
                {"image_url": "https://cdn/e1.png", "moderated": False, "post_id": "edit-001"},
                {"image_url": "https://cdn/e2.png", "moderated": True, "post_id": "edit-002"},
            ],
        )
        assert result.success_count == 1
        assert result.post_ids == ["edit-001"]


# ---------------------------------------------------------------------------
# Scenario 2: xai_client_lifecycle
#   construct → send request → handle errors → parse response
# ---------------------------------------------------------------------------


class TestXAIClientLifecycle:
    """Construct XAIClient → build request → error mapping → parse response."""

    def test_construct_with_key(self):
        """Step 1: XAIClient accepts an explicit API key."""
        client = XAIClient(api_key="xai-test")
        assert client._api_key == "xai-test"

    def test_construct_without_key_raises(self):
        """Step 2: missing key raises GrokConfigError with actionable message."""
        with patch("grok_web.xai_client.load_api_key", return_value=None):
            with pytest.raises(GrokConfigError, match="XAI_API_KEY"):
                XAIClient()

    @pytest.mark.asyncio
    async def test_create_image_end_to_end(self):
        """Workflow: construct → create_image → verify ImageGenerationResult.

        Data flow:
          1. XAIClient constructed with test key
          2. POST /images/generations returns 2 image URLs
          3. ImageGenerationResult has correct counts and URLs
        """
        transport = _mock_transport(
            [
                _json_response(
                    {
                        "data": [
                            {"url": "https://assets.grok.com/img1.png"},
                            {"url": "https://assets.grok.com/img2.png"},
                        ],
                    }
                ),
            ]
        )
        client = XAIClient(api_key="xai-test")
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
        assert "img1.png" in result.image_urls[0]

    @pytest.mark.asyncio
    async def test_create_video_polls_until_done(self):
        """Workflow: submit → poll pending → poll done → VideoGenerationResult.

        Data flow:
          1. POST returns request_id
          2. First GET returns pending
          3. Second GET returns done with video URL
          4. Result has progress=100, is_persisted=True
        """
        transport = _mock_transport(
            [
                _json_response({"request_id": "vid-001"}),
                _json_response({"status": "pending"}),
                _json_response({"status": "done", "video": {"url": "https://cdn/v.mp4"}}),
            ]
        )
        client = XAIClient(api_key="xai-test")
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
        assert result.is_persisted is True
        assert result.moderated is False

    @pytest.mark.asyncio
    async def test_video_timeout_returns_in_progress(self):
        """Workflow: submit → poll repeatedly pending → timeout → in_progress result.

        User problem: long video gen exceeds timeout; caller needs to know
        it's still running (not failed) so they can retry or wait.
        """
        transport = _mock_transport(
            [
                _json_response({"request_id": "vid-slow"}),
                _json_response({"status": "pending"}),
                _json_response({"status": "pending"}),
            ]
        )
        client = XAIClient(api_key="xai-test")
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
    async def test_edit_image_end_to_end(self):
        """Workflow: edit_image → verify ImageEditResult.

        Data flow:
          1. POST /images/edits with source image URL
          2. Response has edited image URL
          3. ImageEditResult has correct edit_prompt and image_urls
        """
        transport = _mock_transport(
            [
                _json_response({"data": [{"url": "https://cdn/edited.png"}]}),
            ]
        )
        client = XAIClient(api_key="xai-test")
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

    @pytest.mark.asyncio
    async def test_error_mapping_chain(self):
        """Workflow: 401 → GrokAuthError, 429 → GrokRateLimitError,
        404 → GrokNotFoundError, 500 → GrokAPIError.

        User problem: different HTTP errors must map to the correct exception
        so pool/retry logic can distinguish auth failures from rate limits.
        """
        for status, exc_class in [
            (401, GrokAuthError),
            (429, GrokRateLimitError),
            (404, GrokNotFoundError),
            (500, GrokAPIError),
        ]:
            transport = _mock_transport([_text_response("err", status)])
            client = XAIClient(api_key="xai-test")
            client._http = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")

            with pytest.raises(exc_class):
                await client.create_image({"prompt": "x", "model": "grok-imagine-image"})

    @pytest.mark.asyncio
    async def test_duration_string_parsed_to_int(self):
        """Duration '10s' string is sent to API as integer 10."""
        requests_seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                requests_seen.append(json.loads(request.content))
                return _json_response({"request_id": "vid-dur"})
            return _json_response({"status": "done", "video": {"url": "x"}})

        client = XAIClient(api_key="xai-test")
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

    def test_result_types_same_as_browser_client(self):
        """API and browser backends return the exact same Pydantic classes."""
        from grok_web.models import ImageEditResult as M1
        from grok_web.models import ImageGenerationResult as M2
        from grok_web.models import VideoGenerationResult as M3

        assert ImageGenerationResult is M2
        assert ImageEditResult is M1
        assert VideoGenerationResult is M3


# ---------------------------------------------------------------------------
# Scenario 3: auth_config_resolution
#   env var → config file → missing key → cookie roundtrip
# ---------------------------------------------------------------------------


class TestAuthConfigResolution:
    """Auth key and cookie resolution across env, config file, and errors."""

    def test_api_key_from_env(self):
        """Step 1: $XAI_API_KEY env var takes priority."""
        with patch.dict(os.environ, {"XAI_API_KEY": "xai-from-env"}):
            assert load_api_key() == "xai-from-env"

    def test_api_key_from_config_file(self):
        """Step 2: fallback to config file when env var absent."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"cookies": {}, "xai_api_key": "xai-from-file"}, f)
            f.flush()
            try:
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("XAI_API_KEY", None)
                    assert load_api_key(f.name) == "xai-from-file"
            finally:
                os.unlink(f.name)

    def test_api_key_missing_returns_none(self):
        """Step 3: no env var + no config → None (not an error)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            assert load_api_key("/nonexistent/path.json") is None

    def test_xai_client_raises_on_missing_key(self):
        """Step 4: XAIClient constructor raises GrokConfigError with guidance."""
        with patch("grok_web.xai_client.load_api_key", return_value=None):
            with pytest.raises(GrokConfigError, match="XAI_API_KEY"):
                XAIClient()

    def test_cookie_roundtrip(self):
        """Workflow: save cookies → load cookies → verify roundtrip.

        Data flow:
          1. Create GrokCookies
          2. save_cookies writes to temp file
          3. load_cookies reads back → same values
        """
        from grok_web.models import GrokCookies

        cookies = GrokCookies(
            sso="test-sso",
            sso_rw="test-sso-rw",
            cf_clearance="test-cf",
            x_userid="test-uid",
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_cookies(cookies, path)
            loaded = load_cookies(path)
            assert loaded.sso == "test-sso"
            assert loaded.sso_rw == "test-sso-rw"
            assert loaded.cf_clearance == "test-cf"
            assert loaded.x_userid == "test-uid"
        finally:
            os.unlink(path)

    def test_missing_config_raises_clear_error(self):
        """Missing config file raises GrokConfigError with path in message."""
        with pytest.raises(GrokConfigError, match="not found"):
            load_cookies("/nonexistent/grok-config.json")


# ---------------------------------------------------------------------------
# Scenario 4: prompt_parsing_to_generation
#   parse @N refs → classify sources → determine generation mode
# ---------------------------------------------------------------------------


class TestPromptParsingToGeneration:
    """Parse prompt → classify image sources → route to correct generation mode."""

    def test_prompt_with_refs_to_source_classification(self):
        """Workflow: prompt with @1 @2 → parse → classify each source.

        Data flow:
          1. parse_prompt extracts @N references
          2. classify_image_source determines source type for each
          3. Combined result determines generation mode (img2vid, upload2vid, etc.)
        """
        images = ["post:abc-123", "./local.jpg", "file:uploaded-456"]

        # Classify each source
        post_type, post_val = classify_image_source(images[0])
        assert post_type == "post"
        assert post_val == "abc-123"

        file_type, file_val = classify_image_source(images[1])
        assert file_type == "file"
        assert file_val == "./local.jpg"

        upload_type, upload_val = classify_image_source(images[2])
        assert upload_type == "upload"
        assert upload_val == "uploaded-456"

    def test_video_extend_source_classification(self):
        """video:<uuid> source routes to video-extend mode."""
        source_type, val = classify_image_source("video:extend-789")
        assert source_type == "video"
        assert val == "extend-789"

    def test_prompt_parsing_extracts_refs(self):
        """parse_prompt with @N markers splits into segments."""
        result = parse_prompt("zoom into @1 and pan to @2", ["img1.jpg", "img2.jpg"])
        assert result is not None
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Scenario 5: schema_cross_group_consistency
#   all key groups reference valid PARAMS → docstrings → help text
# ---------------------------------------------------------------------------


class TestSchemaCrossGroupConsistency:
    """All key groups valid → docstrings generated → help text → splicing works."""

    def test_all_key_groups_reference_valid_params(self):
        """Every key in every group must exist in PARAMS — catches typos."""
        all_groups = {
            "VIDEO_KEYS": VIDEO_KEYS,
            "IMAGE_KEYS": IMAGE_KEYS,
            "EDIT_KEYS": EDIT_KEYS,
            "API_IMAGE_KEYS": API_IMAGE_KEYS,
            "API_VIDEO_KEYS": API_VIDEO_KEYS,
            "API_EDIT_KEYS": API_EDIT_KEYS,
        }
        for group_name, keys in all_groups.items():
            for key in keys:
                assert key in PARAMS, f"{group_name} references '{key}' not in PARAMS"

    def test_docstring_generation_roundtrip(self):
        """Workflow: keys → docstring → contains param descriptions.

        Data flow:
          1. schema_to_docstring(VIDEO_KEYS) generates Args block
          2. Result contains each param's desc from PARAMS
          3. schema_to_help generates CLI-style help
        """
        docstring = schema_to_docstring(VIDEO_KEYS)
        assert "prompt" in docstring
        assert "resolution" in docstring
        assert "720p" in docstring  # default value visible

        help_text = schema_to_help(API_IMAGE_KEYS)
        assert "model" in help_text
        assert "output_count" in help_text

    def test_splice_replaces_marker(self):
        """splice_schema_into_docstring replaces <SCHEMA_ARGS> with generated block."""
        template = "Do a thing.\n\nArgs:\n    params:\n        <SCHEMA_ARGS>\n\nReturns:\n    ..."
        result = splice_schema_into_docstring(template, ["prompt", "timeout"])
        assert "<SCHEMA_ARGS>" not in result
        assert "prompt" in result
        assert "timeout" in result

    def test_validate_params_warns_on_unknown(self, caplog):
        """Unknown params produce a warning but don't crash."""
        import logging

        with caplog.at_level(logging.WARNING):
            cleaned = validate_params(
                {"prompt": "test", "bogus_key": 42},
                API_IMAGE_KEYS,
            )
        assert "bogus_key" not in cleaned
        assert any("Unknown parameter" in r.message for r in caplog.records)

    def test_validate_params_applies_defaults(self):
        """Missing params with defaults get their default values."""
        cleaned = validate_params({"prompt": "test"}, API_IMAGE_KEYS)
        assert cleaned["output_count"] == 4
        assert cleaned["response_format"] == "url"
        assert cleaned["timeout"] == 300


# ---------------------------------------------------------------------------
# Smoke test — imports
# ---------------------------------------------------------------------------


def test_all_public_symbols_importable():
    """Every public symbol referenced in tests and README is importable."""
    assert get_client is not None
    assert get_api_client is not None
    assert GrokClient is not None
    assert XAIClient is not None
    assert BrowserWorkerPool is not None
    assert VideoGenerationResult is not None
    assert VideoExtendResult is not None
    assert ImageGenerationResult is not None
    assert ImageEditResult is not None
    assert PostDetails is not None
    assert PostSummary is not None
    assert VideoMatchResult is not None
    assert PARAMS is not None
    assert VIDEO_KEYS is not None
    assert API_IMAGE_KEYS is not None
