"""Unit tests for post enumeration / ancestry APIs (no creds/browser).

Mocks get_post_details / _api_request to exercise the ancestry walk (order,
root, cycle guard) and the client-side media_type filter. Live end-to-end runs
are tests/integration/test_workflows.py::test_ancestry_find_source_image_from_video
and ::test_list_posts_media_type_filter.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from grok_web import GrokClient
from grok_web.models import PostDetails, PostSummary


def _client():
    return GrokClient.__new__(GrokClient)


def _run(coro):
    return asyncio.run(coro)


def _pd(pid: str, parent: str | None) -> PostDetails:
    return PostDetails(id=pid, mode="unknown", original_post_id=parent)


class TestGetPostAncestry:
    def test_walks_to_root_ordered_root_first(self):
        c = _client()
        parents = {"post": "mid", "mid": "root", "root": None}
        c.get_post_details = AsyncMock(side_effect=lambda pid: _pd(pid, parents[pid]))
        out = _run(c.get_post_ancestry("post"))
        assert [p.id for p in out] == ["root", "mid"]  # root first, immediate parent last
        assert all(p.id != "post" for p in out)  # queried post excluded

    def test_root_returns_empty(self):
        c = _client()
        c.get_post_details = AsyncMock(return_value=_pd("root", None))
        assert _run(c.get_post_ancestry("root")) == []

    def test_single_level_video_to_source(self):
        c = _client()
        parents = {"video": "img", "img": None}
        c.get_post_details = AsyncMock(side_effect=lambda pid: _pd(pid, parents[pid]))
        out = _run(c.get_post_ancestry("video"))
        assert [p.id for p in out] == ["img"]  # the source image

    def test_cycle_guard(self):
        c = _client()
        parents = {"a": "b", "b": "a"}  # malformed loop
        c.get_post_details = AsyncMock(side_effect=lambda pid: _pd(pid, parents[pid]))
        out = _run(c.get_post_ancestry("a"))
        assert [p.id for p in out] == ["b"]  # stops when it revisits a seen id

    def test_max_depth_cap(self):
        c = _client()
        # infinite ascending chain n -> n+1
        c.get_post_details = AsyncMock(side_effect=lambda pid: _pd(pid, str(int(pid) + 1)))
        out = _run(c.get_post_ancestry("0", max_depth=3))
        assert len(out) == 3


class TestListPostsMediaTypeFilter:
    def _summary(self, item, include_raw_data=False):
        mt = "MEDIA_POST_TYPE_VIDEO" if str(item["id"]).startswith("v") else "MEDIA_POST_TYPE_IMAGE"
        return PostSummary(id=item["id"], mode="unknown", media_type=mt)

    def test_filters_video_client_side(self):
        c = _client()
        c._api_request = AsyncMock(
            return_value={"posts": [{"id": "v1"}, {"id": "i1"}, {"id": "v2"}], "nextCursor": ""}
        )
        c._parse_post_summary = self._summary
        out = _run(c.list_posts(limit=10, source="favorites", media_type="video"))
        assert [p.id for p in out] == ["v1", "v2"]

    def test_server_body_omits_mediatype(self):
        c = _client()
        c._api_request = AsyncMock(return_value={"posts": [{"id": "i1"}], "nextCursor": ""})
        c._parse_post_summary = self._summary
        _run(c.list_posts(limit=5, source="favorites", media_type="image"))
        body = c._api_request.call_args[0][2]
        # media_type is a CLIENT-side filter — must NOT be sent to the server
        assert "mediaType" not in body.get("filter", {})

    def test_no_filter_returns_all(self):
        c = _client()
        c._api_request = AsyncMock(
            return_value={"posts": [{"id": "v1"}, {"id": "i1"}], "nextCursor": ""}
        )
        c._parse_post_summary = self._summary
        out = _run(c.list_posts(limit=10, source="favorites"))
        assert {p.id for p in out} == {"v1", "i1"}
