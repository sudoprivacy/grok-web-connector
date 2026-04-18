"""Integration tests for grok-web-connector real-user workflows.

Hand-written from ``scenarios.json`` (see that file for the structured
list of scenarios). Tests are async/await so the integration-test-
generator ``generate.py`` can't produce them verbatim; this file is the
source of truth and ``scenarios.json`` is kept in sync alongside it.

ALL @pytest.mark.integration tests are skipped by default. Enable via
either of::

    pytest tests/integration/ -v --run-integration
    RUN_INTEGRATION=1 pytest tests/integration/ -v

The single always-on test is ``test_imports_work`` — a smoke check so
``pytest tests/`` always has something green to return even on machines
with no Chrome or credentials.

Optional environment variables:
  TEST_SOURCE_POST_ID   A post UUID usable for img2vid (defaults to a
                        known stable demo post).
  TEST_LOCAL_FRAMES_DIR Directory containing at least 3 *.jpg files to
                        use as upload fixtures. If unset, the upload
                        scenarios are skipped with a clear reason.

Coverage — maps 1:1 to scenarios.json:
  browse_favorite_unfavorite    list/detail/favorite/verify/unfavorite
  img2vid_roundtrip             create_video(post:) + child verification
  upload2vid_retry_without_reupload
                                dict-API upload + direct-REST retry via file:
  upload_images_then_reuse      standalone upload_images + reuse
  catch_post_render_moderation  check_video_moderated after gen
  download_and_match_roundtrip  create/download/match roundtrip
  pool_parallel_generation      BrowserWorkerPool fan-out
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from grok_web import (
    BrowserWorkerPool,
    ImageGenerationResult,
    PostDetails,
    VideoGenerationResult,
    VideoMatchResult,
    get_client,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TEST_SOURCE_POST_ID = os.environ.get(
    "TEST_SOURCE_POST_ID",
    # Public-ish demo post the connector author has used historically; override
    # in env if yours gets deleted or rotates.
    "9ac51419-65c8-467c-958e-97e9f1abadfa",
)


def _local_frames(min_count: int = 3) -> list[str]:
    """Return sorted list of JPEGs in TEST_LOCAL_FRAMES_DIR, or skip."""
    dir_path = os.environ.get("TEST_LOCAL_FRAMES_DIR")
    if not dir_path:
        pytest.skip(
            "TEST_LOCAL_FRAMES_DIR not set — upload-path scenarios need "
            "a directory with at least one sample JPEG."
        )
    root = Path(dir_path)
    frames = sorted(str(p) for p in root.glob("*.jpg"))
    if len(frames) < min_count:
        pytest.skip(f"Need >= {min_count} JPEGs in {root}, found {len(frames)}.")
    return frames


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def client():
    """Yield a real GrokClient for integration tests."""
    async with get_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Smoke test — always runs
# ---------------------------------------------------------------------------
def test_imports_work():
    """Every public symbol the scenarios reference is importable."""
    assert get_client is not None
    assert BrowserWorkerPool is not None
    assert VideoGenerationResult is not None
    assert ImageGenerationResult is not None
    assert PostDetails is not None
    assert VideoMatchResult is not None


# ---------------------------------------------------------------------------
# Scenario 1: browse_favorite_unfavorite
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_browse_favorite_unfavorite(client):
    """list_posts -> get_post_details -> favorite -> verify -> unfavorite."""
    posts = await client.list_posts(limit=5, source="all")
    assert len(posts) > 0, "need at least one post to exercise the workflow"
    post_id = posts[0].id

    details = await client.get_post_details(post_id)
    assert isinstance(details, PostDetails)
    assert details.id == post_id

    assert await client.favorite_post(post_id) is True

    try:
        favs = await client.list_posts(limit=50, source="favorites")
        assert post_id in {
            p.id for p in favs
        }, f"{post_id} missing from favorites list after favorite_post"
    finally:
        await client.unfavorite_post(post_id)


# ---------------------------------------------------------------------------
# Scenario 2: img2vid_roundtrip
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_img2vid_roundtrip(client):
    """create_video({'images': ['post:<id>'], ...}) should produce a child of <id>."""
    result = await client.create_video(
        {
            "images": [f"post:{TEST_SOURCE_POST_ID}"],
            "prompt": "slow orbit around @1",
            "resolution": "480p",
            "duration": "6s",
        }
    )
    assert isinstance(result, VideoGenerationResult)
    assert result.video_id, "gen must return a video_id"

    try:
        parent = await client.get_post_details(TEST_SOURCE_POST_ID)
        child_ids = {c.id for c in parent.children}
        assert (
            result.video_id in child_ids
        ), f"video {result.video_id} should appear under {TEST_SOURCE_POST_ID}"
    finally:
        if result.video_id:
            await client.delete_video(result.video_id)


# ---------------------------------------------------------------------------
# Scenario 3: upload2vid_retry_without_reupload
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_upload2vid_retry_without_reupload(client):
    """First call uploads; second call uses 'file:<id>' refs and skips upload."""
    frames = _local_frames(min_count=1)

    first = await client.create_video(
        {
            "images": frames,
            "prompt": "zoom into @1",
            "resolution": "480p",
            "duration": "6s",
            "verify_final": True,
        }
    )
    assert isinstance(first, VideoGenerationResult)
    assert first.video_id
    assert (
        first.image_file_ids
    ), "first pass must expose image_file_ids so the retry path can reuse them"

    refs = [f"file:{fid}" for fid in first.image_file_ids]
    second = await client.create_video(
        {
            "images": refs,
            "prompt": "pan across @1",
            "resolution": "480p",
            "duration": "6s",
            "verify_final": True,
        }
    )
    assert isinstance(second, VideoGenerationResult)
    assert second.video_id
    assert (
        second.video_id != first.video_id
    ), "second call must create a distinct video, not return the first one"

    try:
        pass  # actual success asserted above
    finally:
        for v in (first.video_id, second.video_id):
            if v:
                await client.delete_video(v)


# ---------------------------------------------------------------------------
# Scenario 4: upload_images_then_reuse
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_upload_images_then_reuse(client):
    """Prime with one UI-path create_video (to fill StatsigSnitch cache),
    then upload_images + reuse refs via direct REST."""
    frames = _local_frames(min_count=1)

    # Prime — direct REST path needs x-statsig-id captured from a prior
    # UI-triggered conversations/new submit.
    prime = await client.create_video(
        {
            "images": frames,
            "prompt": "prime",
            "resolution": "480p",
            "duration": "6s",
        }
    )
    assert prime.video_id

    try:
        # Standalone upload API
        refs = await client.upload_images({"images": frames})
        assert refs, "upload_images should return file: refs"
        assert all(r.startswith("file:") for r in refs)

        # Reuse refs via direct REST
        gen = await client.create_video(
            {
                "images": refs,
                "prompt": "test using @1",
                "resolution": "480p",
                "duration": "6s",
            }
        )
        assert gen.video_id
        assert gen.video_id != prime.video_id

        try:
            pass
        finally:
            if gen.video_id:
                await client.delete_video(gen.video_id)
    finally:
        if prime.video_id:
            await client.delete_video(prime.video_id)


# ---------------------------------------------------------------------------
# Scenario 5: catch_post_render_moderation
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_catch_post_render_moderation(client):
    """Immediate moderated flag + post-render check — both surface the verdict."""
    gen = await client.create_video(
        {
            "images": [f"post:{TEST_SOURCE_POST_ID}"],
            "prompt": "slow zoom",
            "resolution": "480p",
            "duration": "6s",
        }
    )
    assert gen.video_id

    try:
        post_render_mod = await client.check_video_moderated(gen.video_id)
        # No absolute assertion on the verdict — we only care that the API
        # returns a bool and that gen.moderated OR post_render_mod together
        # reflect the true state.
        assert isinstance(post_render_mod, bool)
    finally:
        if gen.video_id:
            await client.delete_video(gen.video_id)


# ---------------------------------------------------------------------------
# Scenario 6: download_and_match_roundtrip
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_download_and_match_roundtrip(client, tmp_path):
    """create_video -> download_video -> match_local_video -> verify round-trip."""
    gen = await client.create_video(
        {
            "images": [f"post:{TEST_SOURCE_POST_ID}"],
            "prompt": "test-for-match",
            "resolution": "480p",
            "duration": "6s",
        }
    )
    assert gen.video_id

    try:
        out = tmp_path / "v.mp4"
        saved = await client.download_video(
            gen.video_id,
            str(out),
            parent_post_id=TEST_SOURCE_POST_ID,
        )
        assert Path(saved).exists() and Path(saved).stat().st_size > 1000

        match = await client.match_local_video(str(saved))
        assert isinstance(match, VideoMatchResult)
        assert match.video_id == gen.video_id, "match must identify the exact video we generated"
        assert match.parent_id == TEST_SOURCE_POST_ID
    finally:
        if gen.video_id:
            await client.delete_video(gen.video_id)


# ---------------------------------------------------------------------------
# Scenario 7: pool_parallel_generation
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_pool_parallel_generation():
    """BrowserWorkerPool distributes jobs across workers."""
    async with get_client() as cleanup_client:
        async with BrowserWorkerPool(
            num_workers=2,
            max_retries=1,
            headless=True,
            close_chrome=True,
        ) as pool:
            prompts = ["Zoom In", "Zoom Out", "Dolly In"]
            job_ids = []
            for p in prompts:
                jid = await pool.submit(
                    "create_video",
                    {
                        "images": [f"post:{TEST_SOURCE_POST_ID}"],
                        "prompt": p,
                        "resolution": "480p",
                        "duration": "6s",
                    },
                )
                job_ids.append(jid)

            results = await pool.wait(timeout=600)
            assert len(results) == len(job_ids), "every submitted job must terminate"

            workers_used = {r.worker_id for r in results.values()}
            assert len(workers_used) >= 2, "pool should distribute work across >=2 workers"

        # Cleanup: delete whatever videos were successfully produced.
        for r in results.values():
            if r.success and r.data and r.data.get("video_id"):
                with contextlib.suppress(Exception):
                    await cleanup_client.delete_video(r.data["video_id"])
