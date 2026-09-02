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

import asyncio
import contextlib
import os
from pathlib import Path

import pytest

from grok_web import (
    BrowserWorkerPool,
    GrokClient,
    ImageGenerationResult,
    PostDetails,
    VideoGenerationResult,
    VideoMatchResult,
    get_client,
    reap_orphan_chrome,
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


@pytest.fixture
async def extension_client():
    """Yield a GrokClient over the EXTENSION transport (drives the user's real
    Chrome via the bundled bridge extension).

    Skipped when the bridge isn't reachable — needs ai-dev-browser >= 0.34 AND
    a running Chrome with the bridge extension loaded. Never launches its own
    Chrome, so it can't be exercised on headless CI; it's the transport the
    2026-09 English composer-direct video flow was verified on.
    """
    client = GrokClient(transport="extension")
    try:
        entered = await client.__aenter__()
    except Exception as e:  # ImportError (adb<0.34) or no bridge/Chrome
        pytest.skip(f"extension transport unavailable: {e}")
    try:
        yield entered
    finally:
        with contextlib.suppress(Exception):
            await client.__aexit__(None, None, None)


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
# Scenario: extension transport — 2026-09 English composer-direct img2vid
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_extension_composer_img2vid(extension_client):
    """transport='extension': custom-prompt img2vid via the 2026-09 English
    composer-direct flow.

    Real journey: pick a source image -> animate it WITH a prompt -> Grok
    returns a video whose uploaded-source mod signal is populated -> verify
    linkage / clean up. Regression guard for (a) the English composer generate
    button ('Make video', lowercase — distinct from the capital sidebar 'Make
    Video'), (b) the selection-pinned _fill_prompt_robust fill on a
    backgrounded tab, and (c) the is_root_user_uploaded mod-signal plumbing. On
    the real English Chrome, create_video(post:) routes through select_post +
    generate_video_from_current, hitting the composer-direct branch.
    """
    c = extension_client
    result = await c.create_video(
        {
            "images": [f"post:{TEST_SOURCE_POST_ID}"],
            "prompt": "slow cinematic zoom",
            "resolution": "480p",
            "duration": "6s",
        }
    )
    assert isinstance(result, VideoGenerationResult)
    assert result.video_id, "composer-direct img2vid must return a video_id"
    # The 2026-09 uploaded-source signal is always populated (bool); it is only
    # a reliable hard stop when paired with moderated=True.
    assert isinstance(result.is_root_user_uploaded, bool)

    try:
        if not result.moderated and not result.is_root_user_uploaded:
            parent = await c.get_post_details(TEST_SOURCE_POST_ID)
            child_ids = {ch.id for ch in parent.children}
            assert result.video_id in child_ids, (
                f"video {result.video_id} should appear under {TEST_SOURCE_POST_ID}"
            )
    finally:
        if result.video_id and not result.moderated:
            with contextlib.suppress(Exception):
                await c.delete_video(result.video_id)


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


# ---------------------------------------------------------------------------
# Scenario: v0.20 orthogonal API — the role-explicit methods, live
# (img2img / compose / animate / reference_video). Each routes to an already
# live-verified internal path; these confirm the NEW public surface end-to-end.
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_img2img_new_method(client):
    """img2img({'source': 'post:<id>', ...}) — the role-explicit replacement
    for create_image(images=[...]). Same in-Grok imageToImage result, cleaner
    call. Data flows image_id -> source -> new image (no re-upload)."""
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

    out = await client.img2img(
        {
            "source": f"post:{hero_id}",
            "prompt": "the same character sitting on a park bench in a green park, wide shot",
        }
    )
    assert isinstance(out, ImageGenerationResult)
    done = [i for i in out.images if not i.get("moderated")]
    assert done, "img2img returned no non-moderated image"
    assert done[0]["image_id"] != hero_id, "should be a NEW image, not the source"


@pytest.mark.integration
async def test_compose_new_method(client):
    """compose({'sources': [a, b], ...}) — the role-explicit replacement for
    create_image(images=[a, b]). Warm-gated (create_video mints the token)."""
    a = await client.create_image(
        {"prompt": "a red cartoon superhero mascot, plain white background", "min_success": 1}
    )
    b = await client.create_image(
        {"prompt": "a blue cartoon robot mascot, plain white background", "min_success": 1}
    )
    id_a = next(i["image_id"] for i in a.images if not i.get("moderated"))
    id_b = next(i["image_id"] for i in b.images if not i.get("moderated"))

    warm = await client.create_video(
        {"prompt": "a waving cartoon character", "duration": "6s", "resolution": "480p"}
    )
    try:
        out = await client.compose(
            {
                "sources": [id_a, id_b],
                "prompt": "the two characters standing side by side in a sunny park",
            }
        )
        assert isinstance(out, ImageGenerationResult)
        done = [i for i in out.images if not i.get("moderated")]
        assert done, "compose returned no non-moderated image"
        assert done[0]["image_id"] not in (id_a, id_b), "should be a NEW composed image"
    finally:
        if warm.video_id:
            with contextlib.suppress(Exception):
                await client.delete_video(warm.video_id)


@pytest.mark.integration
async def test_animate_new_method(client):
    """animate({'frame': 'post:<id>', ...}) — the role-explicit replacement for
    create_video(images=['post:<id>']) / animate_post. Data flows
    image post -> frame -> video child."""
    video = None
    try:
        video = await client.animate(
            {
                "frame": f"post:{TEST_SOURCE_POST_ID}",
                "prompt": "slow cinematic zoom",
                "duration": "6s",
                "resolution": "480p",
            }
        )
        assert isinstance(video, VideoGenerationResult)
        assert video.video_id, "animate must return a video_id"
    finally:
        if video and video.video_id:
            with contextlib.suppress(Exception):
                await client.delete_video(video.video_id)


@pytest.mark.integration
async def test_can_animate_gates_img2vid(client):
    """can_animate(post_id) reports whether the Make-Video button is present in
    THIS profile — a read-only pre-flight so a batch doesn't 100%-fail into
    GrokModerationError. On a healthy profile the known-good demo post returns
    True (verified live 2026-08 it also returns True for a consumer's reported
    'no-button' post — the button gap is profile-degradation-scoped, not a
    universal connector-chrome trait). A 404 raises rather than returning False.
    """
    from grok_web import GrokAPIError

    assert await client.can_animate(TEST_SOURCE_POST_ID) is True

    with pytest.raises(GrokAPIError):
        await client.can_animate("00000000-0000-4000-8000-000000000000")


@pytest.mark.integration
async def test_reference_video_new_method(client):
    """reference_video({'references': [<id>], ...}) — the role-explicit
    replacement for create_video(images=['ref:<id>']). Data flows
    image_id -> reference_id -> references -> video."""
    gen = await client.create_image(
        {"prompt": "a friendly cartoon superhero mascot, plain white background", "min_success": 1}
    )
    image_id = next(i["image_id"] for i in gen.images if not i.get("moderated"))
    ref = await client.create_reference(
        {"images": [f"post:{image_id}"], "title": "itest-refvid", "category": "character"}
    )
    ref_id = ref["reference_id"]

    video = None
    try:
        video = await client.reference_video(
            {
                "references": [ref_id],
                "prompt": "the hero waves hello in a sunny park",
                "resolution": "480p",
                "duration": "6s",
            }
        )
        assert isinstance(video, VideoGenerationResult)
        assert video.video_id, "reference_video must return a video_id"
        assert video.mode == "reference", f"expected mode='reference', got {video.mode!r}"
    finally:
        if video and video.video_id:
            with contextlib.suppress(Exception):
                await client.delete_video(video.video_id)
        with contextlib.suppress(Exception):
            await client.delete_reference(ref_id)


# ---------------------------------------------------------------------------
# Scenario: ancestry_find_source_image_from_video (post enumeration APIs)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_ancestry_find_source_image_from_video(client):
    """get_post_details(root image) -> pick a video child -> get_post_ancestry
    walks that video back to the root image. The 'I have a video, find its
    SOURCE image' journey. Data flows: image -> child video -> ancestry -> root.
    """
    root = await client.get_post_details(TEST_SOURCE_POST_ID)
    videos = [c for c in root.children if c.is_video]
    assert videos, f"{TEST_SOURCE_POST_ID} should have video children to walk from"
    video_id = videos[0].id

    ancestry = await client.get_post_ancestry(video_id)
    assert ancestry, "a derived video must have at least the root image as ancestor"
    assert ancestry[0].id == TEST_SOURCE_POST_ID, "ancestry[0] is the root SOURCE image"
    assert ancestry[0].media_type == "MEDIA_POST_TYPE_IMAGE", "the source is an image"
    assert all(a.id != video_id for a in ancestry), "queried post is NOT included"


@pytest.mark.integration
async def test_get_post_ancestry_root_returns_empty(client):
    """A root post (no parent) has empty ancestry — it IS the source."""
    ancestry = await client.get_post_ancestry(TEST_SOURCE_POST_ID)
    assert ancestry == [], f"{TEST_SOURCE_POST_ID} is a root → no ancestors"


# ---------------------------------------------------------------------------
# Scenario: list_posts_media_type_filter (client-side media_type)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_list_posts_media_type_filter(client):
    """list_posts(media_type='video'|'image') returns ONLY that type (Grok's
    list endpoint ignores a server-side media-type filter, so it's client-side).
    Cross-checks against the unfiltered list so an empty result can't pass
    vacuously.
    """
    both = await client.list_posts(limit=40, source="favorites")
    have_video = any(p.media_type == "MEDIA_POST_TYPE_VIDEO" for p in both)
    have_image = any(p.media_type == "MEDIA_POST_TYPE_IMAGE" for p in both)

    videos = await client.list_posts(limit=20, source="favorites", media_type="video")
    images = await client.list_posts(limit=20, source="favorites", media_type="image")
    assert all(p.media_type == "MEDIA_POST_TYPE_VIDEO" for p in videos), [
        p.media_type for p in videos
    ]
    assert all(p.media_type == "MEDIA_POST_TYPE_IMAGE" for p in images), [
        p.media_type for p in images
    ]
    if have_video:
        assert videos, "favorites contain video posts but the video filter returned none"
    if have_image:
        assert images, "favorites contain image posts but the image filter returned none"


# ---------------------------------------------------------------------------
# Scenario: list_generations (2026 canvas/conversation own-media enumeration)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_list_generations_enumerates_own_media(client):
    """list_generations() surfaces the user's OWN generated media via the 2026
    app-chat conversation surface — the generations that legacy list_posts
    (LIKED-only) can't see. Verifies real items with fetchable CDN URLs, and
    that find_generation_by_id round-trips a download-filename id.

    Skips (not fails) if the test account has no enumerable generations — the
    surface + extraction are also covered by tests/test_generation_enumeration.
    """
    from grok_web import GenerationMedia

    gens = await client.list_generations(limit=25)
    if not gens:
        pytest.skip("test account has no enumerable generations")
    assert all(isinstance(g, GenerationMedia) for g in gens)
    assert all(g.media_type in ("image", "video", "audio") for g in gens)
    assert all(g.asset_id and g.url.startswith("https://") for g in gens)

    # The enumerated URL must be real + sized (the consumer's size-match relies
    # on this). Use the connector's own auth'd HEAD — assets.grok.com 403s a
    # naked request; get_asset_file_size fetches with the session credentials.
    top = gens[0]
    size = await client.get_asset_file_size(top.url)
    assert size > 0, f"enumerated URL returned no size: {top.url}"

    # Direct by-conversationId fetch: get_conversation_media resolves a
    # conversation by id even when it's outside the windowed list (this is how a
    # downloaded grok-video-<conversationId>.mp4 resolves). The enumerated item's
    # own conversation_id must round-trip to include that item.
    conv_media = await client.get_conversation_media(top.conversation_id)
    assert any(m.asset_id == top.asset_id for m in conv_media), (
        "get_conversation_media did not return the enumerated item"
    )

    # Resolver round-trip: size-match pins the exact asset within its conversation
    # (a conversation may hold many generations). Uses conversationId + size —
    # exactly the downloaded-file resolution path.
    hit = await client.find_generation_by_id(top.conversation_id, size=size)
    assert hit is not None and hit.asset_id == top.asset_id


# ---------------------------------------------------------------------------
# Scenario: create_image_quality_v2 (2026-08 Image 2.0 tier)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_create_image_quality_v2(client):
    """create_image(quality='v2') selects the 2026-08 '质量 (v2.0)' tier
    (Grok Imagine Image 2.0) and generates. Regression guard for the
    label-PREFIX match — the old exact '质量' match silently missed the
    renamed '质量 (v2.0)' chip, leaving the wrong tier selected.
    """
    res = await client.create_image(
        {
            "prompt": "a friendly cartoon superhero mascot, plain white background",
            "quality": "v2",
            "min_success": 1,
            "max_scroll": 2,
        }
    )
    assert isinstance(res, ImageGenerationResult)
    done = [i for i in res.images if not i.get("moderated")]
    assert done, "quality='v2' (Image 2.0 tier) produced no non-moderated image"


@pytest.mark.integration
async def test_create_image_v2_batch_accumulates(client):
    """BR 2026-08: create_image(quality='v2') used to STALL at 4 images — the
    v2 tier delivers a FIXED batch (~4) with no infinite-scroll, so the old
    scroll-to-load-more loop (and the hard-coded '>=6' first-batch wait) never
    accumulated past the first batch. The fix re-SUBMITS the prompt per batch.

    Assert min_success=8 now accumulates BEYOND one v2 batch (>=5 proves a
    second batch was generated), and that it completes in bounded time rather
    than hanging. Data flows: batch1(4) -> re-submit -> batch2 -> >=8.
    """
    import time

    t0 = time.perf_counter()
    res = await client.create_image(
        {
            "prompt": "a friendly cartoon superhero mascot, full body, plain white background",
            "quality": "v2",
            "aspect_ratio": "9:16",
            "min_success": 8,
            "max_scroll": 4,
            "timeout": 240,
        }
    )
    elapsed = time.perf_counter() - t0
    assert isinstance(res, ImageGenerationResult)
    done = [i for i in res.images if not i.get("moderated")]
    # A single v2 batch is ~4; >=5 proves the re-submit accumulation fired.
    assert len(done) >= 5, (
        f"v2 accumulation broken: got {len(done)} non-moderated (expected >=5 via "
        f"re-submit; a single batch is ~4). Total jobs={len(res.images)}."
    )
    # Guard against the old full-timeout hang: two v2 batches render well under
    # 4 * timeout. (Sanity only — generation speed varies.)
    assert elapsed < 240 * 3, f"took {elapsed:.0f}s — unexpectedly close to a timeout hang"


# ---------------------------------------------------------------------------
# Scenario: chrome_no_orphans (Chrome-lifecycle fix — sequential sessions)
# ---------------------------------------------------------------------------
def _count_profile_chrome() -> int:
    """Count chrome.exe whose command line references the connector profile dir."""
    import platform
    import subprocess

    prof_dir = str(Path.home() / ".grok-web-connector" / "profiles" / "grok-chrome")
    if platform.system() != "Windows":
        out = subprocess.run(["pgrep", "-f", prof_dir], capture_output=True, text=True)
        return len([x for x in out.stdout.splitlines() if x.strip().isdigit()])
    esc = prof_dir.replace("'", "''")
    ps = (
        "$d='" + esc + "'.ToLower(); @(Get-CimInstance Win32_Process -Filter "
        "\"name='chrome.exe'\" | Where-Object { $_.CommandLine -and "
        "$_.CommandLine.ToLower().Contains($d) }).Count"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=15
    )
    s = (out.stdout or "").strip()
    return int(s) if s.isdigit() else 0


@pytest.mark.integration
async def test_chrome_no_orphans():
    """Two sequential get_client() sessions: the first closes its Chrome on
    exit (no orphan), and the second launches successfully — even if a
    leftover Chrome is holding the profile lock (auto-reaped on launch).
    Regression for the orphan-accumulation → profile-lock launch failure.
    """
    reap_orphan_chrome()  # clean slate
    await asyncio.sleep(1)

    async with get_client() as c:  # default close_chrome=True
        assert await c.list_posts(limit=1, source="favorites") is not None
    await asyncio.sleep(2)
    assert _count_profile_chrome() == 0, "default session must not orphan Chrome"

    # A leftover Chrome (kept alive) must not block the next launch.
    async with get_client(close_chrome=False) as c:
        assert await c.list_posts(limit=1, source="favorites") is not None
    await asyncio.sleep(2)
    assert _count_profile_chrome() >= 1, "close_chrome=False should keep Chrome alive"

    async with get_client() as c:  # must auto-reap the orphan, not fail
        posts = await c.list_posts(limit=1, source="favorites")
    assert posts is not None, "session after an orphan must launch (auto-reap)"
    await asyncio.sleep(2)
    assert _count_profile_chrome() == 0, "orphan should be reaped + this session closed"
