"""Regression tests for video NDJSON parsing across the 2026-07 endpoint
split (no creds/browser).

Two live-captured shapes must both parse:

  * /conversations/new (txt2vid, upload2vid) NESTS the payload under
    ``result.response.streamingVideoGenerationResponse``.
  * /conversations/{id}/responses (img2vid-from-post, video-extend) HOISTS
    it onto ``result.streamingVideoGenerationResponse`` — same migration
    edit_image hit.

Also locks the CDPMonitor optional HTTP-method filter used to catch the
/responses POST submit without latching onto the same-prefix GET hydration.
"""

from __future__ import annotations

import json

import pytest

from grok_web._internal import parse_video_ndjson_response
from grok_web.actions.network_monitor import CDPMonitor
from grok_web.exceptions import GrokAPIError


def _ndjson(*objs: dict) -> str:
    return "\n".join(json.dumps(o) for o in objs)


class TestParseVideoNdjson:
    def test_nested_legacy_shape(self):
        """result.response.streamingVideoGenerationResponse (/conversations/new)."""
        body = _ndjson(
            {"result": {"conversation": {"conversationId": "c1"}}},
            {
                "result": {
                    "response": {
                        "streamingVideoGenerationResponse": {
                            "videoId": "vid-nested",
                            "progress": 100,
                            "moderated": False,
                        }
                    }
                }
            },
        )
        r = parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert r.video_id == "vid-nested"

    def test_flat_2026_07_responses_shape(self):
        """result.streamingVideoGenerationResponse (/conversations/{id}/responses)."""
        body = _ndjson(
            {
                "result": {
                    "streamingVideoGenerationResponse": {"videoId": "vid-hoisted", "progress": 50}
                }
            },
            {
                "result": {
                    "streamingVideoGenerationResponse": {
                        "videoId": "vid-hoisted",
                        "progress": 100,
                        "moderated": False,
                    }
                }
            },
        )
        r = parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert r.video_id == "vid-hoisted"

    def test_no_video_response_raises(self):
        body = _ndjson(
            {"result": {"conversation": {"conversationId": "c1"}}},
            {"result": {"response": {"token": "hi"}}},
        )
        with pytest.raises(GrokAPIError):
            parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")


class TestCDPMonitorMethodFilter:
    def test_defaults_to_any_method(self):
        m = CDPMonitor(tab=None, url_pattern="/app-chat/conversations/")
        assert m.method is None

    def test_method_stored(self):
        m = CDPMonitor(tab=None, url_pattern="/app-chat/conversations/", method="POST")
        assert m.method == "POST"
