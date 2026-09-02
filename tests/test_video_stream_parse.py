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
from grok_web.exceptions import GrokAPIError, GrokModerationError


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

    def test_chatmode_userresponse_raises_moderation(self):
        """BR 2026-08: img2vid on a rejected/orphan source comes back in CHAT
        mode (result.userResponse, no streamingVideoGenerationResponse). Must
        raise the typed GrokModerationError with the chat message, NOT a
        confusing generic parse error."""
        body = _ndjson(
            {"result": {"conversation": {"conversationId": "c1"}}},
            {
                "result": {
                    "userResponse": {
                        "responseId": "32427816-787c-4766-973f-614aade744ee",
                        "message": "--mode=normal",
                        "sender": "user",
                    }
                }
            },
        )
        with pytest.raises(GrokModerationError) as exc:
            parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert exc.value.chat_message == "--mode=normal"
        # subclass of GrokAPIError so existing broad catches still work
        assert isinstance(exc.value, GrokAPIError)

    def test_moderated_video_result_still_returns(self):
        """A real streamingVideoGenerationResponse with moderated=True is a
        VALID result (moderated flag), NOT a chat-mode rejection — must return,
        not raise."""
        body = _ndjson(
            {
                "result": {
                    "streamingVideoGenerationResponse": {
                        "videoId": "vid-mod",
                        "progress": 100,
                        "moderated": True,
                    }
                }
            },
        )
        r = parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert r.video_id == "vid-mod" and r.moderated is True

    def test_is_root_user_uploaded_parsed(self):
        """The 2026-09 'uploaded-source' stricter-mod signal: a moderated video
        result carrying isRootUserUploaded=True must surface BOTH flags. Shape
        live-captured 2026-09 (post c775a882 extend -> moderated video
        b0082090, DOM banner 'Generations from uploaded photos have extra
        safeguards'). This is the reliable hard-stop, distinct from the noisy
        bare moderated=True."""
        body = _ndjson(
            {
                "result": {
                    "streamingVideoGenerationResponse": {
                        "videoId": "vid-uploaded-root",
                        "progress": 100,
                        "moderated": True,
                        "isRootUserUploaded": True,
                        "resolutionName": "480p",
                    }
                }
            },
        )
        r = parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert r.video_id == "vid-uploaded-root"
        assert r.moderated is True
        assert r.is_root_user_uploaded is True

    def test_is_root_user_uploaded_defaults_false(self):
        """Absent isRootUserUploaded → False (back-compat: an ordinary moderated
        result is the NOISY case, not a hard stop)."""
        body = _ndjson(
            {
                "result": {
                    "streamingVideoGenerationResponse": {
                        "videoId": "vid-ordinary",
                        "progress": 100,
                        "moderated": True,
                    }
                }
            },
        )
        r = parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert r.moderated is True
        assert r.is_root_user_uploaded is False

    def test_clean_result_is_not_uploaded_root(self):
        """A clean, non-moderated gen leaves both flags False."""
        body = _ndjson(
            {
                "result": {
                    "streamingVideoGenerationResponse": {
                        "videoId": "vid-clean",
                        "progress": 100,
                    }
                }
            },
        )
        r = parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert r.moderated is False
        assert r.is_root_user_uploaded is False

    def test_video_result_wins_even_if_userresponse_present(self):
        """A normal gen echoes the user's message (userResponse) AND streams the
        video — the video result must win, no moderation error."""
        body = _ndjson(
            {"result": {"userResponse": {"responseId": "r0", "message": "hi --mode=custom"}}},
            {
                "result": {
                    "streamingVideoGenerationResponse": {"videoId": "vid-ok", "progress": 100}
                }
            },
        )
        r = parse_video_ndjson_response(body, parent_post_id="p1", statsig_id="s")
        assert r.video_id == "vid-ok"


class TestCDPMonitorMethodFilter:
    def test_defaults_to_any_method(self):
        m = CDPMonitor(tab=None, url_pattern="/app-chat/conversations/")
        assert m.method is None

    def test_method_stored(self):
        m = CDPMonitor(tab=None, url_pattern="/app-chat/conversations/", method="POST")
        assert m.method == "POST"
