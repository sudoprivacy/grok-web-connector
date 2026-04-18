"""Pytest configuration for grok-web-connector.

The test suite is integration-only: every meaningful test drives a real
Chrome + Grok session end-to-end. Unit tests were retired in favor of
workflow-level coverage (see tests/integration/).

Integration tests are OFF by default. To run them:

    pytest tests/integration/ -v --run-integration

Or set the env var::

    RUN_INTEGRATION=1 pytest tests/integration/ -v

Without either, all @pytest.mark.integration tests are skipped. The
smoke test (`test_imports_work`) still runs so `pytest tests/` always
returns at least one passing test.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run @pytest.mark.integration tests (requires real Chrome + Grok creds).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip @pytest.mark.integration tests unless explicitly enabled.

    Enabled via either:
      - `pytest --run-integration`
      - `RUN_INTEGRATION=1` env var
    """
    env_flag = os.environ.get("RUN_INTEGRATION", "").lower() in {"1", "true", "yes"}
    if config.getoption("--run-integration") or env_flag:
        return  # run all integration tests
    skip_marker = pytest.mark.skip(
        reason="integration test — pass --run-integration or set RUN_INTEGRATION=1"
    )
    for item in items:
        # Check for the explicit marker only, not item.keywords (which
        # also includes path components, so a file under tests/integration/
        # would always match even if not marked).
        if item.get_closest_marker("integration") is not None:
            item.add_marker(skip_marker)
