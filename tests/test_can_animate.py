"""Unit tests for can_animate() — the img2vid pre-flight diagnostic.

The '制作视频' / Make Video button is profile/account-scoped (present for a post
on one browser profile, absent on a flagged/degraded one). can_animate() loads
the post read-only and reports button presence so callers can gate a batch
instead of 100%-failing into GrokModerationError. Live-verified: True for a real
post on a healthy profile (tests/integration). These pin the routing + the
True/False/404 branches without a browser.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from grok_web import GrokClient
from grok_web.exceptions import GrokAPIError


def _run(coro):
    return asyncio.run(coro)


def _client_with_button(present: bool):
    c = GrokClient.__new__(GrokClient)
    c._navigate_to_post_safe = AsyncMock(return_value=None)

    class _Tab:
        async def evaluate(self, *a, **k):
            return present  # the button-presence JS boolean

    c._tab = _Tab()
    return c


class TestCanAnimate:
    def test_button_present_returns_true(self):
        assert _run(_client_with_button(True).can_animate("p1")) is True

    def test_button_absent_returns_false(self):
        assert _run(_client_with_button(False).can_animate("p1")) is False

    def test_coerces_truthy_to_bool(self):
        c = _client_with_button(present=1)  # JS could return 1/0
        out = _run(c.can_animate("p1"))
        assert out is True and isinstance(out, bool)

    def test_404_propagates(self):
        c = GrokClient.__new__(GrokClient)
        c._navigate_to_post_safe = AsyncMock(side_effect=GrokAPIError("Post p1 not found (404)"))
        with pytest.raises(GrokAPIError, match="not found"):
            _run(c.can_animate("p1"))
