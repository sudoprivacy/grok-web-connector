"""Unit tests for get_segments (no creds/browser).

Mocks _api_request to exercise asset-id resolution, request payload, and
response parsing. The live end-to-end run is
tests/integration/test_workflows.py::test_segment_generated_image.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from grok_web import GrokClient
from grok_web.exceptions import GrokAPIError


def _client():
    return GrokClient.__new__(GrokClient)


def _run(coro):
    return asyncio.run(coro)


SEG_RESP = {
    "cached": False,
    "map": {
        "objects": [
            {
                "name": "red apple",
                "boxXyxy": [124.2, 342.2, 490.8, 709.8],
                "score": 0.97,
                "maskUrl": "",
                "maskRle": {"size": [1152, 768], "counts": "abc"},
            },
            {
                "name": "white surface",
                "boxXyxy": [0.0, 0.0, 768.0, 1152.0],
                "score": 0.80,
                "maskUrl": "https://x/mask.png",
                "maskRle": {"size": [1152, 768], "counts": "def"},
            },
        ]
    },
}


class TestGetSegments:
    def test_post_source_payload_and_parse(self):
        c = _client()
        c._api_request = AsyncMock(return_value=SEG_RESP)
        out = _run(c.get_segments("post:img-abc"))
        method, ep, body = c._api_request.call_args[0]
        assert ep == "/rest/media/segment"
        assert body == {"assetId": "img-abc", "cachedOnly": False, "maskFormat": "rle"}
        assert out == [
            {
                "name": "red apple",
                "box": [124.2, 342.2, 490.8, 709.8],
                "score": 0.97,
                "mask_rle": {"size": [1152, 768], "counts": "abc"},
                "mask_url": None,
            },
            {
                "name": "white surface",
                "box": [0.0, 0.0, 768.0, 1152.0],
                "score": 0.80,
                "mask_rle": {"size": [1152, 768], "counts": "def"},
                "mask_url": "https://x/mask.png",
            },
        ]

    def test_bare_uuid_source(self):
        c = _client()
        c._api_request = AsyncMock(return_value=SEG_RESP)
        _run(c.get_segments("9b2d97a6-ffd9-4af1-8936-a485668c96f1"))
        assert c._api_request.call_args[0][2]["assetId"] == "9b2d97a6-ffd9-4af1-8936-a485668c96f1"

    def test_cached_only_flag(self):
        c = _client()
        c._api_request = AsyncMock(return_value=SEG_RESP)
        _run(c.get_segments("post:x", cached_only=True))
        assert c._api_request.call_args[0][2]["cachedOnly"] is True

    def test_local_path_rejected(self):
        c = _client()
        c._api_request = AsyncMock(return_value=SEG_RESP)
        with pytest.raises(GrokAPIError, match="in-Grok image id"):
            _run(c.get_segments("./photo.png"))

    def test_empty_map(self):
        c = _client()
        c._api_request = AsyncMock(return_value={"cached": True, "map": {}})
        assert _run(c.get_segments("post:x")) == []
