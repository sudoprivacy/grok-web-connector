"""
Integration tests for grok-web-connector real workflows.

Generated from scenarios.json following integration-test-generator conventions.
Hand-written because generate.py does not support async/await.

Requires:
- Real Chrome browser (launched automatically by ai-dev-browser)
- Valid Grok credentials in ~/.grok-config.json
- Network access to grok.com

Run with: pytest tests/integration/test_workflows.py -v
Run single: pytest tests/integration/test_workflows.py::test_generate_video_720p_10s -v -s

Coverage: 5/5 real workflows (100%)
  - Browse → Detail → Favorite → Verify → Unfavorite
  - Create Video (720p, 10s) → Verify Resolution → Delete
  - Create Video → Download → Match Local → Delete
  - Create Image → Create Video (img2vid) → Verify
  - List → Detail → Like (social feedback)
"""

import os
from pathlib import Path

import pytest

from grok_web import (
    ImageGenerationResult,
    PostDetails,
    VideoGenerationResult,
    VideoMatchResult,
    get_client,
)

# ---------------------------------------------------------------------------
# Integration guard: allow CI to opt-out with SKIP_INTEGRATION=1
# ---------------------------------------------------------------------------
SKIP_INTEGRATION = os.environ.get("SKIP_INTEGRATION", "").lower() in ("1", "true", "yes")

# Test image post — a known image that can be used for video generation.
# Override with TEST_POST_ID env var if needed.
TEST_POST_ID = os.environ.get("TEST_POST_ID", "9ac51419-65c8-467c-958e-97e9f1abadfa")


@pytest.fixture
async def client():
    """Create a real GrokClient for integration tests.

    Also serves as the integration guard — skips if SKIP_INTEGRATION is set.
    Tests that don't need a client (e.g. smoke test) won't be skipped.
    """
    if SKIP_INTEGRATION:
        pytest.skip("SKIP_INTEGRATION is set — skipping integration tests")
    async with get_client() as c:
        yield c


# Smoke test - can import without errors
def test_imports_work():
    """Verify all imports are valid."""
    assert get_client is not None
    assert PostDetails is not None
    assert VideoGenerationResult is not None
    assert ImageGenerationResult is not None
    assert VideoMatchResult is not None


@pytest.mark.integration
async def test_browse_and_favorite(client):
    """
    Real scenario: Browse gallery, inspect a post, and favorite it

    Workflow: list_posts → get_post_details → favorite_post → verify in favorites → unfavorite_post

    User problem: User browses their gallery, finds an interesting image, saves it
    to favorites for later, then un-favorites when done.

    Data flow:
      1. list_posts returns PostSummary list — pick first post ID
      2. get_post_details uses that ID to fetch full details (children, media URLs)
      3. favorite_post saves the post to favorites
      4. list_posts(favorites) verifies the post appears in favorites list
      5. unfavorite_post removes the post (cleanup)
    """
    # Step 1: List recent posts
    posts = await client.list_posts(limit=5, source="all")
    assert len(posts) > 0, "Should have at least one post"
    post_id = posts[0].id
    assert post_id, "Post should have a valid ID"

    # Step 2: Get full details
    details = await client.get_post_details(post_id)
    assert isinstance(details, PostDetails)
    assert details.id == post_id
    assert details.media_url, "Post should have a media URL"

    # Step 3: Favorite the post
    fav_ok = await client.favorite_post(post_id)
    assert fav_ok is True

    try:
        # Step 4: Verify it appears in favorites
        favs = await client.list_posts(limit=50, source="favorites")
        fav_ids = [p.id for p in favs]
        assert post_id in fav_ids, f"Post {post_id} should appear in favorites"
    finally:
        # Step 5: Cleanup — unfavorite
        await client.unfavorite_post(post_id)


@pytest.mark.integration
async def test_generate_video_720p_10s(client):
    """
    Real scenario: Generate 720p 10s video from an existing image

    Workflow: create_video(720p, 10s) → get_post_details → verify resolution → delete_video

    User problem: User wants to animate an existing Grok image into a high-quality
    720p 10-second video and verify the output resolution.

    Data flow:
      1. create_video generates a 720p 10s video from the test image post
      2. get_post_details fetches parent to find the new child video
      3. Verify child video has 720p resolution (width ~784)
      4. delete_video cleans up the generated video
    """
    # Step 1: Create video with 720p + 10s settings
    result = await client.create_video(
        source_post_id=TEST_POST_ID,
        duration=10,
        resolution="720p",
    )
    assert isinstance(result, VideoGenerationResult)
    assert result.video_id, "Should return a video ID"

    try:
        # Step 2: Verify via post details
        details = await client.get_post_details(TEST_POST_ID)
        assert isinstance(details, PostDetails)

        # Step 3: Find our video and check resolution
        child = next((c for c in details.children if c.id == result.video_id), None)
        assert (
            child is not None
        ), f"Video {result.video_id} should appear as child of {TEST_POST_ID}"
        if child.resolution:
            # 720p portrait: 784x1168; 720p landscape: 1168x784
            assert (
                child.resolution["width"] >= 720 or child.resolution["height"] >= 720
            ), f"720p video should have at least one dimension >= 720, got {child.resolution}"
    finally:
        # Step 4: Cleanup — delete the generated video
        if result.video_id:
            await client.delete_video(result.video_id)


@pytest.mark.integration
async def test_generate_download_and_match(client, tmp_path):
    """
    Real scenario: Generate video, download it, then match the local file back to Grok

    Workflow: create_video → download_video → match_local_video → verify match → delete_video

    User problem: User generates a video, downloads it locally, and later wants to
    find the original Grok post from the local file.

    Data flow:
      1. create_video generates a quick 480p 6s video
      2. download_video downloads the video to a temp file
      3. match_local_video identifies the Grok post from the downloaded file
      4. Verify match.parent_id and match.video_id are correct
      5. delete_video cleans up
    """
    # Step 1: Generate a quick video
    result = await client.create_video(
        source_post_id=TEST_POST_ID,
        duration=6,
        resolution="480p",
    )
    assert isinstance(result, VideoGenerationResult)
    assert result.video_id, "Should return a video ID"

    try:
        # Step 2: Download the video
        download_path = tmp_path / "test_video.mp4"
        saved_path = await client.download_video(
            result.video_id,
            str(download_path),
            parent_post_id=TEST_POST_ID,
        )
        assert Path(saved_path).exists(), "Downloaded file should exist"
        assert Path(saved_path).stat().st_size > 1000, "Downloaded file should not be empty"

        # Step 3: Match the local file back to Grok
        match = await client.match_local_video(str(saved_path))
        assert isinstance(match, VideoMatchResult)
        assert match.video_id == result.video_id, "Matched video ID should equal generated video ID"
        assert match.parent_id == TEST_POST_ID, "Matched parent ID should equal source post ID"
    finally:
        # Step 4: Cleanup
        if result.video_id:
            await client.delete_video(result.video_id)


@pytest.mark.integration
async def test_text_to_image_to_video(client):
    """
    Real scenario: Create image from text, then animate it into a video

    Workflow: create_image → create_video(img2vid) → get_post_details → verify

    User problem: User has a creative idea, generates an image from text, then
    animates the result into a video — the full creative pipeline.

    Data flow:
      1. create_image generates an image from text prompt, returns post IDs
      2. create_video animates the generated image into a video
      3. get_post_details verifies the video appears as a child of the image
    """
    # Step 1: Create image from text
    image_result = await client.create_image(
        "A serene lake at sunset with mountains in the background"
    )
    assert isinstance(image_result, ImageGenerationResult)
    assert image_result.success, "Image generation should succeed (at least 1 non-moderated)"
    assert len(image_result.post_ids) > 0, "Should return at least one post ID"
    image_post_id = image_result.post_ids[0]

    # Step 2: Generate video from the image
    video_result = await client.create_video(
        source_post_id=image_post_id,
        preset="normal",
        duration=6,
    )
    assert isinstance(video_result, VideoGenerationResult)
    assert video_result.video_id, "Should return a video ID"

    # Step 3: Verify video appears as child
    details = await client.get_post_details(image_post_id)
    assert isinstance(details, PostDetails)
    child_ids = [c.id for c in details.children]
    assert (
        video_result.video_id in child_ids
    ), f"Video {video_result.video_id} should appear as child of {image_post_id}"


@pytest.mark.integration
async def test_social_feedback_workflow(client):
    """
    Real scenario: Review posts and give social feedback

    Workflow: list_posts → get_post_details → like_post

    User problem: User reviews their generated content, gives thumbs up to
    good results for Grok's feedback system.

    Data flow:
      1. list_posts returns recent posts to review
      2. get_post_details shows all child videos for evaluation
      3. like_post gives thumbs up to a video the user approves of
    """
    # Step 1: List recent posts
    posts = await client.list_posts(limit=5, source="all")
    assert len(posts) > 0, "Should have at least one post"
    post_id = posts[0].id

    # Step 2: Get details to see child videos
    details = await client.get_post_details(post_id)
    assert isinstance(details, PostDetails)

    # Step 3: Like a child video (or the parent if no children)
    target_id = details.children[0].id if details.children else post_id
    liked = await client.like_post(target_id)
    assert liked is True
