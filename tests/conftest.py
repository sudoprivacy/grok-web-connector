"""Pytest fixtures for grok_web tests."""

import pytest
from datetime import datetime, timezone

from grok_web.models import GrokCookies


@pytest.fixture
def mock_cookies() -> GrokCookies:
    """Mock cookies for testing."""
    return GrokCookies(
        sso="mock_sso_token",
        **{"sso-rw": "mock_sso_rw_token"},
        **{"x-userid": "mock_user_id"},
        cf_clearance="mock_cf_clearance",
    )


@pytest.fixture
def sample_post_data() -> dict:
    """Sample post data from API."""
    return {
        "id": "test-post-id-1234",
        "userId": "user-123",
        "mediaType": "MEDIA_POST_TYPE_IMAGE",
        "prompt": "A beautiful sunset over the ocean",
        "originalPrompt": "A beautiful sunset over the ocean",
        "mediaUrl": "https://assets.grok.com/image.jpg",
        "hdMediaUrl": "https://assets.grok.com/image_hd.jpg",
        "thumbnailImageUrl": "https://assets.grok.com/thumb.jpg",
        "createTime": "2025-12-10T10:30:00Z",
        "resolution": {"width": 1920, "height": 1080},
        "modelName": "aurora",
        "childPosts": [
            {
                "id": "child-video-id-1",
                "originalPostId": "test-post-id-1234",
                "mediaType": "MEDIA_POST_TYPE_VIDEO",
                "originalPrompt": "Make it move",
                "mediaUrl": "https://assets.grok.com/video.mp4",
                "hdMediaUrl": "https://assets.grok.com/video_hd.mp4",
                "thumbnailImageUrl": "https://assets.grok.com/video_thumb.jpg",
                "createTime": "2025-12-10T10:35:00Z",
                "resolution": {"width": 1920, "height": 1080},
                "duration": 6,
                "modelName": "mochi",
                "mode": "normal",
            },
            {
                "id": "child-video-id-2",
                "originalPostId": "test-post-id-1234",
                "mediaType": "MEDIA_POST_TYPE_VIDEO",
                "originalPrompt": "Zoom in slowly",
                "mediaUrl": "https://assets.grok.com/video2.mp4",
                "hdMediaUrl": "https://assets.grok.com/video2_hd.mp4",
                "thumbnailImageUrl": "https://assets.grok.com/video2_thumb.jpg",
                "createTime": "2025-12-10T10:40:00Z",
                "resolution": {"width": 1920, "height": 1080},
                "duration": 6,
                "modelName": "mochi",
                "mode": "normal",
            },
        ],
    }


@pytest.fixture
def sample_text_to_video_post() -> dict:
    """Sample text-to-video post data."""
    return {
        "id": "txt2vid-post-id",
        "userId": "user-123",
        "mediaType": "MEDIA_POST_TYPE_VIDEO",
        "prompt": "A cat playing piano",
        "originalPrompt": "A cat playing piano",
        "mediaUrl": "https://assets.grok.com/txt2vid.mp4",
        "hdMediaUrl": "https://assets.grok.com/txt2vid_hd.mp4",
        "thumbnailImageUrl": "https://assets.grok.com/thumb.jpg",
        "createTime": "2025-12-10T12:00:00Z",
        "resolution": {"width": 1920, "height": 1080},
        "modelName": "mochi",
        "mode": "text",
        "childPosts": [],
    }


@pytest.fixture
def sample_list_response(sample_post_data: dict, sample_text_to_video_post: dict) -> dict:
    """Sample list posts API response."""
    return {
        "posts": [sample_post_data, sample_text_to_video_post],
    }


@pytest.fixture
def sample_get_response(sample_post_data: dict) -> dict:
    """Sample get post details API response."""
    return {
        "post": sample_post_data,
    }
