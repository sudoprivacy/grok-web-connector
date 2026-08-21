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
        assert post_id in {p.id for p in favs}, (
            f"{post_id} missing from favorites list after favorite_post"
        )
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
        assert result.video_id in child_ids, (
            f"video {result.video_id} should appear under {TEST_SOURCE_POST_ID}"
        )
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
    assert first.image_file_ids, (
        "first pass must expose image_file_ids so the retry path can reuse them"
    )

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
    assert second.video_id != first.video_id, (
        "second call must create a distinct video, not return the first one"
    )

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
            {
                "video_id": gen.video_id,
                "output_path": str(out),
            }
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


# ---------------------------------------------------------------------------
# Scenario: segment_generated_image (2026-08 Segments / 分段)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_segment_generated_image(client):
    """create_image -> get_segments: generate a multi-object image and confirm
    Grok auto-detects labeled objects with boxes + masks (data flows
    image_id -> get_segments)."""
    gen = await client.create_image(
        {
            "prompt": (
                "a red apple, a yellow banana, and a green pear on a plain white table, flat lay"
            ),
            "min_success": 1,
            "max_scroll": 3,
        }
    )
    clean = [i for i in gen.images if not i.get("moderated")]
    assert clean, "need a non-moderated image to segment"
    image_id = clean[0]["image_id"]

    segments = await client.get_segments(f"post:{image_id}")
    assert isinstance(segments, list), "get_segments returns a list"
    assert len(segments) >= 1, "expected at least one detected object"

    seg = segments[0]
    assert {"name", "box", "score", "mask_rle"} <= set(seg), f"segment keys: {list(seg)}"
    assert seg["name"], "segment has a label"
    assert isinstance(seg["box"], list) and len(seg["box"]) == 4, f"box: {seg['box']}"

    labels = " ".join((s.get("name") or "").lower() for s in segments)
    assert any(k in labels for k in ("apple", "banana", "pear")), (
        f"expected fruit labels from the prompt, got: {labels!r}"
    )


# ---------------------------------------------------------------------------
# Scenario: reference_lifecycle (2026-08 References)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_reference_lifecycle(client):
    """create_image -> create_reference (in-Grok) -> list_references ->
    delete_reference. Confirms a Grok image_id works as an assetId and the
    reference round-trips (created id appears in list, gone after delete)."""
    gen = await client.create_image(
        {"prompt": "a bright red apple on white, product photo", "min_success": 1, "max_scroll": 3}
    )
    clean = [i for i in gen.images if not i.get("moderated")]
    assert clean, "need a non-moderated image"
    image_id = clean[0]["image_id"]

    ref = await client.create_reference(
        {"images": [f"post:{image_id}"], "title": "itest-ref", "category": "character"}
    )
    ref_id = ref["reference_id"]
    assert ref_id, f"create_reference returned no id: {ref}"
    assert ref["asset_ids"] == [image_id], f"asset_ids: {ref['asset_ids']}"

    try:
        refs = await client.list_references(limit=50)
        assert isinstance(refs, list)
        assert any(r["reference_id"] == ref_id for r in refs), "created ref missing from list"
    finally:
        assert await client.delete_reference(ref_id) is True

    refs_after = await client.list_references(limit=50)
    assert not any(r["reference_id"] == ref_id for r in refs_after), (
        "ref still present after delete"
    )


# ---------------------------------------------------------------------------
# Scenario: precise_edit_segment (2026-08 精确编辑 / region inpaint)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_precise_edit_segment(client):
    """create_image -> get_segments -> precise_edit one detected object's
    region. Region-scoped edit returns a new edited image (data flows
    image_id -> segment -> region-scoped edit)."""
    gen = await client.create_image(
        {
            "prompt": "a red apple and a green pear on a plain white table, flat lay",
            "min_success": 1,
            "max_scroll": 3,
        }
    )
    clean = [i for i in gen.images if not i.get("moderated")]
    assert clean, "need a non-moderated image"
    image_id = clean[0]["image_id"]

    segs = await client.get_segments(f"post:{image_id}")
    assert segs, "expected detected segments"
    target = next((s for s in segs if s.get("box") and s.get("mask_rle")), segs[0])

    result = await client.precise_edit(
        {
            "images": [f"post:{image_id}"],
            "prompt": "make it a shiny metallic silver object",
            "region": target,
            "timeout": 150,
        }
    )
    assert result.images, "precise_edit returned no images"
    done = [i for i in result.images if i.get("progress") == 100]
    assert done, f"no completed edit image: {result.images}"
    assert done[0].get("post_id"), "edited image missing post_id"


# ---------------------------------------------------------------------------
# Scenario: reference_to_video (2026-08 reference consumption)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_reference_to_video(client):
    """create_image -> create_reference -> create_video(['ref:<id>']).

    Confirms reference CONSUMPTION: a saved character reference conditions a
    video generation (Grok's referenceToVideo path). Data flows
    image_id -> reference_id -> ref:<id> -> video. Cleans up the reference
    and the generated video.
    """
    gen = await client.create_image(
        {
            "prompt": "a friendly cartoon superhero mascot, full body, plain white background",
            "min_success": 1,
            "max_scroll": 3,
        }
    )
    clean = [i for i in gen.images if not i.get("moderated")]
    assert clean, "need a non-moderated hero image"
    image_id = clean[0]["image_id"]

    ref = await client.create_reference(
        {"images": [f"post:{image_id}"], "title": "itest-hero", "category": "character"}
    )
    ref_id = ref["reference_id"]
    assert ref_id, f"create_reference returned no id: {ref}"

    video = None
    try:
        video = await client.create_video(
            {
                "images": [f"ref:{ref_id}"],
                "prompt": "the hero waves hello in a sunny park",
                "resolution": "480p",
                "duration": "6s",
            }
        )
        assert isinstance(video, VideoGenerationResult)
        assert video.video_id, "reference-conditioned gen must return a video_id"
        assert video.mode == "reference", f"expected mode='reference', got {video.mode!r}"
    finally:
        if video and video.video_id:
            with contextlib.suppress(Exception):
                await client.delete_video(video.video_id)
        with contextlib.suppress(Exception):
            await client.delete_reference(ref_id)


# ---------------------------------------------------------------------------
# Scenario: reference_rejected_for_image (guardrail — references are video-only)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_reference_rejected_for_image(client):
    """create_image / edit_image must reject 'ref:<id>' with a clear pointer
    to create_video — references are consumed by video generation only."""
    from grok_web import GrokAPIError

    with pytest.raises(GrokAPIError, match="create_video"):
        await client.create_image(
            {"images": ["ref:00000000-0000-0000-0000-000000000000"], "prompt": "x"}
        )

    with pytest.raises(GrokAPIError, match="create_video"):
        await client.edit_image(
            {
                "images": ["post:11111111-1111-1111-1111-111111111111", "ref:2222"],
                "prompt": "x",
            }
        )


# ---------------------------------------------------------------------------
# Scenario: create_image_in_grok_img2img (2026-08 in-Grok image->image)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_create_image_in_grok_img2img(client):
    """create_image with a single Grok-native ref → in-Grok imageToImage.

    A 'post:<id>' reference is served via edit_image's imageToImage path —
    referencing the Grok image server-side with NO download / re-upload (low
    moderation), producing a NEW image. Data flows image_id -> post: -> new
    image (the 'same character in a new scene' workflow).
    """
    gen = await client.create_image(
        {
            "prompt": "a friendly cartoon superhero mascot, full body, plain white background",
            "min_success": 1,
            "max_scroll": 3,
        }
    )
    clean = [i for i in gen.images if not i.get("moderated")]
    assert clean, "need a non-moderated hero image"
    hero_id = clean[0]["image_id"]

    out = await client.create_image(
        {
            "images": [f"post:{hero_id}"],
            "prompt": "the same character sitting on a park bench in a green park, wide shot",
            "min_success": 1,
            "max_scroll": 3,
        }
    )
    assert isinstance(out, ImageGenerationResult)
    done = [i for i in out.images if not i.get("moderated")]
    assert done, "in-Grok img2img returned no non-moderated image"
    assert done[0].get("image_id"), "edited image missing image_id"
    assert done[0]["image_id"] != hero_id, "should be a NEW image, not the source"


# ---------------------------------------------------------------------------
# Scenario: create_image_in_grok_multi_compose (2026-08 multi-asset img2img)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_create_image_in_grok_multi_compose(client):
    """create_image with MULTIPLE Grok-native refs → in-Grok imageToImage
    composition, referenced BY ID (no gallery, works for ephemeral gens).

    Needs a warm conversations/new statsig token, which create_image /
    edit_image do NOT mint — so we first run a create_video (which does), then
    compose. Data flows (id_a, id_b) -> [post:a, post:b] -> composed image.
    """
    a = await client.create_image(
        {
            "prompt": "a red cartoon superhero mascot, full body, plain white background",
            "min_success": 1,
            "max_scroll": 3,
        }
    )
    b = await client.create_image(
        {
            "prompt": "a blue cartoon robot mascot, full body, plain white background",
            "min_success": 1,
            "max_scroll": 3,
        }
    )
    clean_a = [i for i in a.images if not i.get("moderated")]
    clean_b = [i for i in b.images if not i.get("moderated")]
    assert clean_a and clean_b, "need two non-moderated source images"
    id_a, id_b = clean_a[0]["image_id"], clean_b[0]["image_id"]

    # Warm the compose token (create_video POSTs conversations/new).
    warm = await client.create_video(
        {"prompt": "a waving cartoon character", "duration": "6s", "resolution": "480p"}
    )
    try:
        out = await client.create_image(
            {
                "images": [f"post:{id_a}", f"post:{id_b}"],
                "prompt": "the two characters standing side by side in a sunny park, wide shot",
            }
        )
        assert isinstance(out, ImageGenerationResult)
        done = [i for i in out.images if not i.get("moderated")]
        assert done, "multi-ref composition returned no non-moderated image"
        assert done[0].get("image_id"), "composed image missing image_id"
        assert done[0]["image_id"] not in (id_a, id_b), "should be a NEW composed image"
    finally:
        if warm.video_id:
            with contextlib.suppress(Exception):
                await client.delete_video(warm.video_id)


@pytest.mark.integration
async def test_create_image_multi_compose_cold_hint(client):
    """When the compose token is cold, multi-ref composition raises a clear
    warming hint (Failure -> hint) rather than silently failing."""
    from grok_web import GrokAPIError

    # Fresh client: no create_video has warmed conversations/new yet.
    if client._statsig_snitch is not None:
        client._statsig_snitch._by_endpoint.pop("/rest/app-chat/conversations/new", None)
    with pytest.raises(GrokAPIError, match="create_video|warm"):
        await client._create_image_via_imagetoimage(
            ["00000000-0000-0000-0000-000000000000", "11111111-1111-1111-1111-111111111111"],
            "compose",
            30,
        )
