"""Unit tests for the References API (no creds/browser).

Mocks GrokClient._api_request so we exercise create/list/delete logic:
category->kind mapping, in-Grok asset-id resolution, return shapes, and the
error paths — all without hitting Grok. The live REST calls themselves are
verified separately in workbench.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from grok_web import GrokClient
from grok_web.exceptions import GrokAPIError, GrokNotFoundError


def _client():
    c = GrokClient.__new__(GrokClient)  # skip __init__ (no browser/config needed)
    return c


def _run(coro):
    return asyncio.run(coro)


CREATE_RESP = {
    "reference": {
        "id": "ref-123",
        "kind": "MEDIA_REFERENCE_KIND_CHARACTER",
        "name": "hero",
        "description": "",
        "assets": [{"assetId": "img-abc", "mimeType": "image/jpeg", "url": "https://x/y.jpg"}],
    }
}


class TestCreateReference:
    def test_post_source_in_grok(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        out = _run(c.create_reference({"images": ["post:img-abc"], "title": "hero"}))
        method, ep, body = c._api_request.call_args[0]
        assert ep == "/rest/media/reference/create"
        assert body == {
            "kind": "MEDIA_REFERENCE_KIND_CHARACTER",
            "name": "hero",
            "assetIds": ["img-abc"],
        }
        assert out == {
            "reference_id": "ref-123",
            "name": "hero",
            "category": "character",
            "asset_ids": ["img-abc"],
        }

    def test_bare_uuid_source(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        _run(
            c.create_reference(
                {"images": ["9b2d97a6-ffd9-4af1-8936-a485668c96f1"], "title": "hero"}
            )
        )
        body = c._api_request.call_args[0][2]
        assert body["assetIds"] == ["9b2d97a6-ffd9-4af1-8936-a485668c96f1"]

    def test_category_maps_to_kind(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        _run(c.create_reference({"images": ["post:a"], "title": "t", "category": "outfit"}))
        assert c._api_request.call_args[0][2]["kind"] == "MEDIA_REFERENCE_KIND_OUTFIT"

    def test_description_included_when_set(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        _run(c.create_reference({"images": ["post:a"], "title": "t", "description": "d"}))
        assert c._api_request.call_args[0][2]["description"] == "d"

    def test_local_path_rejected(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        with pytest.raises(GrokAPIError, match="local file paths are not supported"):
            _run(c.create_reference({"images": ["./face.png"], "title": "t"}))

    def test_video_source_rejected(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        with pytest.raises(GrokAPIError, match="video:"):
            _run(c.create_reference({"images": ["video:abc"], "title": "t"}))

    def test_missing_images_and_title(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        with pytest.raises(GrokAPIError, match="images"):
            _run(c.create_reference({"title": "t"}))
        with pytest.raises(GrokAPIError, match="title"):
            _run(c.create_reference({"images": ["post:a"]}))

    def test_bad_category(self):
        c = _client()
        c._api_request = AsyncMock(return_value=CREATE_RESP)
        with pytest.raises(GrokAPIError, match="category"):
            _run(c.create_reference({"images": ["post:a"], "title": "t", "category": "nope"}))


class TestListReferences:
    def test_parses_list(self):
        c = _client()
        c._api_request = AsyncMock(
            return_value={
                "references": [
                    {
                        "id": "r1",
                        "kind": "MEDIA_REFERENCE_KIND_SCENE",
                        "name": "beach",
                        "assets": [{"assetId": "a1"}, {"assetId": "a2"}],
                    },
                ]
            }
        )
        out = _run(c.list_references(limit=10))
        assert c._api_request.call_args[0][1] == "/rest/media/reference/list"
        assert c._api_request.call_args[0][2] == {"limit": 10}
        assert isinstance(out, list)
        assert out[0] == {
            "reference_id": "r1",
            "name": "beach",
            "category": "scene",
            "asset_ids": ["a1", "a2"],
        }

    def test_empty(self):
        c = _client()
        c._api_request = AsyncMock(return_value={})
        assert _run(c.list_references()) == []


class TestDeleteReference:
    def test_delete_ok(self):
        c = _client()
        c._api_request = AsyncMock(return_value={})
        assert _run(c.delete_reference("ref-123")) is True
        assert c._api_request.call_args[0] == (
            "POST",
            "/rest/media/reference/delete",
            {"id": "ref-123"},
        )

    def test_delete_idempotent_on_404(self):
        c = _client()
        c._api_request = AsyncMock(side_effect=GrokNotFoundError("not found"))
        assert _run(c.delete_reference("gone")) is True
