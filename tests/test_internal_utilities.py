"""Tests for _internal.py utility functions."""

import pytest

from grok_web._internal import (
    parse_video_ndjson_response,
)
from grok_web.exceptions import GrokAPIError


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
