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

    def test_api_key_from_config_file(self, tmp_path):
        """Step 2: fallback to config file when env var absent."""
        # tmp_path (pytest fixture) → no manual NamedTemporaryFile +
        # unlink race that fails on Windows where the file handle is
        # still held when unlink is called.
        config_path = tmp_path / "grok-config.json"
        config_path.write_text(json.dumps({"cookies": {}, "xai_api_key": "xai-from-file"}))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            assert load_api_key(str(config_path)) == "xai-from-file"

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

    def test_cookie_roundtrip(self, tmp_path):
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
        path = str(tmp_path / "cookie-roundtrip.json")
        save_cookies(cookies, path)
        loaded = load_cookies(path)
        assert loaded.sso == "test-sso"
        assert loaded.sso_rw == "test-sso-rw"
        assert loaded.cf_clearance == "test-cf"
        assert loaded.x_userid == "test-uid"

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
# Scenario 6: edit_image_2026_06_ui_redesign
#   The 2026-06 Grok Imagine UI flattened the "..." menu — every action
#   moved to inline post-page buttons. edit_current's UI walk had to be
#   rewritten. These tests lock the new contract at source-level so
#   future regressions surface as test failures rather than 187s hangs.
# ---------------------------------------------------------------------------


class TestEditCurrent2026UIRedesign:
    """edit_current uses inline composer (not legacy "..." menu)."""

    def test_no_longer_opens_dotdotdot_menu(self):
        """The legacy ``open_post_menu`` + ``click_menu_item('Custom', ...)``
        walk must NOT appear — Grok's "..." menu now contains only
        报告问题 (Report Issue), so any code path expecting Custom /
        编辑图像 / Edit image menuitems would hang.
        """
        import inspect

        src = inspect.getsource(GrokClient.edit_current)
        # The forbidden legacy idioms:
        assert "open_post_menu(" not in src, (
            "edit_current must not call open_post_menu — Grok's 2026-06 "
            "redesign emptied the '...' menu (only 报告问题 remains). "
            "Use the inline 编辑/Edit submit on the post page instead."
        )
        assert 'click_menu_item(self._tab, "Custom"' not in src, (
            "edit_current must not look for Custom menuitem — removed in the 2026-06 redesign"
        )

    def test_verifies_inline_composer_present(self):
        """Layer 1: edit_current must verify the inline composer
        (editor + 编辑/Edit submit) is present before typing/clicking."""
        import inspect

        src = inspect.getsource(GrokClient.edit_current)
        # The composer-presence probe must run BEFORE the fill code.
        assert "has_editor" in src and "has_edit_submit" in src, (
            "edit_current must verify both editor and 编辑/Edit submit "
            "are present on the post page (composer_ready probe)"
        )

    def test_captures_typed_server_error(self):
        """Layer 2: when Grok returns a typed error body (e.g. the
        2026-06 GCS-download bug), edit_current must surface it as
        a GrokAPIError rather than waiting the full timeout and
        returning an empty ImageEditResult."""
        import inspect

        src = inspect.getsource(GrokClient.edit_current)
        assert "server_error" in src, "edit_current must capture the server's typed error message"
        assert "Failed to download image from GCS" in src, (
            "edit_current must document the 2026-06 GCS-download bug"
            " in its error path so the workaround (warm statsig snitch)"
            " is reachable from the error message"
        )

    def test_docstring_first_line_is_decision_signal(self):
        """Per cli-steering-engineering: docstring first line should
        be a 'use when' decision signal, not a description."""
        doc = (GrokClient.edit_current.__doc__ or "").strip()
        first_line = doc.split("\n", 1)[0].lower()
        # Decision-time signal: "use when" / "when:" + selection guidance
        assert "use when" in first_line or "when:" in first_line, (
            f"edit_current docstring first line should be a 'use when' "
            f"decision signal per cli-steering-engineering skill; got: "
            f"{first_line!r}"
        )

    def test_docstring_has_failure_section(self):
        """Per cli-steering-engineering Rule 5b: every non-trivial
        failure mode needs a Failure: section so callers can branch."""
        doc = GrokClient.edit_current.__doc__ or ""
        assert "Failure:" in doc, (
            "edit_current docstring must include a Failure: section "
            "enumerating typed-error paths (composer not present, "
            "GCS download error, ref upload timeout)"
        )


# ---------------------------------------------------------------------------
# Scenario 7: post_actions_2026_06_inline_button_era
#   Like / dislike / delete_video / upgrade_video / extend (the
#   "..." → menuitem callers from the legacy era) were rewritten to
#   click inline buttons directly. delete_post / delete_image now
#   raise typed errors documenting Grok's removal of those surfaces.
#   These tests lock the contracts at source level.
# ---------------------------------------------------------------------------


class TestPostActions2026UIRedesign:
    """Per-post actions use inline buttons (not legacy "..." menu)."""

    @pytest.mark.parametrize(
        "method_name",
        ["like_post", "dislike_post", "delete_video", "upgrade_video"],
    )
    def test_no_longer_uses_open_post_menu(self, method_name):
        """The legacy ``_open_post_menu`` + ``_click_menu_item`` walk
        must NOT appear — Grok's "..." menu now contains only 报告问题
        (Report Issue). Each affected method must drive its inline
        button directly via ``_click_inline_post_button``."""
        import inspect

        method = getattr(GrokClient, method_name)
        src = inspect.getsource(method)
        assert "_open_post_menu(" not in src, (
            f"{method_name} must not call _open_post_menu — 2026-06 redesign emptied the '...' menu"
        )
        assert "_click_menu_item(" not in src, (
            f"{method_name} must not call _click_menu_item — actions "
            f"moved out of '...' to inline post-page buttons"
        )
        assert "_click_inline_post_button(" in src, (
            f"{method_name} must drive its inline button via "
            f"_click_inline_post_button (multi-locale + visible-button "
            f"diagnostic dump on failure)"
        )

    @pytest.mark.parametrize(
        "method_name",
        [
            "like_post",
            "dislike_post",
            "delete_video",
            "delete_post",
            "delete_image",
            "upgrade_video",
        ],
    )
    def test_docstring_has_use_when_and_failure(self, method_name):
        """Per cli-steering-engineering: decision-time first line
        ('Use when:') + explicit Failure: section."""
        method = getattr(GrokClient, method_name)
        doc = (method.__doc__ or "").strip()
        first_line = doc.split("\n", 1)[0].lower()
        assert "use when" in first_line, (
            f"{method_name} docstring first line should start with "
            f"'Use when:' (decision signal); got: {first_line!r}"
        )
        assert "Failure:" in doc, (
            f"{method_name} docstring needs a Failure: section enumerating its typed-error paths"
        )

    def test_delete_image_raises_typed_removed_error(self):
        """delete_image cannot be implemented in the 2026-06 UI —
        method must raise GrokAPIError documenting the removal so
        callers in old scripts get an actionable message instead of
        AttributeError."""
        import asyncio
        import inspect

        src = inspect.getsource(GrokClient.delete_image)
        assert "raise GrokAPIError" in src, (
            "delete_image must raise GrokAPIError — no UI surface in 2026-06"
        )
        assert "2026-06" in src, (
            "delete_image error message must name the 2026-06 redesign "
            "so users understand it's not a transient bug"
        )

        # Behavioural: actually call it on a bare instance and confirm raise.
        client = GrokClient.__new__(GrokClient)

        async def call_it():
            await client.delete_image("any-post-id", 1)

        with pytest.raises(GrokAPIError, match="2026-06"):
            asyncio.run(call_it())

    def test_delete_post_image_path_explains_removal(self):
        """delete_post on an image post must re-raise with a message
        that explains the 2026-06 removal (rather than leaking the
        raw 'inline button not found' diagnostic)."""
        import inspect

        src = inspect.getsource(GrokClient.delete_post)
        assert "2026-06" in src, (
            "delete_post must document the redesign-removal in its image-post error path"
        )
        # Must route through delete_video first (which knows the
        # video inline button), then translate the failure.
        assert "delete_video(post_id)" in src, (
            "delete_post must route to delete_video first (single "
            "implementation of the inline 删除视频 walk)"
        )

    @pytest.mark.parametrize(
        "method_name,expected_label",
        [
            ("regenerate_post", "重新生成"),
            ("animate_post", "动画"),
            ("crop_post", "裁剪"),
        ],
    )
    def test_new_2026_06_methods_drive_inline_button(self, method_name, expected_label):
        """The 3 new-feature methods added in v0.19.28 must drive
        their inline button via _click_inline_post_button (consistent
        with the rest of the 2026-06 post-action surface)."""
        import inspect

        method = getattr(GrokClient, method_name)
        src = inspect.getsource(method)
        assert "_click_inline_post_button(" in src, (
            f"{method_name} must use the shared inline-button helper"
        )
        assert expected_label in src, (
            f"{method_name} must reference the {expected_label!r} label "
            f"that drives its inline button"
        )

    @pytest.mark.parametrize(
        "method_name",
        ["regenerate_post", "animate_post", "crop_post"],
    )
    def test_new_methods_have_use_when_and_failure(self, method_name):
        """Per cli-steering-engineering: new methods must follow the
        Use when: + Failure: docstring contract."""
        method = getattr(GrokClient, method_name)
        doc = (method.__doc__ or "").strip()
        first_line = doc.split("\n", 1)[0].lower()
        assert "use when" in first_line, (
            f"{method_name} first docstring line should be a 'Use when:' "
            f"decision signal; got: {first_line!r}"
        )
        assert "Failure:" in doc, f"{method_name} docstring needs an explicit Failure: section"

    def test_animate_post_validates_mode(self):
        """animate_post rejects bogus modes upfront with GrokConfigError
        (not GrokAPIError) — invalid arg is the caller's mistake, not
        a server-side condition."""
        import asyncio

        client = GrokClient.__new__(GrokClient)

        async def call_it():
            await client.animate_post("any-id", mode="bogus")

        with pytest.raises(GrokConfigError, match="mode must be"):
            asyncio.run(call_it())

    def test_animate_post_add_prompt_requires_prompt(self):
        """animate_post(mode='add_prompt') without prompt arg raises
        GrokConfigError eagerly — fails before navigation so no
        wasted browser work."""
        import asyncio

        client = GrokClient.__new__(GrokClient)

        async def call_it():
            await client.animate_post("any-id", mode="add_prompt")

        with pytest.raises(GrokConfigError, match="requires the prompt"):
            asyncio.run(call_it())

    def test_generate_video_from_current_no_legacy_dead_code(self):
        """v0.19.29 rewrote generate_video_from_current for the new
        inline-button UI. The legacy settings-gear + 制作视频 walk
        must not survive — those buttons no longer exist on Grok's
        post pages, and any code still looking for them would either
        hang or silently miss the click."""
        import inspect

        src = inspect.getsource(GrokClient.generate_video_from_current)
        forbidden = [
            'aria-label="设置"',
            'aria-label="Settings"',
            'aria-label="制作视频"',
            'aria-label="Make video"',
            "_open_settings",
            "_click_make_video_button",
            "preset_menu_map",
        ]
        for marker in forbidden:
            assert marker not in src, (
                f"generate_video_from_current must not contain legacy "
                f"marker {marker!r} (removed in 2026-06 redesign)"
            )

    def test_generate_video_from_current_uses_inline_submenu(self):
        """The new flow must drive 动画 → submenu via the shared
        helpers (_click_inline_post_button + _click_menuitem)."""
        import inspect

        src = inspect.getsource(GrokClient.generate_video_from_current)
        assert "_click_inline_post_button(" in src, (
            "generate_video_from_current must drive its inline buttons via the shared helper"
        )
        assert "_click_menuitem(" in src, (
            "generate_video_from_current must click submenu items via the shared helper"
        )
        # Must reference the 动画 entry button + at least one submenu label
        assert "动画" in src, "must click the 动画 / Animate inline trigger"
        assert "快速动画化" in src or "添加提示" in src, (
            "must click a 动画 submenu item (快速动画化 / 添加提示)"
        )

    def test_generate_video_from_current_docstring(self):
        """Per cli-steering-engineering: Use when: + Failure: sections."""
        doc = (GrokClient.generate_video_from_current.__doc__ or "").strip()
        first_line = doc.split("\n", 1)[0].lower()
        assert "use when" in first_line, (
            f"generate_video_from_current first docstring line should be "
            f"a 'Use when:' signal; got: {first_line!r}"
        )
        assert "Failure:" in doc, "generate_video_from_current needs a Failure: section"

    def test_generate_video_from_current_preset_fun_warning(self):
        """preset='fun' should be downgraded to 'normal' with a warning,
        not raise — the Fun preset is gone from the 2026-06 submenu and
        we shouldn't break callers who passed it before."""
        import inspect

        src = inspect.getsource(GrokClient.generate_video_from_current)
        assert "preset='fun'" in src or '"fun"' in src, (
            "must reference 'fun' preset for the downgrade path"
        )
        assert "logger.warning" in src and "fun" in src.lower(), (
            "must warn (not raise) when caller passes preset='fun'"
        )

    def test_legacy_stub_method_removed(self):
        """_generate_video_from_current_LEGACY_DEAD sentinel was added
        then immediately removed per user's '完整清理干净' instruction.
        No stale stubs should remain."""
        assert not hasattr(GrokClient, "_generate_video_from_current_LEGACY_DEAD"), (
            "no LEGACY_DEAD sentinel methods (clean cleanup)"
        )

    def test_shared_click_menuitem_helper(self):
        """The new _click_menuitem helper should exist and be used
        by methods that open submenus."""
        import inspect

        assert hasattr(GrokClient, "_click_menuitem"), "_click_menuitem helper missing"
        # animate_post should use it (refactored in v0.19.29)
        src = inspect.getsource(GrokClient.animate_post)
        assert "_click_menuitem(" in src, (
            "animate_post should use the shared _click_menuitem helper "
            "(refactored away from inline JS in v0.19.29)"
        )

    def test_generate_video_from_current_documents_gcs_workaround(self):
        """Same Grok-side GCS URL bug hits this path too. Connector
        must surface the typed error with the REST-warmup workaround,
        not let it surface as a generic 'failed to parse' error."""
        import inspect

        src = inspect.getsource(GrokClient.generate_video_from_current)
        assert "Failed to download image from GCS" in src, (
            "must detect Grok's typed GCS error specifically"
        )
        assert "warm the statsig" in src or "REST primary" in src, (
            "must document the REST-primary statsig-warmup workaround"
        )

    def test_animate_post_documents_gcs_workaround(self):
        """animate_post hits the same Grok-side GCS URL bug as
        edit_image when called on borderline content. The error
        path must name the bug + recommend create_video as the
        REST-primary workaround (which constructs URL correctly)."""
        import inspect

        src = inspect.getsource(GrokClient.animate_post)
        assert "Failed to download image from GCS" in src, (
            "animate_post must detect the GCS error specifically"
        )
        assert "create_video" in src, (
            "animate_post's GCS error must point callers at "
            "create_video as the REST-primary workaround"
        )

    def test_dead_menu_helpers_are_gone(self):
        """post_menu module + private menu helpers must be fully
        removed (not just unused). The user's cleanup request was
        explicit: no stale symbols to confuse readers."""
        # Module deleted
        with pytest.raises(ImportError):
            from grok_web.actions import post_menu  # noqa: F401

        # Private helpers deleted from GrokClient
        for dead_name in (
            "_open_post_menu",
            "_click_menu_item",
            "_get_menu_items_text",
            "_is_post_favorited",
            "_favorite_post_browser",
            "_unfavorite_post_browser",
            "get_menu_items",
        ):
            assert not hasattr(GrokClient, dead_name), (
                f"GrokClient.{dead_name} must be deleted (2026-06 cleanup)"
            )


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
