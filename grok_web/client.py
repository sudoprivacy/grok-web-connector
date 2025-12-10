"""
Grok Web Connector - Main Client

Provides 8 core APIs for interacting with Grok Imagine:

Read APIs:
1. list_posts() - Scan and get overview of all posts
2. get_post_details() - Get full details for a specific post
3. get_asset_file_size() - Get file size from assets.grok.com URL
4. validate_auth() - Check if authentication is valid
5. match_local_video() - Match local file to web video, generate new filename

Write APIs:
6. like_post() - Save post to favorites (long-term persistence)
7. unlike_post() - Remove post from favorites (delete)
8. create_video_from_image() - Generate video from image via chat API

IMPORTANT - Cloudflare Bot Detection:
    This client uses curl_cffi (not standard requests) to bypass Cloudflare's
    bot detection. Cloudflare detects bots via TLS fingerprinting - the way
    a client negotiates TLS (cipher suites, extensions, etc.) reveals whether
    it's a real browser or a Python script.

    curl_cffi impersonates Chrome's TLS fingerprint, making requests appear
    to come from a real browser. Without this, you'll get 403 errors even
    with valid cookies.

    If you encounter 403 errors:
    1. First check if curl_cffi impersonation version needs updating
       (we use chrome136, but newer versions may be needed as Chrome updates)
    2. Cookie expiration is RARE - cookies typically last weeks/months
    3. Try updating the impersonate parameter to a newer Chrome version
       Available: chrome131, chrome133a, chrome136, etc.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from curl_cffi import requests

from .auth import get_platform_headers, load_config, load_cookies
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
import os
import re

from .models import (
    ChildVideo,
    GenerationMode,
    GrokCookies,
    PostDetails,
    PostSummary,
    VideoMatchResult,
)


class GrokClient:
    """
    Client for Grok Imagine web API.

    Uses curl_cffi with Chrome TLS impersonation to bypass Cloudflare bot detection.
    This is essential - standard Python requests will be blocked with 403 errors.

    Troubleshooting 403 errors:
        1. Update impersonation: Try newer Chrome version (chrome136 -> chrome140, etc.)
        2. Update headers: Match sec-ch-ua to current Chrome version
        3. Cookie expiration: RARE, but check cf_clearance if all else fails

    Example:
        >>> client = GrokClient()
        >>> posts = client.list_posts(limit=10)
        >>> details = client.get_post_details(posts[0].id)
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    # Base headers for API requests (platform-specific headers added at runtime)
    BASE_API_HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "sec-ch-ua-mobile": "?0",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }

    # Base headers for asset requests (requires Referer and Origin!)
    BASE_ASSET_HEADERS = {
        "referer": "https://grok.com/",
        "origin": "https://grok.com",
    }

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """
        Initialize Grok client.

        Headers are determined in the following priority:
        1. Custom headers from config file ("headers" key in ~/.grok-config.json)
        2. Auto-detected platform headers (Windows/macOS/Linux)

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Only used if cookies is None.
                        Defaults to ~/.grok-config.json
        """
        # Load config (cookies + optional custom headers)
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]
            custom_headers = config["headers"]
        else:
            custom_headers = {}

        self.cookies = cookies

        # Build headers: base + platform-specific + custom overrides
        platform_headers = get_platform_headers()

        self._api_headers = {
            **self.BASE_API_HEADERS,
            **platform_headers,  # sec-ch-ua, sec-ch-ua-platform, user-agent
            **custom_headers,    # Custom overrides from config file
        }

        self._asset_headers = {
            **self.BASE_ASSET_HEADERS,
            "user-agent": platform_headers["user-agent"],
            **{k: v for k, v in custom_headers.items() if k == "user-agent"},
        }

        # Use curl_cffi with Chrome 136 impersonation to bypass Cloudflare bot detection
        # This mimics Chrome's TLS fingerprint for anti-bot bypass
        self._session = requests.Session(impersonate="chrome136")
        self._session.headers.update(self._api_headers)
        self._session.cookies.update(cookies.to_dict())

    # =========================================================================
    # API 1: list_posts() - Scan and get overview
    # =========================================================================

    def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
    ) -> list[PostSummary]:
        """
        List posts with basic metadata.

        By default, returns only your liked/favorited posts (the only reliable
        way to access your own content). Use source=None to browse all public
        posts from any user.

        Args:
            limit: Maximum number of posts to return (default: 40)
            source: Filter by source type. Options:
                    - "MEDIA_POST_SOURCE_LIKED": Your liked posts only (default)
                    - None: All public posts (from any user, not just yours)

        Returns:
            List of PostSummary objects with:
            - id: Post UUID
            - mode: Generation mode (img2vid, txt2vid, upload2vid)
            - prompt_preview: First 100 chars of prompt
            - video_count: Number of child videos
            - created_at: Creation timestamp

        Example:
            >>> posts = client.list_posts(limit=10)  # Your liked posts
            >>> for p in posts:
            ...     print(f"{p.id}: {p.mode.value} ({p.video_count} videos)")
            >>> all_posts = client.list_posts(source=None)  # All public posts
        """
        json_data: dict[str, Any] = {"limit": limit}
        # API requires filter to be present (even if empty)
        if source:
            json_data["filter"] = {"source": source}
        else:
            json_data["filter"] = {}

        data = self._api_request("POST", "/rest/media/post/list", json_data)

        posts = []
        for item in data.get("posts", []):
            try:
                summary = self._parse_post_summary(item)
                posts.append(summary)
            except Exception:
                continue  # Skip malformed posts

        return posts

    # =========================================================================
    # API 2: get_post_details() - Explore single post
    # =========================================================================

    def get_post_details(self, post_id: str) -> PostDetails:
        """
        Get full details of a post including all child videos.

        Args:
            post_id: Post UUID (from filename or web URL)

        Returns:
            PostDetails object with:
            - Parent post metadata (prompt, media_url, etc.)
            - Generation mode detection
            - All child videos with their metadata

        Raises:
            GrokNotFoundError: If post doesn't exist
            GrokAuthError: If authentication fails

        Example:
            >>> details = client.get_post_details("0c5c5864-fadb-440b-a52b-e441dab973d3")
            >>> print(f"Mode: {details.mode.value}")
            >>> print(f"Children: {details.video_count}")
            >>> for child in details.children:
            ...     print(f"  - {child.id}: {child.original_prompt[:50]}")
        """
        data = self._api_request("POST", "/rest/media/post/get", {"id": post_id})

        # Handle nested 'post' key
        post_data = data.get("post", data)

        return self._parse_post_details(post_data, post_id, raw_data=data)

    # =========================================================================
    # API 3: get_asset_file_size() - Get file size from assets URL
    # =========================================================================

    def get_asset_file_size(self, asset_url: str) -> int:
        """
        Get file size of a Grok asset (video or image) via HEAD request.

        Works for ALL video types:
        - img2vid children: child.hd_media_url
        - txt2vid parent: details.hd_media_url (parent itself is a video!)
        - txt2vid children: child.media_url or child.hd_media_url

        This method handles the special headers required by assets.grok.com:
        - Referer: https://grok.com/
        - Origin: https://grok.com

        Without these headers, requests return 403 Forbidden.

        Args:
            asset_url: Full URL to asset on assets.grok.com
                      (from PostDetails.hd_media_url or ChildVideo.hd_media_url)

        Returns:
            File size in bytes

        Raises:
            GrokAPIError: If request fails or URL is invalid

        Example:
            >>> details = client.get_post_details(post_id)
            >>>
            >>> # For txt2vid: parent itself is a video
            >>> if details.mode.value == 'txt2vid' and details.hd_media_url:
            ...     parent_size = client.get_asset_file_size(details.hd_media_url)
            ...     print(f"Parent video: {parent_size} bytes")
            >>>
            >>> # For all modes: children videos
            >>> for child in details.children:
            ...     url = child.hd_media_url or child.media_url
            ...     if url:
            ...         size = client.get_asset_file_size(url)
            ...         print(f"{child.id}: {size} bytes")
        """
        if not asset_url:
            raise GrokAPIError("Asset URL is empty")

        if not asset_url.startswith(self.ASSETS_URL):
            raise GrokAPIError(
                f"Invalid asset URL. Expected {self.ASSETS_URL}/..., got: {asset_url[:50]}..."
            )

        try:
            response = requests.head(
                asset_url,
                headers=self._asset_headers,
                cookies=self.cookies.to_dict(),
                timeout=15,
                impersonate="chrome136",
            )
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}")

        if response.status_code == 403:
            raise GrokAuthError(
                "Asset access denied (403). Check:\n"
                "1. TLS impersonation version (try newer chrome version)\n"
                "2. Required headers: Referer and Origin must be https://grok.com\n"
                "3. Cookie expiration (RARE) - cf_clearance may need refresh"
            )

        if response.status_code != 200:
            raise GrokAPIError(
                f"Asset request failed: {response.status_code}"
            )

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)

    # =========================================================================
    # API 4: validate_auth() - Check authentication status
    # =========================================================================

    def validate_auth(self) -> bool:
        """
        Check if current authentication and TLS bypass are working.

        Returns:
            True if requests succeed, False otherwise

        Note:
            False does NOT necessarily mean cookies expired!
            Most common cause is TLS fingerprint mismatch (Cloudflare bot detection).
            Try updating the impersonate version before refreshing cookies.

        Example:
            >>> if not client.validate_auth():
            ...     print("Check TLS impersonation version first, then cookies")
        """
        try:
            # Try to list posts with minimal limit
            self._api_request(
                "POST",
                "/rest/media/post/list",
                {"limit": 1, "filter": {}},
            )
            return True
        except GrokAuthError:
            return False
        except Exception:
            return False

    # =========================================================================
    # API 5: match_local_video() - Match local file to web video
    # =========================================================================

    def match_local_video(self, local_path: str | Path) -> VideoMatchResult:
        """
        Match a local grok video to its web counterpart and generate new filename.

        Takes a local file with old naming (grok-video-{parent_uuid}.mp4) and:
        1. Extracts parent_id from filename
        2. Gets local file size
        3. Fetches post details from web API
        4. Matches by file size to find exact video_id
        5. Generates new filename: grok-video_{parent_id}_{video_id}.mp4

        Args:
            local_path: Path to local video file
                       (e.g., "/path/to/grok-video-0c5c5864-fadb-440b-a52b-e441dab973d3.mp4")

        Returns:
            VideoMatchResult with parent_id, video_id, mode, new_filename, etc.

        Raises:
            GrokAPIError: If file not found, invalid filename, or no match found

        Example:
            >>> result = client.match_local_video("/path/to/grok-video-xxx.mp4")
            >>> print(f"Rename to: {result.new_filename}")
            >>> print(f"Mode: {result.mode.value}")
            >>> print(f"Is parent video: {result.is_parent_video}")
        """
        local_path = Path(local_path)

        # Validate file exists
        if not local_path.exists():
            raise GrokAPIError(f"File not found: {local_path}")

        # Extract parent_id from filename
        # Pattern: grok-video-{UUID}.mp4 or grok-video-{UUID} (1).mp4
        filename = local_path.name
        match = re.match(
            r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            filename,
        )
        if not match:
            raise GrokAPIError(
                f"Invalid filename format. Expected 'grok-video-{{uuid}}.mp4', got: {filename}"
            )

        parent_id = match.group(1)
        local_size = local_path.stat().st_size

        # Fetch post details
        details = self.get_post_details(parent_id)

        # Build list of all videos to check
        videos_to_check = []

        # For txt2vid: parent itself is a video
        if details.mode == GenerationMode.TEXT_TO_VIDEO and details.hd_media_url:
            videos_to_check.append({
                "video_id": details.id,
                "url": details.hd_media_url,
                "is_parent": True,
                "prompt": details.original_prompt,
            })

        # Add all children
        for child in details.children:
            url = child.hd_media_url or child.media_url
            if url:
                videos_to_check.append({
                    "video_id": child.id,
                    "url": url,
                    "is_parent": False,
                    "prompt": child.original_prompt,
                })

        # Match by file size
        for video in videos_to_check:
            try:
                web_size = self.get_asset_file_size(video["url"])
                if web_size == local_size:
                    # Found match!
                    new_filename = f"grok-video_{parent_id}_{video['video_id']}.mp4"
                    return VideoMatchResult(
                        parent_id=parent_id,
                        video_id=video["video_id"],
                        is_parent_video=video["is_parent"],
                        mode=details.mode,
                        original_prompt=video["prompt"],
                        file_size=local_size,
                        new_filename=new_filename,
                    )
            except Exception:
                continue  # Try next video if size fetch fails

        # No match found
        raise GrokAPIError(
            f"No matching video found on web for local file: {filename}\n"
            f"Local size: {local_size} bytes\n"
            f"Parent ID: {parent_id}\n"
            f"Videos checked: {len(videos_to_check)}"
        )

    def like_post(self, post_id: str) -> bool:
        """
        Like a post to save it to favorites.

        This is the ONLY way to keep posts accessible long-term in Grok Imagine.
        Unliked posts will eventually be removed from all views.

        Args:
            post_id: Post UUID to like

        Returns:
            True if successful

        Raises:
            GrokAuthError: If authentication fails
            GrokAPIError: If request fails
        """
        self._api_request("POST", "/rest/media/post/like", {"id": post_id})
        return True

    def unlike_post(self, post_id: str) -> bool:
        """
        Unlike a post to remove it from favorites.

        WARNING: This effectively deletes the post from your view.
        There is no way to recover it from the UI after unliking.

        Args:
            post_id: Post UUID to unlike

        Returns:
            True if successful

        Raises:
            GrokAuthError: If authentication fails
            GrokAPIError: If request fails
        """
        self._api_request("POST", "/rest/media/post/unlike", {"id": post_id})
        return True

    def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 6,
    ) -> dict[str, Any]:
        """
        Generate a video from an image using Grok's chat API.

        This triggers video generation (img2vid) by calling the chat interface
        with a special videoGen tool override.

        Args:
            image_url: Full URL to image on imagine-public.x.ai
            parent_post_id: Parent image post UUID (for linking video as child)
            aspect_ratio: Video aspect ratio. Options: "2:3", "16:9", etc. Default: "2:3"
            video_length: Video duration in seconds. Typical: 6 or 15. Default: 6

        Returns:
            Chat response dict (contains conversation ID and stream info)

        Raises:
            GrokAuthError: If authentication fails
            GrokAPIError: If request fails

        Example:
            >>> client = GrokClient()
            >>> response = client.create_video_from_image(
            ...     image_url="https://imagine-public.x.ai/imagine-public/images/uuid.png",
            ...     parent_post_id="uuid",
            ...     aspect_ratio="2:3",
            ...     video_length=6
            ... )
            >>> # Monitor chat response stream for completion
            >>> # Then verify video in parent post's childPosts via get_post_details()
        """
        # Construct chat message with image URL
        message = f"{image_url}  --mode=normal"

        # Build chat API payload
        payload = {
            "temporary": True,
            "modelName": "grok-3",
            "message": message,
            "toolOverrides": {"videoGen": True},
            "responseMetadata": {
                "experiments": [],
                "modelConfigOverride": {
                    "modelMap": {
                        "videoGenModelConfig": {
                            "parentPostId": parent_post_id,
                            "aspectRatio": aspect_ratio,
                            "videoLength": video_length,
                        }
                    }
                },
            },
        }

        # Call chat API
        return self._api_request("POST", "/rest/app-chat/conversations/new", payload)

    # =========================================================================
    # Internal methods
    # =========================================================================

    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self._session.request(method, url, json=json_data)
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}")

        if response.status_code in (401, 403):
            raise GrokAuthError(
                "Request blocked (401/403). Most likely causes:\n"
                "1. TLS fingerprint mismatch - try updating impersonate version "
                "(chrome136 -> newer)\n"
                "2. Headers outdated - update sec-ch-ua to match current Chrome\n"
                "3. Cookie expiration (RARE) - update ~/.grok-config.json\n"
                "Note: Cloudflare blocks by TLS fingerprint, not just cookies!"
            )

        if response.status_code == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status_code >= 400:
            raise GrokAPIError(f"API error: {response.status_code}")

        try:
            return response.json()
        except ValueError:
            return {}

    def _detect_generation_mode(self, post_data: dict) -> GenerationMode:
        """
        Detect generation mode from post metadata.

        Logic:
        - MEDIA_POST_TYPE_VIDEO + mode=text → TEXT_TO_VIDEO
        - MEDIA_POST_TYPE_IMAGE + prompt exists → GROK_IMAGE_TO_VIDEO
        - MEDIA_POST_TYPE_IMAGE + no prompt → UPLOAD_IMAGE_TO_VIDEO
        """
        media_type = post_data.get("mediaType", "")
        prompt = post_data.get("prompt")
        mode = post_data.get("mode")

        if media_type == "MEDIA_POST_TYPE_VIDEO":
            if mode == "text":
                return GenerationMode.TEXT_TO_VIDEO
            return GenerationMode.UNKNOWN

        if media_type == "MEDIA_POST_TYPE_IMAGE":
            if prompt:
                return GenerationMode.GROK_IMAGE_TO_VIDEO
            else:
                return GenerationMode.UPLOAD_IMAGE_TO_VIDEO

        return GenerationMode.UNKNOWN

    def _parse_timestamp(self, value: Any) -> datetime | None:
        """Parse ISO timestamp string to datetime."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            # Handle 'Z' suffix
            if isinstance(value, str):
                value = value.replace("Z", "+00:00")
                return datetime.fromisoformat(value)
        except Exception:
            pass
        return None

    def _parse_post_summary(self, data: dict) -> PostSummary:
        """Parse API response into PostSummary."""
        mode = self._detect_generation_mode(data)

        # Get prompt for preview
        prompt = data.get("prompt") or data.get("originalPrompt") or ""
        prompt_preview = prompt[:100] if prompt else None

        # Count child videos
        child_posts = data.get("childPosts", [])
        video_count = sum(
            1 for c in child_posts
            if c.get("mediaType") == "MEDIA_POST_TYPE_VIDEO"
        )

        return PostSummary(
            id=data.get("id", ""),
            mode=mode,
            prompt_preview=prompt_preview,
            video_count=video_count,
            created_at=self._parse_timestamp(data.get("createTime")),
            media_type=data.get("mediaType"),
        )

    def _parse_post_details(
        self,
        data: dict,
        post_id: str,
        raw_data: dict | None = None,
    ) -> PostDetails:
        """Parse API response into PostDetails with all children."""
        mode = self._detect_generation_mode(data)

        # Parse child videos
        children = []
        for child in data.get("childPosts", []):
            if child.get("mediaType") == "MEDIA_POST_TYPE_VIDEO":
                child_video = ChildVideo(
                    id=child.get("id", ""),
                    parent_id=child.get("originalPostId", post_id),
                    original_prompt=child.get("originalPrompt"),
                    media_url=child.get("mediaUrl"),
                    hd_media_url=child.get("hdMediaUrl"),
                    thumbnail_url=child.get("thumbnailImageUrl"),
                    created_at=self._parse_timestamp(child.get("createTime")),
                    resolution=child.get("resolution"),
                    duration=child.get("duration"),
                    model_name=child.get("modelName"),
                    mode=child.get("mode"),
                )
                children.append(child_video)

        return PostDetails(
            id=data.get("id", post_id),
            user_id=data.get("userId"),
            mode=mode,
            media_type=data.get("mediaType"),
            prompt=data.get("prompt"),
            original_prompt=data.get("originalPrompt"),
            media_url=data.get("mediaUrl"),
            hd_media_url=data.get("hdMediaUrl"),
            thumbnail_url=data.get("thumbnailImageUrl"),
            created_at=self._parse_timestamp(data.get("createTime")),
            resolution=data.get("resolution"),
            model_name=data.get("modelName"),
            children=children,
            raw_data=raw_data,
        )
