"""Tests for GrokClient class."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import tempfile
import os

from grok_web import GrokClient
from grok_web.models import GrokCookies, GenerationMode, PostSummary, PostDetails
from grok_web.exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError


class TestGrokClientInit:
    """Tests for GrokClient initialization."""

    def test_init_with_cookies(self, mock_cookies: GrokCookies):
        """Initialize with provided cookies."""
        with patch("grok_web.client.requests.Session") as mock_session:
            mock_session.return_value = MagicMock()
            client = GrokClient(cookies=mock_cookies)
            assert client.cookies == mock_cookies

    def test_init_loads_from_config(self, mock_cookies: GrokCookies):
        """Initialize loads cookies from config file."""
        mock_config = {"cookies": mock_cookies, "headers": {}}

        with patch("grok_web.client.load_config", return_value=mock_config):
            with patch("grok_web.client.requests.Session") as mock_session:
                mock_session.return_value = MagicMock()
                client = GrokClient()
                assert client.cookies == mock_cookies

    def test_init_custom_config_path(self, mock_cookies: GrokCookies):
        """Initialize with custom config path."""
        mock_config = {"cookies": mock_cookies, "headers": {"x-custom": "header"}}

        with patch("grok_web.client.load_config", return_value=mock_config) as mock_load:
            with patch("grok_web.client.requests.Session") as mock_session:
                mock_session.return_value = MagicMock()
                GrokClient(config_path="/custom/path.json")
                mock_load.assert_called_once_with("/custom/path.json")


class TestGrokClientAPIRequest:
    """Tests for GrokClient._api_request method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_api_request_success(self, client: GrokClient):
        """Successful API request returns JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"posts": []}
        client._session.request.return_value = mock_response

        result = client._api_request("POST", "/rest/media/post/list", {"limit": 10})
        assert result == {"posts": []}

    def test_api_request_401_raises_auth_error(self, client: GrokClient):
        """401 response raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        client._session.request.return_value = mock_response

        with pytest.raises(GrokAuthError):
            client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_403_raises_auth_error(self, client: GrokClient):
        """403 response raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        client._session.request.return_value = mock_response

        with pytest.raises(GrokAuthError):
            client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_404_raises_not_found(self, client: GrokClient):
        """404 response raises GrokNotFoundError."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        client._session.request.return_value = mock_response

        with pytest.raises(GrokNotFoundError):
            client._api_request("POST", "/rest/media/post/get", {"id": "invalid"})

    def test_api_request_500_raises_api_error(self, client: GrokClient):
        """500 response raises GrokAPIError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        client._session.request.return_value = mock_response

        with pytest.raises(GrokAPIError):
            client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_connection_error(self, client: GrokClient):
        """Connection error raises GrokAPIError."""
        client._session.request.side_effect = Exception("Connection failed")

        with pytest.raises(GrokAPIError, match="Connection failed"):
            client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_invalid_json(self, client: GrokClient):
        """Invalid JSON response returns empty dict."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")
        client._session.request.return_value = mock_response

        result = client._api_request("POST", "/rest/media/post/list", {})
        assert result == {}


class TestGrokClientListPosts:
    """Tests for GrokClient.list_posts method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_list_posts_returns_summaries(self, client: GrokClient, sample_list_response: dict):
        """list_posts returns list of PostSummary objects."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_list_response
        client._session.request.return_value = mock_response

        posts = client.list_posts(limit=10)

        assert len(posts) == 2
        assert all(isinstance(p, PostSummary) for p in posts)

    def test_list_posts_default_source_is_liked(self, client: GrokClient):
        """Default source is MEDIA_POST_SOURCE_LIKED."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"posts": []}
        client._session.request.return_value = mock_response

        client.list_posts()

        # Check the call was made with correct filter
        call_args = client._session.request.call_args
        json_data = call_args[1]["json"]
        assert json_data["filter"]["source"] == "MEDIA_POST_SOURCE_LIKED"

    def test_list_posts_source_none_uses_empty_filter(self, client: GrokClient):
        """source=None uses empty filter for all public posts."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"posts": []}
        client._session.request.return_value = mock_response

        client.list_posts(source=None)

        call_args = client._session.request.call_args
        json_data = call_args[1]["json"]
        assert json_data["filter"] == {}

    def test_list_posts_custom_limit(self, client: GrokClient):
        """Custom limit is passed to API."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"posts": []}
        client._session.request.return_value = mock_response

        client.list_posts(limit=50)

        call_args = client._session.request.call_args
        json_data = call_args[1]["json"]
        assert json_data["limit"] == 50

    def test_list_posts_skips_malformed_posts(self, client: GrokClient):
        """Malformed posts are skipped."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "posts": [
                {"id": "good-post", "mediaType": "MEDIA_POST_TYPE_IMAGE", "childPosts": []},
                None,  # Will be skipped
                {"id": "another-good", "mediaType": "MEDIA_POST_TYPE_VIDEO", "childPosts": []},
            ]
        }
        client._session.request.return_value = mock_response

        posts = client.list_posts()
        assert len(posts) == 2


class TestGrokClientGetPostDetails:
    """Tests for GrokClient.get_post_details method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_get_post_details_returns_details(self, client: GrokClient, sample_get_response: dict):
        """get_post_details returns PostDetails object."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_get_response
        client._session.request.return_value = mock_response

        details = client.get_post_details("test-post-id-1234")

        assert isinstance(details, PostDetails)
        assert details.id == "test-post-id-1234"
        assert len(details.children) == 2

    def test_get_post_details_includes_raw_data(self, client: GrokClient, sample_get_response: dict):
        """Raw API response is preserved in raw_data."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_get_response
        client._session.request.return_value = mock_response

        details = client.get_post_details("test-id")
        assert details.raw_data == sample_get_response


class TestGrokClientGetAssetFileSize:
    """Tests for GrokClient.get_asset_file_size method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_get_asset_file_size_success(self, client: GrokClient):
        """Successful HEAD request returns file size."""
        with patch("grok_web.client.requests.head") as mock_head:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-length": "1234567"}
            mock_head.return_value = mock_response

            size = client.get_asset_file_size("https://assets.grok.com/video.mp4")
            assert size == 1234567

    def test_get_asset_file_size_empty_url(self, client: GrokClient):
        """Empty URL raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Asset URL is empty"):
            client.get_asset_file_size("")

    def test_get_asset_file_size_invalid_domain(self, client: GrokClient):
        """Non-grok URL raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Invalid asset URL"):
            client.get_asset_file_size("https://other.com/video.mp4")

    def test_get_asset_file_size_403_raises_auth_error(self, client: GrokClient):
        """403 response raises GrokAuthError."""
        with patch("grok_web.client.requests.head") as mock_head:
            mock_response = MagicMock()
            mock_response.status_code = 403
            mock_head.return_value = mock_response

            with pytest.raises(GrokAuthError):
                client.get_asset_file_size("https://assets.grok.com/video.mp4")

    def test_get_asset_file_size_no_content_length(self, client: GrokClient):
        """Missing Content-Length raises GrokAPIError."""
        with patch("grok_web.client.requests.head") as mock_head:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_head.return_value = mock_response

            with pytest.raises(GrokAPIError, match="No Content-Length"):
                client.get_asset_file_size("https://assets.grok.com/video.mp4")


class TestGrokClientValidateAuth:
    """Tests for GrokClient.validate_auth method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_validate_auth_success(self, client: GrokClient):
        """Returns True when auth is valid."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"posts": []}
        client._session.request.return_value = mock_response

        assert client.validate_auth() is True

    def test_validate_auth_failure(self, client: GrokClient):
        """Returns False when auth fails."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        client._session.request.return_value = mock_response

        assert client.validate_auth() is False


class TestGrokClientLikeUnlike:
    """Tests for like_post and unlike_post methods."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_like_post_success(self, client: GrokClient):
        """like_post returns True on success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        client._session.request.return_value = mock_response

        result = client.like_post("post-id-123")
        assert result is True

        # Verify correct endpoint called
        call_args = client._session.request.call_args
        assert "/rest/media/post/like" in call_args[0][1]

    def test_unlike_post_success(self, client: GrokClient):
        """unlike_post returns True on success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        client._session.request.return_value = mock_response

        result = client.unlike_post("post-id-123")
        assert result is True

        # Verify correct endpoint called
        call_args = client._session.request.call_args
        assert "/rest/media/post/unlike" in call_args[0][1]


class TestGrokClientMatchLocalVideo:
    """Tests for GrokClient.match_local_video method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_match_local_video_file_not_found(self, client: GrokClient):
        """Raises error for non-existent file."""
        with pytest.raises(GrokAPIError, match="File not found"):
            client.match_local_video("/nonexistent/grok-video-12345678-1234-1234-1234-123456789012.mp4")

    def test_match_local_video_invalid_filename(self, client: GrokClient):
        """Raises error for invalid filename format."""
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"dummy")
            temp_path = f.name

        try:
            with pytest.raises(GrokAPIError, match="Invalid filename format"):
                client.match_local_video(temp_path)
        finally:
            os.unlink(temp_path)

    def test_match_local_video_success(self, client: GrokClient, sample_get_response: dict):
        """Successfully matches video by file size."""
        # Create temp file with known size
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "grok-video-12345678-1234-1234-1234-123456789012.mp4"
            filepath.write_bytes(b"x" * 1000)

            # Mock get_post_details
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = sample_get_response
            client._session.request.return_value = mock_response

            # Mock get_asset_file_size
            with patch.object(client, "get_asset_file_size", return_value=1000):
                result = client.match_local_video(filepath)

                assert result.parent_id == "12345678-1234-1234-1234-123456789012"
                assert result.file_size == 1000


class TestGrokClientCreateVideoFromImage:
    """Tests for GrokClient.create_video_from_image method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create a GrokClient with mocked session."""
        with patch("grok_web.client.requests.Session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            client = GrokClient(cookies=mock_cookies)
            client._session = session_instance
            return client

    def test_create_video_from_image_payload(self, client: GrokClient):
        """Verify correct payload structure."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"conversationId": "conv-123"}
        client._session.request.return_value = mock_response

        result = client.create_video_from_image(
            image_url="https://imagine-public.x.ai/image.jpg",
            parent_post_id="parent-123",
            aspect_ratio="16:9",
            video_length=10,
        )

        # Verify payload
        call_args = client._session.request.call_args
        json_data = call_args[1]["json"]

        assert json_data["modelName"] == "grok-3"
        assert json_data["toolOverrides"]["videoGen"] is True
        assert "parent-123" in str(json_data)
        assert "16:9" in str(json_data)
        assert result == {"conversationId": "conv-123"}
