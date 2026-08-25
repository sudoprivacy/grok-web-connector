"""Unit tests for the Chrome-orphan fix (no real browser).

Exercises the __aexit__ close decision: close the Chrome WE launched (default),
but NEVER a Chrome we attached to (reused), and never when close_chrome=False.
The full live sequence (close-on-exit → 0 orphans; auto-reap a leftover on the
next launch) is tests/integration/test_workflows.py::test_chrome_no_orphans.
"""

from __future__ import annotations

import asyncio
import platform
import subprocess
from unittest.mock import MagicMock

from grok_web import GrokClient


def _run(coro):
    return asyncio.run(coro)


def _client(*, close_chrome, reused):
    c = GrokClient.__new__(GrokClient)
    c._initialized = False
    c._browser = None
    c._tab = None
    c._close_chrome = close_chrome
    c._reused_chrome = reused
    proc = MagicMock()
    proc.pid = 4242
    c._chrome_process = proc
    return c, proc


def _patch_taskkill(monkeypatch):
    calls = []
    real = subprocess.run
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (
            (calls.append(a[0] if a else None), real(["cmd", "/c", "echo"], capture_output=True))[1]
            if platform.system() == "Windows"
            else calls.append(a[0] if a else None)
        ),
    )
    return calls


class TestAexitCloseDecision:
    def test_closes_owned_chrome_by_default(self, monkeypatch):
        c, proc = _client(close_chrome=True, reused=False)
        calls = _patch_taskkill(monkeypatch)
        _run(c.__aexit__(None, None, None))
        if platform.system() == "Windows":
            assert any("taskkill" in (x or "") for x in calls), calls
        else:
            proc.terminate.assert_called_once()
        assert c._chrome_process is None  # cleared after close

    def test_skips_reused_chrome(self, monkeypatch):
        c, proc = _client(close_chrome=True, reused=True)
        calls = _patch_taskkill(monkeypatch)
        _run(c.__aexit__(None, None, None))
        assert not any("taskkill" in (x or "") for x in calls), calls
        proc.terminate.assert_not_called()
        assert c._chrome_process is proc  # untouched — not ours to kill

    def test_skips_when_close_chrome_false(self, monkeypatch):
        c, proc = _client(close_chrome=False, reused=False)
        calls = _patch_taskkill(monkeypatch)
        _run(c.__aexit__(None, None, None))
        assert not any("taskkill" in (x or "") for x in calls), calls
        proc.terminate.assert_not_called()
        assert c._chrome_process is proc  # kept alive on request


class TestReapNamespaceScoped:
    def test_reap_targets_only_our_profile_path(self, monkeypatch, tmp_path):
        # The reap must pass OUR user_data_dir into the match, never a blanket
        # kill. Assert the profile path appears in the powershell/pgrep command.
        seen = {}

        def fake_run(cmd, *a, **k):
            seen["cmd"] = cmd
            r = MagicMock()
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        GrokClient._reap_profile_chrome(tmp_path)
        joined = " ".join(seen.get("cmd", []))
        assert str(tmp_path) in joined, joined
        assert "/IM" not in joined and "-IM" not in joined  # never a blanket /IM chrome.exe
