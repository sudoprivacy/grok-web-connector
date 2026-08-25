"""Unit tests for the Image 2.0 (2026-08) UI handling (no creds/browser).

Covers the submit-disabled classifier's EMPTY-prompt path — the FR fix so an
empty composer (UI-not-ready / broken prompt-fill) is NOT misreported as an
hourly throttle (which sends users to wait 24h for nothing). Live checks
(quality tier selection, create_image with quality='v2') run against the real
UI in workbench probes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from grok_web import GrokClient
from grok_web.exceptions import GrokAPIError, GrokRateLimitError


def _client():
    return GrokClient.__new__(GrokClient)


def _run(coro):
    return asyncio.run(coro)


def _disabled_state():
    return {
        "submit_disabled": True,
        "banners": [],
        "candidate_messages": [],
        "submit_aria": "提交",
        "submit_text": "",
    }


class TestClassifySubmitBlock:
    def test_empty_prompt_is_not_throttle(self):
        c = _client()
        c._probe_submit_state = AsyncMock(return_value=_disabled_state())
        c._tab = MagicMock()
        c._tab.evaluate = AsyncMock(return_value="")  # prompt editor empty
        err = _run(c._classify_submit_block(action="create_image"))
        assert isinstance(err, GrokAPIError)
        assert not isinstance(err, GrokRateLimitError), "empty prompt must NOT be a rate-limit"
        assert "EMPTY" in str(err) and "not a throttle" in str(err).lower()

    def test_nonempty_prompt_disabled_is_throttle(self):
        c = _client()
        c._probe_submit_state = AsyncMock(return_value=_disabled_state())
        c._tab = MagicMock()
        c._tab.evaluate = AsyncMock(return_value="a real prompt")  # prompt present
        err = _run(c._classify_submit_block(action="create_image"))
        assert isinstance(err, GrokRateLimitError), "disabled + prompt present → throttle verdict"

    def test_submit_enabled_returns_none(self):
        c = _client()
        c._probe_submit_state = AsyncMock(return_value={"submit_disabled": False})
        assert _run(c._classify_submit_block(action="create_image")) is None

    def test_quota_banner_takes_precedence_over_empty_prompt(self):
        c = _client()
        st = _disabled_state()
        st["banners"] = ["You have reached your daily quota"]
        c._probe_submit_state = AsyncMock(return_value=st)
        c._tab = MagicMock()
        c._tab.evaluate = AsyncMock(return_value="")  # even if empty, quota wins
        from grok_web.exceptions import GrokQuotaExceededError

        err = _run(c._classify_submit_block(action="create_image"))
        assert isinstance(err, GrokQuotaExceededError)
