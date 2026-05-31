"""Lock in BrowserWorkerPool.profile_prefix wiring.

The reporter (jailbreak AI, 2026-05) tried to isolate concurrent agents'
Chrome profiles by monkey-patching ``grok_web.client.GROK_CHROME_PROFILE``.
That doesn't work because ``worker_pool.py`` does
``from ..client import GROK_CHROME_PROFILE`` and captures the value at
import time. Add a ``profile_prefix`` kwarg that's resolved at
``__init__`` so caller overrides actually take effect.

Pure shape test — no browser, no network. Constructs the pool with
``num_workers=0`` so ``add_worker`` never fires (it would need a real
Chrome). We're verifying the prefix is stored correctly on the instance
and that the default still falls back to ``GROK_CHROME_PROFILE``.
"""

from __future__ import annotations

import pytest

from grok_web import BrowserWorkerPool
from grok_web.client import GROK_CHROME_PROFILE


@pytest.fixture
def mock_cookies(monkeypatch, tmp_path):
    """Provide a stub config so BrowserWorkerPool() construction skips
    interactive auth setup. We only care about the prefix wiring here."""
    from grok_web.models import GrokCookies

    cookies = GrokCookies(
        sso="x" * 20,
        sso_rw="y" * 20,
        cf_clearance="z" * 20,
        x_userid="0" * 20,
    )
    return cookies


def test_default_profile_prefix_is_package_constant(mock_cookies):
    pool = BrowserWorkerPool(num_workers=0, cookies=mock_cookies)
    assert pool._profile_prefix == GROK_CHROME_PROFILE


def test_explicit_profile_prefix_wins(mock_cookies):
    pool = BrowserWorkerPool(
        num_workers=0,
        cookies=mock_cookies,
        profile_prefix="jailbreak-chrome",
    )
    assert pool._profile_prefix == "jailbreak-chrome"


def test_profile_prefix_none_falls_back_to_default(mock_cookies):
    # Explicitly passing None must still use the package default —
    # this matches every existing caller, who pass no kwarg.
    pool = BrowserWorkerPool(num_workers=0, cookies=mock_cookies, profile_prefix=None)
    assert pool._profile_prefix == GROK_CHROME_PROFILE
