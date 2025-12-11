"""Tests for _internal.py utility functions."""

import pytest

from grok_web._internal import (
    PRESET_MAP,
    build_video_payload,
    generate_statsig_id,
    parse_video_ndjson_response,
    resolve_preset,
)
from grok_web.exceptions import GrokAPIError
from grok_web.models import VideoPreset


class TestResolvePreset:
    """Tests for resolve_preset function."""

    def test_resolve_preset_from_enum_normal(self):
        """Resolve VideoPreset.NORMAL enum."""
        result = resolve_preset(VideoPreset.NORMAL)
        assert result == "normal"

    def test_resolve_preset_from_enum_fun(self):
        """Resolve VideoPreset.FUN enum."""
        result = resolve_preset(VideoPreset.FUN)
        assert result == "extremely-crazy"

    def test_resolve_preset_from_enum_spicy(self):
        """Resolve VideoPreset.SPICY enum."""
        result = resolve_preset(VideoPreset.SPICY)
        assert result == "extremely-spicy-or-crazy"

    def test_resolve_preset_from_string_normal(self):
        """Resolve 'normal' string preset."""
        result = resolve_preset("normal")
        assert result == "normal"

    def test_resolve_preset_from_string_fun(self):
        """Resolve 'fun' string preset."""
        result = resolve_preset("fun")
        assert result == "extremely-crazy"

    def test_resolve_preset_from_string_spicy(self):
        """Resolve 'spicy' string preset."""
        result = resolve_preset("spicy")
        assert result == "extremely-spicy-or-crazy"

    def test_resolve_preset_case_insensitive(self):
        """Preset resolution is case-insensitive."""
        assert resolve_preset("NORMAL") == "normal"
        assert resolve_preset("FUN") == "extremely-crazy"
        assert resolve_preset("Spicy") == "extremely-spicy-or-crazy"

    def test_resolve_preset_raw_value_passthrough(self):
        """Unknown preset strings pass through unchanged."""
        result = resolve_preset("extremely-crazy")
        assert result == "extremely-crazy"

    def test_resolve_preset_custom_value(self):
        """Custom values pass through as-is."""
        result = resolve_preset("custom-mode")
        assert result == "custom-mode"


class TestGenerateStatsigId:
    """Tests for generate_statsig_id function."""

    def test_generates_string(self):
        """Returns a string."""
        result = generate_statsig_id()
        assert isinstance(result, str)

    def test_generates_base64(self):
        """Returns valid base64-like string."""
        result = generate_statsig_id()
        # Should only contain base64 characters (no padding = at end)
        valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
        assert all(c in valid_chars for c in result)

    def test_generates_correct_length(self):
        """Returns string of expected length (~94 chars for 70 bytes)."""
        result = generate_statsig_id()
        # 70 bytes -> 94 chars in base64 (without padding)
        assert len(result) >= 90
        assert len(result) <= 100

    def test_generates_unique_values(self):
        """Each call generates different value."""
        results = [generate_statsig_id() for _ in range(10)]
        assert len(set(results)) == 10  # All unique


class TestBuildVideoPayload:
    """Tests for build_video_payload function."""

    def test_basic_payload(self):
        """Build basic video generation payload."""
        result = build_video_payload(
            image_url="https://example.com/image.png",
            parent_post_id="test-post-id",
            mode_value="normal",
        )

        assert result["temporary"] is True
        assert result["modelName"] == "grok-3"
        assert result["message"] == "https://example.com/image.png  --mode=normal"
        assert result["toolOverrides"] == {"videoGen": True}

    def test_payload_with_custom_aspect_ratio(self):
        """Build payload with custom aspect ratio."""
        result = build_video_payload(
            image_url="https://example.com/image.png",
            parent_post_id="test-post-id",
            mode_value="normal",
            aspect_ratio="16:9",
        )

        config = result["responseMetadata"]["modelConfigOverride"]["modelMap"][
            "videoGenModelConfig"
        ]
        assert config["aspectRatio"] == "16:9"

    def test_payload_with_custom_video_length(self):
        """Build payload with custom video length."""
        result = build_video_payload(
            image_url="https://example.com/image.png",
            parent_post_id="test-post-id",
            mode_value="normal",
            video_length=10,
        )

        config = result["responseMetadata"]["modelConfigOverride"]["modelMap"][
            "videoGenModelConfig"
        ]
        assert config["videoLength"] == 10

    def test_payload_includes_parent_post_id(self):
        """Payload includes parent post ID in config."""
        result = build_video_payload(
            image_url="https://example.com/image.png",
            parent_post_id="my-parent-id",
            mode_value="normal",
        )

        config = result["responseMetadata"]["modelConfigOverride"]["modelMap"][
            "videoGenModelConfig"
        ]
        assert config["parentPostId"] == "my-parent-id"

    def test_payload_message_format(self):
        """Message format includes mode flag."""
        result = build_video_payload(
            image_url="https://example.com/image.png",
            parent_post_id="test-post-id",
            mode_value="extremely-crazy",
        )

        assert result["message"] == "https://example.com/image.png  --mode=extremely-crazy"


class TestParseVideoNdjsonResponse:
    """Tests for parse_video_ndjson_response function."""

    def test_parse_successful_response(self):
        """Parse successful video generation response."""
        ndjson = """{"result":{"conversation":{"conversationId":"conv-123"}}}
{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-456","parentPostId":"parent-789","moderated":false,"progress":100,"mode":"normal","modelName":"mochi"}}}}"""

        result = parse_video_ndjson_response(ndjson, "parent-789", "statsig-abc")

        assert result.video_id == "vid-456"
        assert result.parent_post_id == "parent-789"
        assert result.moderated is False
        assert result.progress == 100
        assert result.mode == "normal"
        assert result.model_name == "mochi"
        assert result.conversation_id == "conv-123"
        assert result.statsig_id == "statsig-abc"

    def test_parse_moderated_response(self):
        """Parse moderated video response (empty video_id)."""
        ndjson = """{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"","parentPostId":"parent-123","moderated":true,"progress":0}}}}"""

        result = parse_video_ndjson_response(ndjson, "parent-123", "statsig-xyz")

        assert result.video_id == ""
        assert result.moderated is True
        assert result.progress == 0

    def test_parse_response_with_extra_lines(self):
        """Parse response with blank lines and extra data."""
        ndjson = """{"result":{"conversation":{"conversationId":"conv-1"}}}

{"result":{"response":{"text":"Processing..."}}}
{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-1","moderated":false}}}}
"""

        result = parse_video_ndjson_response(ndjson, "parent", None)

        assert result.video_id == "vid-1"
        assert result.conversation_id == "conv-1"

    def test_parse_missing_video_response_raises(self):
        """Raise error when no video response found."""
        ndjson = """{"result":{"conversation":{"conversationId":"conv-1"}}}
{"result":{"response":{"text":"Still processing..."}}}"""

        with pytest.raises(GrokAPIError, match="Failed to parse video generation response"):
            parse_video_ndjson_response(ndjson, "parent", "statsig")

    def test_parse_empty_response_raises(self):
        """Raise error on empty response."""
        with pytest.raises(GrokAPIError, match="Failed to parse video generation response"):
            parse_video_ndjson_response("", "parent", "statsig")

    def test_parse_invalid_json_lines_skipped(self):
        """Invalid JSON lines are skipped gracefully."""
        ndjson = """not json at all
{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-ok","moderated":false}}}}
also not json"""

        result = parse_video_ndjson_response(ndjson, "parent", "statsig")
        assert result.video_id == "vid-ok"

    def test_parse_uses_last_video_response(self):
        """When multiple video responses, use the last one."""
        ndjson = """{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-1","progress":50}}}}
{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-2","progress":100}}}}"""

        result = parse_video_ndjson_response(ndjson, "parent", "statsig")
        # Last response should be used
        assert result.video_id == "vid-2"
        assert result.progress == 100


class TestPresetMap:
    """Tests for PRESET_MAP constant."""

    def test_preset_map_keys(self):
        """PRESET_MAP has expected keys."""
        assert "normal" in PRESET_MAP
        assert "fun" in PRESET_MAP
        assert "spicy" in PRESET_MAP

    def test_preset_map_values(self):
        """PRESET_MAP has expected values."""
        assert PRESET_MAP["normal"] == "normal"
        assert PRESET_MAP["fun"] == "extremely-crazy"
        assert PRESET_MAP["spicy"] == "extremely-spicy-or-crazy"
