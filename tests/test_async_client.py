"""Tests for AsyncClient class."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from grok_web import AsyncClient
from grok_web.exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from grok_web.models import GrokCookies, PostDetails, PostSummary


class TestAsyncClientInit:
    """Tests for AsyncClient initialization."""

    def test_init_with_cookies(self, mock_cookies: GrokCookies):
        """Initialize with provided cookies."""
        client = AsyncClient(cookies=mock_cookies)
        assert client.cookies == mock_cookies
        assert client._playwright is None  # Not started yet

    def test_init_loads_from_config(self, mock_cookies: GrokCookies):
        """Initialize loads cookies from config file."""
        mock_config = {"cookies": mock_cookies, "headers": {}}

        with patch("grok_web.client.load_config", return_value=mock_config):
            client = AsyncClient()
            assert client.cookies == mock_cookies


class TestAsyncClientContextManager:
    """Tests for AsyncClient async context manager."""

    @pytest.mark.asyncio
    async def test_async_context_manager_starts_playwright(self, mock_cookies: GrokCookies):
        """Async context manager starts Playwright."""
        with patch("grok_web.client.async_playwright") as mock_async:
            mock_playwright = AsyncMock()
            mock_context = AsyncMock()
            mock_async.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.request.new_context = AsyncMock(return_value=mock_context)

            client = AsyncClient(cookies=mock_cookies)
            async with client as ctx_client:
                assert ctx_client._playwright == mock_playwright
                assert ctx_client._api_context == mock_context

    @pytest.mark.asyncio
    async def test_async_context_manager_cleanup(self, mock_cookies: GrokCookies):
        """Async context manager cleans up resources."""
        with patch("grok_web.client.async_playwright") as mock_async:
            mock_playwright = AsyncMock()
            mock_context = AsyncMock()
            mock_async.return_value.start = AsyncMock(return_value=mock_playwright)
            mock_playwright.request.new_context = AsyncMock(return_value=mock_context)

            client = AsyncClient(cookies=mock_cookies)
            async with client:
                pass

            # Verify cleanup
            mock_context.dispose.assert_called_once()
            mock_playwright.stop.assert_called_once()


class TestAsyncClientAPIRequest:
    """Tests for AsyncClient._api_request method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_api_request_post_success(self, mock_client: AsyncClient):
        """Successful POST request returns JSON."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"posts": []})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client._api_request("POST", "/rest/media/post/list", {"limit": 10})
        assert result == {"posts": []}

    @pytest.mark.asyncio
    async def test_api_request_get_success(self, mock_client: AsyncClient):
        """Successful GET request returns JSON."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"data": "value"})
        mock_client._api_context.get = AsyncMock(return_value=mock_response)

        result = await mock_client._api_request("GET", "/some/endpoint")
        assert result == {"data": "value"}

    @pytest.mark.asyncio
    async def test_api_request_unsupported_method(self, mock_client: AsyncClient):
        """Unsupported HTTP method raises error."""
        with pytest.raises(GrokAPIError, match="Unsupported HTTP method"):
            await mock_client._api_request("DELETE", "/some/endpoint")

    @pytest.mark.asyncio
    async def test_api_request_cloudflare_challenge(self, mock_client: AsyncClient):
        """Cloudflare challenge triggers specific error."""
        mock_response = AsyncMock()
        mock_response.status = 403
        mock_response.text = AsyncMock(return_value="Just a moment...")
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="Cloudflare challenge"):
            await mock_client._api_request("POST", "/rest/media/post/list", {})

    @pytest.mark.asyncio
    async def test_api_request_404(self, mock_client: AsyncClient):
        """404 raises GrokNotFoundError."""
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokNotFoundError):
            await mock_client._api_request("POST", "/rest/media/post/get", {"id": "invalid"})


class TestAsyncClientListPosts:
    """Tests for AsyncClient.list_posts method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_list_posts_returns_summaries(
        self, mock_client: AsyncClient, sample_list_response: dict
    ):
        """list_posts returns list of PostSummary objects."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sample_list_response)
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        posts = await mock_client.list_posts(limit=10)

        assert len(posts) == 2
        assert all(isinstance(p, PostSummary) for p in posts)

    @pytest.mark.asyncio
    async def test_list_posts_default_source_is_liked(self, mock_client: AsyncClient):
        """Default source is MEDIA_POST_SOURCE_LIKED."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"posts": []})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        await mock_client.list_posts()

        call_args = mock_client._api_context.post.call_args
        data = call_args[1]["data"]
        assert data["filter"]["source"] == "MEDIA_POST_SOURCE_LIKED"

    @pytest.mark.asyncio
    async def test_list_posts_source_none(self, mock_client: AsyncClient):
        """source=None uses empty filter."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"posts": []})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        await mock_client.list_posts(source=None)

        call_args = mock_client._api_context.post.call_args
        data = call_args[1]["data"]
        assert data["filter"] == {}


class TestAsyncClientGetPostDetails:
    """Tests for AsyncClient.get_post_details method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_get_post_details_returns_details(
        self, mock_client: AsyncClient, sample_get_response: dict
    ):
        """get_post_details returns PostDetails object."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sample_get_response)
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        details = await mock_client.get_post_details("test-post-id-1234")

        assert isinstance(details, PostDetails)
        assert details.id == "test-post-id-1234"
        assert len(details.children) == 2


class TestAsyncClientGetAssetFileSize:
    """Tests for AsyncClient.get_asset_file_size method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        client._asset_context = None
        return client

    @pytest.mark.asyncio
    async def test_get_asset_file_size_success(self, mock_client: AsyncClient):
        """Successful HEAD request returns file size."""
        mock_asset_context = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"content-length": "1234567"}
        mock_asset_context.head = AsyncMock(return_value=mock_response)
        mock_client._playwright.request.new_context = AsyncMock(return_value=mock_asset_context)

        size = await mock_client.get_asset_file_size("https://assets.grok.com/video.mp4")
        assert size == 1234567

    @pytest.mark.asyncio
    async def test_get_asset_file_size_empty_url(self, mock_client: AsyncClient):
        """Empty URL raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Asset URL is empty"):
            await mock_client.get_asset_file_size("")

    @pytest.mark.asyncio
    async def test_get_asset_file_size_invalid_domain(self, mock_client: AsyncClient):
        """Non-grok URL raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Invalid asset URL"):
            await mock_client.get_asset_file_size("https://other.com/video.mp4")


class TestAsyncClientValidateAuth:
    """Tests for AsyncClient.validate_auth method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_validate_auth_success(self, mock_client: AsyncClient):
        """Returns True when auth is valid."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"posts": []})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client.validate_auth()
        assert result is True

    @pytest.mark.asyncio
    async def test_validate_auth_failure(self, mock_client: AsyncClient):
        """Returns False when auth fails."""
        mock_response = AsyncMock()
        mock_response.status = 403
        mock_response.text = AsyncMock(return_value="Forbidden")
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client.validate_auth()
        assert result is False


class TestAsyncClientLikeUnlike:
    """Tests for async like_post and unlike_post methods."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_like_post_success(self, mock_client: AsyncClient):
        """like_post returns True on success."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client.like_post("post-id-123")
        assert result is True

    @pytest.mark.asyncio
    async def test_unlike_post_success(self, mock_client: AsyncClient):
        """unlike_post returns True on success."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client.unlike_post("post-id-123")
        assert result is True


class TestAsyncClientMatchLocalVideo:
    """Tests for AsyncClient.match_local_video method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        client._asset_context = None
        return client

    @pytest.mark.asyncio
    async def test_match_local_video_file_not_found(self, mock_client: AsyncClient):
        """Raises error for non-existent file."""
        with pytest.raises(GrokAPIError, match="File not found"):
            await mock_client.match_local_video(
                "/nonexistent/grok-video-12345678-1234-1234-1234-123456789012.mp4"
            )

    @pytest.mark.asyncio
    async def test_match_local_video_invalid_filename(self, mock_client: AsyncClient):
        """Raises error for invalid filename format."""
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"dummy")
            temp_path = f.name

        try:
            with pytest.raises(GrokAPIError, match="Invalid filename format"):
                await mock_client.match_local_video(temp_path)
        finally:
            os.unlink(temp_path)


class TestAsyncClientCreateVideoFromImage:
    """Tests for AsyncClient.create_video_from_image method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_create_video_from_image_payload(self, mock_client: AsyncClient):
        """Verify correct payload structure."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"conversationId": "conv-123"})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client.create_video_from_image(
            image_url="https://imagine-public.x.ai/image.jpg",
            parent_post_id="parent-123",
            aspect_ratio="16:9",
            video_length=10,
        )

        # Verify payload
        call_args = mock_client._api_context.post.call_args
        data = call_args[1]["data"]

        assert data["modelName"] == "grok-3"
        assert data["toolOverrides"]["videoGen"] is True
        assert result == {"conversationId": "conv-123"}
