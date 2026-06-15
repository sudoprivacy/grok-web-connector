"""Regression: silent server-side throttle must classify as
GrokRateLimitError, not generic GrokAPIError.

Reporter pattern (v0.19.24, 2026-06): after ~3000 create_image calls in
one session, Grok's hourly anti-abuse throttle disables the submit
button without rendering any banner / dialog / Turnstile. The
connector's submit-click-fail site raised a generic ``GrokAPIError``
with an "overlay intercepted" message, which:

  1. Misled users into chasing a non-existent overlay.
  2. Did NOT trip downstream pools'
     ``matches_exception(r, GrokRateLimitError)`` early-exit, so the
     pool kept retrying — and retrying during the cooldown ESCALATED
     the hourly throttle to a multi-hour Turnstile hard-flag.

Fix: probe submit state before raising; if submit became disabled (any
keyword match OR silent), raise ``GrokRateLimitError``. Generic
overlay error only fires when submit is GENUINELY still enabled.

These tests are pure unit-level (no browser) — they monkeypatch
``_probe_submit_state`` and inspect what the classifier returns.
"""

from __future__ import annotations

import inspect

import pytest

from grok_web import GrokQuotaExceededError, GrokRateLimitError
from grok_web.client import GrokClient
from grok_web.exceptions import GrokAPIError


def _classifier_source() -> str:
    return inspect.getsource(GrokClient._classify_submit_block)


def _create_image_source() -> str:
    return inspect.getsource(GrokClient.create_image)


# ---------------------------------------------------------------------
# Source-level contracts: both fail-fast sites must go through the
# shared classifier, and the classifier must be the ONLY place that
# decides "submit disabled but no banner = ???".
# ---------------------------------------------------------------------


def test_submit_click_fail_uses_classifier():
    """The 'no WS frames within 30s' site must call _classify_submit_block."""
    src = _create_image_source()
    # The fail-fast site is identifiable by the wait_first variable —
    # it's the only place using it. (Substring 'WebSocket' alone is
    # used elsewhere in the file, so it's not specific enough.)
    assert "wait_first" in src, (
        "expected to find the post-submit 'no WS frames' fail-fast site "
        "(identified by the wait_first variable) in create_image"
    )
    assert "_classify_submit_block(action=" in src, (
        "create_image must call _classify_submit_block before raising "
        "the overlay-hint error; otherwise silent server-side throttle "
        "stays mis-typed as a generic GrokAPIError"
    )


def test_scroll_loop_uses_classifier_too():
    """The mid-scroll fail-fast site must also use the classifier.

    Keeps the silent-throttle classification consistent regardless of
    whether the throttle hits at submit-time or mid-scroll.
    """
    src = _create_image_source()
    # The shared classifier should appear at least twice in
    # create_image (once for each fail-fast site).
    occurrences = src.count("_classify_submit_block(action=")
    assert occurrences >= 2, (
        f"_classify_submit_block should be called from BOTH the "
        f"submit-click-fail site AND the scroll-loop fail-fast site; "
        f"found {occurrences} call(s)"
    )


def test_classifier_returns_ratelimit_for_silent_disabled():
    """Source-level proof: silent-disabled branch returns RateLimit."""
    src = _classifier_source()
    # The last branch (no banner / candidate keyword match) must NOT
    # raise generic GrokAPIError — that was the old behaviour.
    assert "GrokAPIError(" not in src or "return GrokRateLimitError" in src, (
        "_classify_submit_block must not return a generic GrokAPIError "
        "for the silent-disabled case — that re-introduces the bug"
    )
    # Must explicitly return RateLimit when silent-disabled with no
    # keyword match. The relevant phrase from the implementation:
    assert "silently disable" in src.lower() or (
        "no visible banner" in src.lower() and "GrokRateLimitError" in src
    ), (
        "_classify_submit_block must document and return "
        "GrokRateLimitError for the silent-disabled case"
    )


# ---------------------------------------------------------------------
# Behavioural mock tests — exercise the classifier directly with
# synthetic _probe_submit_state outputs.
# ---------------------------------------------------------------------


class _Client:
    """Bare-bones GrokClient subclass we can construct without a
    real browser. We override _probe_submit_state to return whatever
    the test wants and let the real classifier run on it."""

    def __init__(self, probe_result: dict):
        self._probe_result = probe_result

    async def _probe_submit_state(self):
        return self._probe_result

    # Bind the real classifier (and its constant dictionaries) onto
    # the bare instance.
    _classify_submit_block = GrokClient._classify_submit_block
    _QUOTA_HINTS = GrokClient._QUOTA_HINTS
    _RATE_HINTS = GrokClient._RATE_HINTS


@pytest.mark.asyncio
async def test_silent_disabled_no_banner_is_ratelimit():
    """THE bug: submit disabled, no banner, no candidate messages.
    Must return GrokRateLimitError so pool early-exits."""
    c = _Client(
        {
            "submit_disabled": True,
            "banners": [],
            "candidate_messages": [],
            "submit_aria": "提交",
            "submit_text": "",
            "rejected_candidates": [],
        }
    )
    result = await c._classify_submit_block(action="create_image")
    assert isinstance(result, GrokRateLimitError), (
        f"silent-throttle must classify as GrokRateLimitError, got {type(result).__name__}"
    )
    msg = str(result)
    # The error must be self-explanatory and warn about retry escalation.
    assert "DO NOT retry" in msg, "must warn against retry"
    assert "escalat" in msg.lower(), (
        "must mention escalation so caller knows why a typed error fires"
    )
    assert "create_image" in msg, "must name the action"


@pytest.mark.asyncio
async def test_submit_enabled_returns_none():
    """If submit is still enabled it's NOT a server-side block —
    classifier returns None so caller falls back to its own error
    (e.g. the 'overlay intercepted click' hint)."""
    c = _Client(
        {
            "submit_disabled": False,
            "banners": [],
            "candidate_messages": [],
        }
    )
    result = await c._classify_submit_block(action="create_image")
    assert result is None, (
        f"enabled submit must not classify as a throttle error; "
        f"got {type(result).__name__ if result else None}"
    )


@pytest.mark.asyncio
async def test_rate_limit_banner_is_ratelimit():
    """Existing behaviour preserved: banner text with rate-limit
    keyword → GrokRateLimitError."""
    c = _Client(
        {
            "submit_disabled": True,
            "banners": ["请稍后再试，频率过高"],
            "candidate_messages": [],
        }
    )
    result = await c._classify_submit_block(action="create_image")
    assert isinstance(result, GrokRateLimitError)
    assert not isinstance(result, GrokQuotaExceededError)


@pytest.mark.asyncio
async def test_quota_banner_is_quota_error():
    """Quota keywords → GrokQuotaExceededError (subclass of RateLimit)."""
    c = _Client(
        {
            "submit_disabled": True,
            "banners": ["今日生成已达上限，请升级订阅"],
            "candidate_messages": [],
        }
    )
    result = await c._classify_submit_block(action="create_image")
    assert isinstance(result, GrokQuotaExceededError), (
        f"quota text must classify as GrokQuotaExceededError "
        f"(subclass of GrokRateLimitError), got {type(result).__name__}"
    )


@pytest.mark.asyncio
async def test_candidate_messages_text_pool_is_searched():
    """Wide-net candidate_messages pool (plain <div> text) must also
    feed the classifier — Grok sometimes renders rate-limit hints
    outside any role=alert / banner / toast container."""
    c = _Client(
        {
            "submit_disabled": True,
            "banners": [],
            "candidate_messages": [{"text": "Please try again in a few minutes"}],
        }
    )
    result = await c._classify_submit_block(action="create_image")
    assert isinstance(result, GrokRateLimitError)


@pytest.mark.asyncio
async def test_no_GrokAPIError_for_disabled_branch():
    """Belt-and-suspenders: even with weird probe state, disabled
    submit never returns generic GrokAPIError — always RateLimit (or
    a more specific subclass). This prevents regression to the bug."""
    weird_probes = [
        {"submit_disabled": True, "banners": [], "candidate_messages": []},
        {"submit_disabled": True, "banners": ["random unrelated text"], "candidate_messages": []},
        {
            "submit_disabled": True,
            "banners": [],
            "candidate_messages": [{"text": "some other stuff"}],
        },
    ]
    for probe in weird_probes:
        c = _Client(probe)
        result = await c._classify_submit_block(action="create_image")
        assert isinstance(result, GrokRateLimitError), (
            f"probe {probe!r} should classify as RateLimit, "
            f"got {type(result).__name__ if result else None}"
        )
        # Must NOT be the parent GrokAPIError (without RateLimit subclass)
        assert type(result) is not GrokAPIError, (
            "must not return generic GrokAPIError for disabled-submit case"
        )
