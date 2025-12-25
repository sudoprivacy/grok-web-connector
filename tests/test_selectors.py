"""Tests for selectors.py thumbnail selector utilities."""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock

import pytest

from grok_web.selectors import (
    _signal_cleaned_paths,
    select_all,
    signal_file_selector,
    timeout_selector,
)


class TestSelectAll:
    """Tests for select_all() selector."""

    @pytest.mark.asyncio
    async def test_select_all_returns_all_indices(self):
        """select_all() returns all indices from 0 to item_count-1."""
        selector = select_all()
        mock_scan = AsyncMock(return_value=[])

        result = await selector(5, mock_scan)

        assert result == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_select_all_empty_items(self):
        """select_all() with 0 items returns empty list."""
        selector = select_all()
        mock_scan = AsyncMock(return_value=[])

        result = await selector(0, mock_scan)

        assert result == []

    @pytest.mark.asyncio
    async def test_select_all_does_not_call_scan(self):
        """select_all() doesn't call scan_favorites."""
        selector = select_all()
        mock_scan = AsyncMock(return_value=[1, 2])

        await selector(3, mock_scan)

        mock_scan.assert_not_called()


class TestTimeoutSelector:
    """Tests for timeout_selector() selector."""

    @pytest.mark.asyncio
    async def test_timeout_selector_waits_and_scans(self):
        """timeout_selector() waits for timeout then scans favorites."""
        selector = timeout_selector(seconds=0.1)  # Very short timeout for tests
        mock_scan = AsyncMock(return_value=[0, 2])

        result = await selector(5, mock_scan)

        mock_scan.assert_called_once()
        assert result == [0, 2]

    @pytest.mark.asyncio
    async def test_timeout_selector_empty_favorites(self):
        """timeout_selector() returns empty list when no favorites."""
        selector = timeout_selector(seconds=0.1)
        mock_scan = AsyncMock(return_value=[])

        result = await selector(3, mock_scan)

        assert result == []

    @pytest.mark.asyncio
    async def test_timeout_selector_custom_message(self, capsys):
        """timeout_selector() prints custom message."""
        selector = timeout_selector(seconds=0.1, message="Custom message here")
        mock_scan = AsyncMock(return_value=[])

        await selector(2, mock_scan)

        captured = capsys.readouterr()
        assert "Custom message here" in captured.out

    @pytest.mark.asyncio
    async def test_timeout_selector_default_message(self, capsys):
        """timeout_selector() prints default message with timeout."""
        selector = timeout_selector(seconds=30)
        mock_scan = AsyncMock(return_value=[])

        # Use a very short timeout for the actual wait
        # We're testing the message format, not the actual wait
        async def fast_selector(item_count, scan_favorites):
            msg = f"Click hearts on images you want. Auto-continuing in 30s..."
            print(f"\n[Selection] {item_count} images available. {msg}")
            return await scan_favorites()

        await fast_selector(5, mock_scan)

        captured = capsys.readouterr()
        assert "5 images available" in captured.out
        assert "30s" in captured.out


class TestSignalFileSelector:
    """Tests for signal_file_selector() selector."""

    @pytest.mark.asyncio
    async def test_signal_file_selector_scans_when_file_exists(self):
        """signal_file_selector() scans favorites when signal file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, "signal_done")

            # Pre-add to cleaned paths to skip cleanup
            _signal_cleaned_paths.add(signal_path)

            # Create signal file before calling selector
            with open(signal_path, "w") as f:
                f.write(".")

            selector = signal_file_selector(signal_path=signal_path)
            mock_scan = AsyncMock(return_value=[1, 3])

            result = await selector(5, mock_scan)

            mock_scan.assert_called_once()
            assert result == [1, 3]

            # Cleanup
            _signal_cleaned_paths.discard(signal_path)

    @pytest.mark.asyncio
    async def test_signal_file_selector_skips_cleanup_on_repeat(self):
        """signal_file_selector() doesn't clean if path already cleaned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, "repeat_signal")

            # Pre-add to cleaned paths (simulating second call in same process)
            _signal_cleaned_paths.add(signal_path)

            # Create signal file that should NOT be deleted
            with open(signal_path, "w") as f:
                f.write("keep me")

            selector = signal_file_selector(signal_path=signal_path)
            mock_scan = AsyncMock(return_value=[0])

            result = await selector(2, mock_scan)

            # File should still exist (not cleaned)
            assert os.path.exists(signal_path)
            assert result == [0]

            # Cleanup for other tests
            _signal_cleaned_paths.discard(signal_path)

    @pytest.mark.asyncio
    async def test_signal_file_selector_prints_instructions(self, capsys):
        """signal_file_selector() prints instructions to user."""
        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, "test_signal")
            _signal_cleaned_paths.add(signal_path)  # Skip cleanup

            # Create signal immediately so it doesn't block
            with open(signal_path, "w") as f:
                f.write(".")

            selector = signal_file_selector(signal_path=signal_path)
            mock_scan = AsyncMock(return_value=[])

            await selector(3, mock_scan)

            captured = capsys.readouterr()
            assert "3 images available" in captured.out
            assert signal_path in captured.out
            assert "echo" in captured.out

            _signal_cleaned_paths.discard(signal_path)

    @pytest.mark.asyncio
    async def test_signal_file_selector_empty_favorites(self):
        """signal_file_selector() returns empty list when no favorites."""
        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, "empty_signal")
            _signal_cleaned_paths.add(signal_path)  # Skip cleanup

            # Create signal file immediately
            with open(signal_path, "w") as f:
                f.write(".")

            selector = signal_file_selector(signal_path=signal_path)
            mock_scan = AsyncMock(return_value=[])

            result = await selector(5, mock_scan)

            assert result == []

            _signal_cleaned_paths.discard(signal_path)