"""Tests for AsyncClient class."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from grok_web.client import AsyncClient
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
    async def test_api_request_401_plain(self, mock_client: AsyncClient):
        """401 without Cloudflare raises plain auth error."""
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.text = AsyncMock(return_value="Unauthorized")
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="blocked"):
            await mock_client._api_request("POST", "/rest/media/post/list", {})

    @pytest.mark.asyncio
    async def test_api_request_404(self, mock_client: AsyncClient):
        """404 raises GrokNotFoundError."""
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokNotFoundError):
            await mock_client._api_request("POST", "/rest/media/post/get", {"id": "invalid"})

    @pytest.mark.asyncio
    async def test_api_request_500_raises_api_error(self, mock_client: AsyncClient):
        """500 raises GrokAPIError."""
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokAPIError, match="API error: 500"):
            await mock_client._api_request("POST", "/rest/media/post/list", {})

    @pytest.mark.asyncio
    async def test_api_request_invalid_json(self, mock_client: AsyncClient):
        """Invalid JSON response returns empty dict."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(side_effect=ValueError("Invalid JSON"))
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client._api_request("POST", "/rest/media/post/list", {})
        assert result == {}

    @pytest.mark.asyncio
    async def test_api_request_connection_error(self, mock_client: AsyncClient):
        """Connection error raises GrokAPIError."""
        mock_client._api_context.post = AsyncMock(side_effect=Exception("Connection failed"))

        with pytest.raises(GrokAPIError, match="Request failed"):
            await mock_client._api_request("POST", "/rest/media/post/list", {})


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


class TestAsyncClientAPIRequestText:
    """Tests for AsyncClient._api_request_text method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_api_request_text_post_success(self, mock_client: AsyncClient):
        """Successful POST returns raw text."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value='{"key": "value"}')
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client._api_request_text("POST", "/endpoint", {"data": 1})
        assert result == '{"key": "value"}'

    @pytest.mark.asyncio
    async def test_api_request_text_get_success(self, mock_client: AsyncClient):
        """Successful GET returns raw text."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="plain text response")
        mock_client._api_context.get = AsyncMock(return_value=mock_response)

        result = await mock_client._api_request_text("GET", "/endpoint")
        assert result == "plain text response"

    @pytest.mark.asyncio
    async def test_api_request_text_unsupported_method(self, mock_client: AsyncClient):
        """Unsupported method raises error."""
        with pytest.raises(GrokAPIError, match="Unsupported HTTP method"):
            await mock_client._api_request_text("PUT", "/endpoint")

    @pytest.mark.asyncio
    async def test_api_request_text_401_cloudflare(self, mock_client: AsyncClient):
        """401 with Cloudflare raises auth error."""
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.text = AsyncMock(return_value="Just a moment...")
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="Cloudflare"):
            await mock_client._api_request_text("POST", "/endpoint", {})

    @pytest.mark.asyncio
    async def test_api_request_text_403_plain(self, mock_client: AsyncClient):
        """403 without Cloudflare raises plain auth error."""
        mock_response = AsyncMock()
        mock_response.status = 403
        mock_response.text = AsyncMock(return_value="Forbidden")
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="blocked"):
            await mock_client._api_request_text("POST", "/endpoint", {})

    @pytest.mark.asyncio
    async def test_api_request_text_404(self, mock_client: AsyncClient):
        """404 raises GrokNotFoundError."""
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokNotFoundError):
            await mock_client._api_request_text("POST", "/endpoint", {})

    @pytest.mark.asyncio
    async def test_api_request_text_500(self, mock_client: AsyncClient):
        """500 raises GrokAPIError."""
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        with pytest.raises(GrokAPIError, match="API error: 500"):
            await mock_client._api_request_text("POST", "/endpoint", {})

    @pytest.mark.asyncio
    async def test_api_request_text_connection_error(self, mock_client: AsyncClient):
        """Connection error raises GrokAPIError."""
        mock_client._api_context.post = AsyncMock(side_effect=Exception("Network error"))

        with pytest.raises(GrokAPIError, match="Request failed"):
            await mock_client._api_request_text("POST", "/endpoint", {})


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


class TestAsyncClientAssetRequestHead:
    """Tests for AsyncClient._asset_request_head method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        client._asset_context = None
        return client

    @pytest.mark.asyncio
    async def test_asset_head_403_raises_auth_error(self, mock_client: AsyncClient):
        """403 raises GrokAuthError."""
        mock_asset_context = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 403
        mock_asset_context.head = AsyncMock(return_value=mock_response)
        mock_client._playwright.request.new_context = AsyncMock(return_value=mock_asset_context)

        with pytest.raises(GrokAuthError, match="denied"):
            await mock_client._asset_request_head("https://assets.grok.com/video.mp4")

    @pytest.mark.asyncio
    async def test_asset_head_500_raises_api_error(self, mock_client: AsyncClient):
        """Non-200 raises GrokAPIError."""
        mock_asset_context = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_asset_context.head = AsyncMock(return_value=mock_response)
        mock_client._playwright.request.new_context = AsyncMock(return_value=mock_asset_context)

        with pytest.raises(GrokAPIError, match="failed: 500"):
            await mock_client._asset_request_head("https://assets.grok.com/video.mp4")

    @pytest.mark.asyncio
    async def test_asset_head_no_content_length(self, mock_client: AsyncClient):
        """Missing Content-Length raises GrokAPIError."""
        mock_asset_context = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_asset_context.head = AsyncMock(return_value=mock_response)
        mock_client._playwright.request.new_context = AsyncMock(return_value=mock_asset_context)

        with pytest.raises(GrokAPIError, match="Content-Length"):
            await mock_client._asset_request_head("https://assets.grok.com/video.mp4")

    @pytest.mark.asyncio
    async def test_asset_head_connection_error(self, mock_client: AsyncClient):
        """Connection error raises GrokAPIError."""
        mock_asset_context = AsyncMock()
        mock_asset_context.head = AsyncMock(side_effect=Exception("Network error"))
        mock_client._playwright.request.new_context = AsyncMock(return_value=mock_asset_context)

        with pytest.raises(GrokAPIError, match="Asset request failed"):
            await mock_client._asset_request_head("https://assets.grok.com/video.mp4")


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


class TestAsyncClientFavoriteUnfavorite:
    """Tests for async favorite_post and unfavorite_post methods."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = AsyncClient(cookies=mock_cookies)
        client._playwright = AsyncMock()
        client._api_context = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_favorite_post_success(self, mock_client: AsyncClient):
        """favorite_post returns True on success."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client.favorite_post("post-id-123")
        assert result is True

    @pytest.mark.asyncio
    async def test_unfavorite_post_success(self, mock_client: AsyncClient):
        """unfavorite_post returns True on success."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={})
        mock_client._api_context.post = AsyncMock(return_value=mock_response)

        result = await mock_client.unfavorite_post("post-id-123")
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

    @pytest.mark.asyncio
    async def test_match_local_video_web_format_recognized(self, mock_client: AsyncClient):
        """Web format filenames are recognized (uuid_hd.mp4) - O(1) direct lookup."""
        # Create temp file with web format name
        temp_dir = tempfile.mkdtemp()
        video_id = "b8db4523-04f0-496a-b516-6044972bb3fd"
        temp_path = os.path.join(temp_dir, f"{video_id}_hd.mp4")
        with open(temp_path, "wb") as f:
            f.write(b"x" * 1000)

        try:
            # Mock get_post_details to raise not found, then fallback searches favorites
            mock_client.get_post_details = AsyncMock(
                side_effect=GrokNotFoundError(f"Post not found: {video_id}")
            )
            # Mock list_posts to return empty (no favorites)
            mock_client.list_posts = AsyncMock(return_value=[])

            # Should raise GrokAPIError saying video not found in recent favorites
            with pytest.raises(GrokAPIError, match="Video not found in recent"):
                await mock_client.match_local_video(temp_path)
        finally:
            os.unlink(temp_path)
            os.rmdir(temp_dir)

    @pytest.mark.asyncio
    async def test_match_local_video_web_format_with_copy_number(self, mock_client: AsyncClient):
        """Web format with copy number is recognized (uuid_hd (1).mp4) - O(1) direct lookup."""
        temp_dir = tempfile.mkdtemp()
        video_id = "b8db4523-04f0-496a-b516-6044972bb3fd"
        temp_path = os.path.join(temp_dir, f"{video_id}_hd (1).mp4")
        with open(temp_path, "wb") as f:
            f.write(b"x" * 1000)

        try:
            # Mock get_post_details to raise not found, then fallback searches favorites
            mock_client.get_post_details = AsyncMock(
                side_effect=GrokNotFoundError(f"Post not found: {video_id}")
            )
            # Mock list_posts to return empty (no favorites)
            mock_client.list_posts = AsyncMock(return_value=[])

            # Should raise GrokAPIError saying video not found in recent favorites
            with pytest.raises(GrokAPIError, match="Video not found in recent"):
                await mock_client.match_local_video(temp_path)
        finally:
            os.unlink(temp_path)
            os.rmdir(temp_dir)

    @pytest.mark.asyncio
    async def test_match_local_video_web_format_without_hd(self, mock_client: AsyncClient):
        """Web format without _hd suffix is recognized (uuid.mp4) - O(1) direct lookup."""
        temp_dir = tempfile.mkdtemp()
        video_id = "b8db4523-04f0-496a-b516-6044972bb3fd"
        temp_path = os.path.join(temp_dir, f"{video_id}.mp4")
        with open(temp_path, "wb") as f:
            f.write(b"x" * 1000)

        try:
            # Mock get_post_details to raise not found, then fallback searches favorites
            mock_client.get_post_details = AsyncMock(
                side_effect=GrokNotFoundError(f"Post not found: {video_id}")
            )
            # Mock list_posts to return empty (no favorites)
            mock_client.list_posts = AsyncMock(return_value=[])

            # Should raise GrokAPIError saying video not found in recent favorites
            with pytest.raises(GrokAPIError, match="Video not found in recent"):
                await mock_client.match_local_video(temp_path)
        finally:
            os.unlink(temp_path)
            os.rmdir(temp_dir)


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
        """Verify correct payload structure and VideoGenerationResult."""
        # Mock NDJSON streaming response
        ndjson_response = (
            '{"result":{"conversation":{"conversationId":"conv-123"}}}\n'
            '{"result":{"response":{"streamingVideoGenerationResponse":'
            '{"videoId":"vid-456","parentPostId":"parent-123",'
            '"moderated":false,"progress":100,"mode":"normal",'
            '"modelName":"imagine_xdit_1"}}}}'
        )

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=ndjson_response)
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

        # Verify result type and values
        from grok_web import VideoGenerationResult

        assert isinstance(result, VideoGenerationResult)
        assert result.video_id == "vid-456"
        assert result.parent_post_id == "parent-123"
        assert result.moderated is False
        assert result.progress == 100
        assert result.success is True
