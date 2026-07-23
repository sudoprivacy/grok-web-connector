"""Regression tests for the edit-image stream capture (no creds/browser).

Locks in the 2026-07 Grok changes that broke edit_image:

  * The inline edit composer submits to
    ``/rest/app-chat/conversations/{id}/responses`` instead of
    ``/conversations/new`` — both must be recognized as edit-stream URLs.
  * That endpoint hoists the image payload directly onto ``result``
    (``result.streamingImageGenerationResponse``) instead of nesting it
    under ``result.response`` — the parser must handle both.

If either regresses, edit_current silently captures 0 images again.
"""

from __future__ import annotations

from grok_web.client import _ingest_edit_image_line, _is_edit_stream_url


class TestEditStreamUrl:
    def test_matches_legacy_new_endpoint(self):
        assert _is_edit_stream_url("https://grok.com/rest/app-chat/conversations/new")

    def test_matches_2026_07_responses_endpoint(self):
        assert _is_edit_stream_url(
            "https://grok.com/rest/app-chat/conversations/"
            "bb1767c1-d012-4008-8b76-83f672ea4dd4/responses"
        )
        # trailing slash tolerated
        assert _is_edit_stream_url("https://grok.com/rest/app-chat/conversations/abc/responses/")

    def test_ignores_unrelated_endpoints(self):
        assert not _is_edit_stream_url("https://grok.com/rest/app-chat/conversations")
        assert not _is_edit_stream_url("https://grok.com/rest/app-chat/conversations-many")
        assert not _is_edit_stream_url("https://grok.com/rest/media/imagine/quota_info")


class TestIngestEditImageLine:
    def test_flat_2026_07_shape(self):
        """result.streamingImageGenerationResponse (new /responses endpoint)."""
        images: dict = {}
        _ingest_edit_image_line(
            {
                "result": {
                    "streamingImageGenerationResponse": {
                        "imageId": "img-A",
                        "imageUrl": "users/u/generated/img-A/image.jpg",
                        "progress": 100,
                        "moderated": False,
                        "imageModel": "imagine-image-edit",
                    }
                }
            },
            images,
        )
        assert images["img-A"] == {
            "image_id": "img-A",
            "post_id": "img-A",
            "image_url": "users/u/generated/img-A/image.jpg",
            "moderated": False,
            "progress": 100,
        }

    def test_nested_legacy_shape(self):
        """result.response.streamingImageGenerationResponse (/conversations/new)."""
        images: dict = {}
        _ingest_edit_image_line(
            {
                "result": {
                    "response": {
                        "streamingImageGenerationResponse": {
                            "imageId": "img-B",
                            "imageUrl": "users/u/generated/img-B.jpg",
                            "progress": 50,
                        }
                    }
                }
            },
            images,
        )
        assert images["img-B"]["progress"] == 50
        assert images["img-B"]["post_id"] == "img-B"

    def test_later_line_overwrites_with_final_status(self):
        images: dict = {}
        base = {"imageId": "x", "imageUrl": "u.jpg"}
        _ingest_edit_image_line(
            {"result": {"streamingImageGenerationResponse": {**base, "progress": 50}}}, images
        )
        _ingest_edit_image_line(
            {
                "result": {
                    "streamingImageGenerationResponse": {
                        **base,
                        "progress": 100,
                        "moderated": True,
                    }
                }
            },
            images,
        )
        assert images["x"]["progress"] == 100
        assert images["x"]["moderated"] is True

    def test_non_image_lines_ignored(self):
        images: dict = {}
        _ingest_edit_image_line({"result": {"conversation": {"conversationId": "c1"}}}, images)
        _ingest_edit_image_line({"result": {"response": {}}}, images)
        _ingest_edit_image_line({"result": {}}, images)
        _ingest_edit_image_line({}, images)
        assert images == {}

    def test_missing_image_id_skipped(self):
        images: dict = {}
        _ingest_edit_image_line(
            {"result": {"streamingImageGenerationResponse": {"imageUrl": "u.jpg"}}}, images
        )
        assert images == {}
