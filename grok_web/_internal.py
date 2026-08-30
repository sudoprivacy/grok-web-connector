"""
Internal implementation for Grok Web Connector.

Contains:
- ResponseParser: Pure data transformation (API JSON → Python objects)
- Utility functions: Response parsers
- Endpoint constants

Do not import from this module directly. Use the public API from grok_web instead.
"""

import re
from datetime import datetime
from typing import Any

from .exceptions import GrokAPIError, GrokModerationError, GrokRateLimitError
from .models import (
    MODE_IMG2VID,
    MODE_TXT2VID,
    MODE_UNKNOWN,
    MODE_UPLOAD2VID,
    ChildPost,
    PostDetails,
    PostSummary,
    VideoGenerationResult,
    VideoMatchResult,
)

# =============================================================================
# Mod-layer classifier (create_image attempts_trace)
# =============================================================================

# Pixel gen normally takes ≥1.5s. A moderated attempt with image_url that
# came back faster than this is suspicious — probably a cache hit or CDN
# edge case, not a real "server ran vision-classifier on our pixels" event.
_MOD_LAYER_VISION_MIN_LATENCY_MS = 1500


def classify_mod_layer(
    *,
    is_moderated: bool,
    has_image_url: bool,
    latency_ms: int | None,
) -> str | None:
    """Classify a moderated attempt's mod layer for :attr:`attempts_trace`.

    Returns one of ``"prompt_intent"`` / ``"vision_output"`` / ``"uncertain"``
    for moderated attempts, or ``None`` when the attempt is not moderated
    (nothing to classify).

    Rules (v0.19.32; frame count is intentionally not consulted — see the
    heuristic docstring in the ImageGenerationResult.attempts_trace field):

    * not moderated                         → None
    * moderated + no image_url              → prompt_intent
    * moderated + image_url + latency <1.5s → uncertain
    * moderated + image_url + latency ≥1.5s → vision_output
    * moderated + image_url + latency None  → vision_output
      (URL was templated, so pixel-gen ran to completion; safe default)

    The heuristic is pure so it's cheap to unit-test and consistent
    between the create_image call site and any downstream consumer that
    wants to reclassify raw traces.
    """
    if not is_moderated:
        return None
    if not has_image_url:
        return "prompt_intent"
    # has image_url — pixel gen ran to `current_status: completed`.
    if latency_ms is not None and latency_ms < _MOD_LAYER_VISION_MIN_LATENCY_MS:
        return "uncertain"
    return "vision_output"


# =============================================================================
# API Endpoint Constants
# =============================================================================

MEDIA_POST_LIKE_ENDPOINT = "/rest/media/post/like"
MEDIA_POST_UNLIKE_ENDPOINT = "/rest/media/post/unlike"
MEDIA_POST_LIST_ENDPOINT = "/rest/media/post/list"
MEDIA_POST_GET_ENDPOINT = "/rest/media/post/get"
# 2026-04: clicking 生成视频 on a gallery image POSTs here to persist
# the temporary image as a real post under the user's account. Body
# is {"id": "<image_id>"}.
MEDIA_POST_CREATE_ENDPOINT = "/rest/media/post/create"

# 2026-08: "References" (Grok Imagine Video 1.5) — reusable saved subjects
# (character / outfit / product / scene) for cross-generation consistency.
# create body: {"kind": "MEDIA_REFERENCE_KIND_<CATEGORY>", "name": "<title>",
#               "assetIds": ["<grok-image-id>", ...]}  (a Grok image_id IS a
# valid assetId — in-Grok, no upload). Returns {"reference": {...}}.
# list body: {"limit": <n>} -> {"references": [...]}. delete body: {"id": ...}.
MEDIA_REFERENCE_CREATE_ENDPOINT = "/rest/media/reference/create"
MEDIA_REFERENCE_LIST_ENDPOINT = "/rest/media/reference/list"
MEDIA_REFERENCE_DELETE_ENDPOINT = "/rest/media/reference/delete"

# 2026-08: image segmentation — the "分段"/Segments panel. Auto-detects
# labeled objects (with bounding boxes + masks) in a Grok image.
# body: {"assetId": "<image_id>", "cachedOnly": false, "maskFormat": "rle"}
# -> {"cached": bool, "map": {"objects": [{"name","boxXyxy":[x1,y1,x2,y2],
#     "score","maskUrl","maskRle":{"size":[h,w],"counts":"<rle>"}}, ...]}}
MEDIA_SEGMENT_ENDPOINT = "/rest/media/segment"

# 2026 canvas/conversation model — the user's OWN generations live here, NOT in
# /rest/media/post/list (that endpoint holds only LIKED posts, ~700 + residual;
# a user's own 2026 Imagine creations are invisible to it). Discovered live
# 2026-08 (consumer CDP capture + our recon): the Imagine UI enumerates via
#   GET  /rest/app-chat/conversations                     -> {"conversations":[{conversationId,mediaTypes,latestAssetMetadata,kind,...}]}
#   GET  /rest/app-chat/conversations/{id}/responses      -> {"responses":[{responseId,generatedImageUrls,fileUris,imageEditUris,...}]}
# and a parallel canvas surface: /rest/media/canvas/{list,get}. The actual media
# URLs live in the per-response payload; see extract_generation_media().
APP_CHAT_CONVERSATIONS_ENDPOINT = "/rest/app-chat/conversations"
MEDIA_CANVAS_LIST_ENDPOINT = "/rest/media/canvas/list"
MEDIA_CANVAS_GET_ENDPOINT = "/rest/media/canvas/get"

# Media-URL classification for generation enumeration. We deep-scan response
# payloads for Grok-hosted media URLs and classify by extension rather than by
# field name — the field a video URL lands in varies (generatedImageUrls /
# fileUris / imageEditUris / latestAssetMetadata), and new fields appear as the
# UI churns, but the CDN host + extension are stable.
_GEN_MEDIA_EXT = {
    "video": (".mp4", ".webm", ".mov", ".m4v"),
    "image": (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"),
    "audio": (".mp3", ".wav", ".m4a", ".ogg", ".opus"),
}
# Restrict to Grok/xAI asset hosts so we never mistake a web-search result URL
# (news sites, etc. — those also appear in chat responses) for the user's media.
_GEN_MEDIA_HOSTS = ("grok.com", "x.ai")
_GEN_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _asset_url(uri: str | None) -> str | None:
    """Turn a Grok asset reference into a full CDN URL.

    Response payloads carry RELATIVE paths ('users/<uid>/generated/<id>/image.jpg')
    — the same convention ImageGenerationResult.image_urls templates against
    https://assets.grok.com/. Full URLs pass through unchanged.
    """
    if not uri:
        return None
    return uri if uri.startswith("http") else "https://assets.grok.com/" + uri.lstrip("/")


def _mime_to_type(mime: str | None) -> str | None:
    """image/jpeg -> 'image'; video/mp4 -> 'video'; audio/* -> 'audio'; else None."""
    if not mime:
        return None
    top = mime.split("/", 1)[0].lower()
    return top if top in ("image", "video", "audio") else None


def classify_media_url(url: str) -> tuple[str | None, str | None]:
    """Return (media_type, asset_id) for a Grok media URL/path, else (None, None).

    media_type by extension; asset_id is the ``generated/<id>/`` segment when
    present (the fileMetadataId = the download-filename id), else the filename
    stem. Accepts full URLs and relative 'users/.../generated/...' paths.
    """
    base = url.split("?")[0]
    low = base.lower()
    # For a full URL, keep it Grok-hosted; relative paths (no scheme) are ours.
    if "://" in low:
        try:
            host = low.split("/", 3)[2]
        except IndexError:
            return None, None
        if not any(host.endswith(h) or ("." + h) in host for h in _GEN_MEDIA_HOSTS):
            return None, None
    mtype = None
    for cand, exts in _GEN_MEDIA_EXT.items():
        if low.endswith(exts):
            mtype = cand
            break
    if not mtype:
        return None, None
    parts = base.split("/")
    asset_id = None
    if "generated" in parts:
        i = parts.index("generated")
        if i + 1 < len(parts):
            asset_id = parts[i + 1]
    if not asset_id:
        asset_id = parts[-1].rsplit(".", 1)[0] if parts else None
    return mtype, (asset_id or None)


def extract_generation_media(responses_payload, conversation_id):
    """Extract the user's generated media from a /responses payload.

    Field-aware (the media a response carries is structured, not just loose
    URLs): primary source is ``fileAttachmentsMetadata`` (each entry has
    ``fileMetadataId`` = the asset/download id, ``fileMimeType`` = reliable
    type, ``fileUri`` = relative CDN path); ``generatedImageUrls`` (relative
    paths, may include videos) is folded in and classified by extension.
    Dedup by asset_id. Returns list of dicts:
    {url, media_type, asset_id, response_id, conversation_id, created_at}.
    Pure/deterministic — unit-tested against captured payload samples.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def _add(asset_id, media_type, url, rid, created):
        if not asset_id or not media_type or asset_id in seen:
            return
        seen.add(asset_id)
        out.append(
            {
                "url": url,
                "media_type": media_type,
                "asset_id": asset_id,
                "response_id": rid,
                "conversation_id": conversation_id,
                "created_at": created,
            }
        )

    responses = (
        responses_payload.get("responses", []) if isinstance(responses_payload, dict) else []
    )
    for resp in responses:
        if not isinstance(resp, dict):
            continue
        rid = resp.get("responseId")
        created = resp.get("createTime")

        # Primary: fileAttachmentsMetadata carries mime + id + relative uri.
        for meta in resp.get("fileAttachmentsMetadata") or []:
            if not isinstance(meta, dict):
                continue
            mtype = _mime_to_type(meta.get("fileMimeType"))
            if not mtype:
                # fall back to extension of the file name / uri
                mtype, _ = classify_media_url(meta.get("fileName") or meta.get("fileUri") or "")
            _add(meta.get("fileMetadataId"), mtype, _asset_url(meta.get("fileUri")), rid, created)

        # Secondary: generatedImageUrls (relative paths; can include videos).
        for path in resp.get("generatedImageUrls") or []:
            if not isinstance(path, str):
                continue
            mtype, asset_id = classify_media_url(path)
            _add(asset_id, mtype, _asset_url(path), rid, created)

    return out


def normalize_asset_id(value: str) -> str:
    """Reduce a download filename / id to its bare asset id for matching.

    'grok-video-<uuid>.mp4' / 'grok-image-<uuid>.jpg' / '<uuid>.mp4' / a bare
    '<uuid>' all normalize to '<uuid>'. Falls back to the extension-stripped
    basename when no UUID is present.
    """
    s = str(value or "").strip()
    # A UUID anywhere (download filename, full URL path, or bare id) IS the id.
    m = _GEN_UUID_RE.search(s)
    if m:
        return m.group(0).lower()
    # No UUID: fall back to the extension-stripped basename.
    v = s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].split("?")[0]
    if "." in v:
        v = v.rsplit(".", 1)[0]
    return v.lower()


# =============================================================================
# Shared Utilities
# =============================================================================


def parse_video_ndjson_response(
    response_text: str,
    parent_post_id: str,
    statsig_id: str,
) -> VideoGenerationResult:
    """Parse NDJSON response from video generation API."""
    import json

    conversation_id = None
    video_result = None
    chat_message: str | None = None  # set if Grok answered in chat mode (no video)

    for line in response_text.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)

            if "error" in data:
                error = data["error"]
                error_code = error.get("code")
                error_message = error.get("message", "Unknown error")

                if error_code == 8 or "too many requests" in error_message.lower():
                    raise GrokRateLimitError(
                        f"Rate limit exceeded: {error_message}. "
                        "This is a GLOBAL limit - stop all requests and wait. "
                        "Rate limits reset every hour (as of December 2025)."
                    )

            result = data.get("result", {})

            if "conversation" in result:
                conversation_id = result["conversation"].get("conversationId")

            # 2026-07: the /conversations/{id}/responses endpoint (img2vid
            # from an existing post, video-extend, and other append-to-
            # conversation flows) HOISTS the payload onto
            # result.streamingVideoGenerationResponse. The classic
            # /conversations/new path (txt2vid, upload2vid) nests it under
            # result.response.streamingVideoGenerationResponse. Handle both,
            # mirroring the edit_image _ingest_edit_image_line dual-nesting.
            response = result.get("response", {})
            if isinstance(response, dict) and "streamingVideoGenerationResponse" in response:
                video_result = response["streamingVideoGenerationResponse"]
            elif "streamingVideoGenerationResponse" in result:
                video_result = result["streamingVideoGenerationResponse"]

            # 2026-08: when Grok REJECTS the request pre-flight (content
            # moderation, or an img2vid source frame that's unavailable/removed
            # — the reporter's orphan-image case), the video pipeline never
            # engages and it replies in CHAT mode: result.userResponse with a
            # plain message (often the bare "--mode=..." tag echoed back) and
            # NO streamingVideoGenerationResponse. Capture it so we can raise a
            # typed, actionable error instead of a confusing "parse failed".
            user_response = result.get("userResponse")
            if isinstance(user_response, dict) and user_response.get("message") is not None:
                chat_message = user_response.get("message")
        except json.JSONDecodeError:
            continue

    if not video_result:
        if chat_message is not None:
            # NOT a parser bug — Grok declined to generate and answered in chat.
            raise GrokModerationError(
                "Grok answered the video request in CHAT mode instead of "
                "generating (no streamingVideoGenerationResponse). The request "
                "was rejected pre-flight — either content moderation, or the "
                "img2vid source frame is unavailable/removed (orphan/expired "
                "post). This is NOT a parser bug; retrying the same source "
                f"won't help. Grok's chat reply began: {chat_message[:200]!r}",
                chat_message=chat_message,
            )
        preview = response_text[:500] if response_text else "(empty)"
        raise GrokAPIError(
            "Failed to parse video generation response. "
            f"No streamingVideoGenerationResponse found. Response preview: {preview}"
        )

    # parent_post_id fallback: Grok's NDJSON parentPostId → caller's input.
    # source_post_id: always the caller's input (the post id they passed
    # to create_video as 'post:<uuid>'). The two differ when Grok roots
    # the video under a grandparent in its chain — see models.py docs.
    return VideoGenerationResult(
        video_id=video_result.get("videoId", ""),
        source_post_id=parent_post_id,
        parent_post_id=video_result.get("parentPostId", parent_post_id),
        moderated=video_result.get("moderated", False),
        progress=video_result.get("progress", 0),
        mode=video_result.get("mode", "normal"),
        model_name=video_result.get("modelName"),
        image_reference=video_result.get("imageReference"),
        conversation_id=conversation_id,
        statsig_id=statsig_id,
    )


# =============================================================================
# Response Parser - Pure data transformation + helper methods
# =============================================================================


class ResponseParser:
    """Parses Grok API responses into Python objects.

    Also provides pure helper methods for video matching logic
    (no I/O, just data manipulation).
    """

    ASSETS_URL = "https://assets.grok.com"

    # =========================================================================
    # Parsing methods
    # =========================================================================

    def _detect_generation_mode(self, post_data: dict) -> str:
        """Detect generation mode from post metadata.

        Returns plain string: 'txt2img', 'img2vid', 'txt2vid', 'upload2vid', or 'unknown'.
        """
        media_type = post_data.get("mediaType", "")
        prompt = post_data.get("prompt")
        mode = post_data.get("mode")

        if media_type == "MEDIA_POST_TYPE_VIDEO":
            if mode == "text":
                return MODE_TXT2VID
            return MODE_UNKNOWN

        if media_type == "MEDIA_POST_TYPE_IMAGE":
            if prompt:
                return MODE_IMG2VID
            else:
                return MODE_UPLOAD2VID

        return MODE_UNKNOWN

    def _parse_timestamp(self, value: Any) -> datetime | None:
        """Parse ISO timestamp string to datetime."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            if isinstance(value, str):
                value = value.replace("Z", "+00:00")
                return datetime.fromisoformat(value)
        except Exception:
            pass
        return None

    def _parse_post_summary(self, data: dict, include_raw_data: bool = False) -> PostSummary:
        """Parse API response into PostSummary."""
        mode = self._detect_generation_mode(data)

        prompt = data.get("prompt") or data.get("originalPrompt") or ""
        prompt_preview = prompt[:100] if prompt else None

        child_posts = data.get("childPosts", [])
        video_count = sum(1 for c in child_posts if c.get("mediaType") == "MEDIA_POST_TYPE_VIDEO")

        return PostSummary(
            id=data.get("id", ""),
            mode=mode,
            prompt_preview=prompt_preview,
            video_count=video_count,
            created_at=self._parse_timestamp(data.get("createTime")),
            media_type=data.get("mediaType"),
            raw_data=data if include_raw_data else None,
        )

    def _parse_post_details(
        self,
        data: dict,
        post_id: str,
        raw_data: dict | None = None,
    ) -> PostDetails:
        """Parse API response into PostDetails with all children.

        Grok's /rest/media/post/get response represents the full edit
        tree via three arrays:

        - ``images[]``  — every image in the edit tree, INCLUDING this
                          post itself if it is an image.
        - ``videos[]``  — every video in the edit tree, INCLUDING this
                          post itself if it is a video.
        - ``childPosts[]`` — legacy field, strict children (does not
                             include self). Can be incomplete under the
                             post 2026 UI (observed missing both
                             edit-image and video entries that DO appear
                             in ``images[]`` / ``videos[]``).

        Parent/child lineage is expressed by each entry's
        ``originalPostId`` — the image or video it was generated from.
        A video whose ``originalPostId`` points at an entry in
        ``images[]`` was made from that image.

        Strategy: union ``images[]`` + ``videos[]`` (the authoritative
        arrays), de-duplicate by id, drop self-references. Fall back to
        ``childPosts[]`` only for entries that the new arrays are
        missing — keeps us robust if Grok rolls back.
        """
        mode = self._detect_generation_mode(data)

        def _to_child(entry: dict) -> ChildPost | None:
            media_type = entry.get("mediaType")
            if media_type not in ("MEDIA_POST_TYPE_VIDEO", "MEDIA_POST_TYPE_IMAGE"):
                return None
            return ChildPost(
                id=entry.get("id", ""),
                media_type=media_type,
                original_post_id=entry.get("originalPostId") or post_id,
                original_prompt=entry.get("originalPrompt"),
                prompt=entry.get("prompt"),
                media_url=entry.get("mediaUrl"),
                hd_media_url=entry.get("hdMediaUrl"),
                thumbnail_url=entry.get("thumbnailImageUrl"),
                created_at=self._parse_timestamp(entry.get("createTime")),
                resolution=entry.get("resolution"),
                # Video post entries carry segment length as ``videoDuration``
                # (seconds, integer). The legacy ``duration`` name used by the
                # image API never appears on videos, so reading it always
                # returned None. Prefer videoDuration; fall back to legacy
                # in case Grok rolls back.
                duration=entry.get("videoDuration") or entry.get("duration"),
                model_name=entry.get("modelName"),
                mode=entry.get("mode"),
            )

        children: list[ChildPost] = []
        seen_ids: set[str] = {post_id}  # exclude self to keep ChildPost semantics
        # Prefer the new top-level arrays first.
        for bucket_key in ("images", "videos"):
            for entry in data.get(bucket_key) or []:
                eid = entry.get("id")
                if not eid or eid in seen_ids:
                    continue
                child = _to_child(entry)
                if child is None:
                    continue
                seen_ids.add(eid)
                children.append(child)
        # Fall back to childPosts[] for anything still missing (legacy +
        # defensive). Skip entries we already captured.
        for entry in data.get("childPosts") or []:
            eid = entry.get("id")
            if not eid or eid in seen_ids:
                continue
            child = _to_child(entry)
            if child is None:
                continue
            seen_ids.add(eid)
            children.append(child)

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
            original_post_id=data.get("originalPostId"),
            raw_data=raw_data,
        )

    # =========================================================================
    # Pure helper methods for video matching (no I/O)
    # =========================================================================

    def _validate_asset_url(self, asset_url: str) -> None:
        """Validate asset URL format."""
        if not asset_url:
            raise GrokAPIError("Asset URL is empty")

        if not (
            asset_url.startswith(self.ASSETS_URL)
            or asset_url.startswith("https://imagine-public.x.ai/")
        ):
            raise GrokAPIError(
                f"Invalid asset URL. Expected {self.ASSETS_URL}/... or "
                f"https://imagine-public.x.ai/..., got: {asset_url[:50]}..."
            )

    def _extract_parent_info(self, details: PostDetails, video_id: str) -> tuple[str, bool]:
        """Extract parent ID and parent status from PostDetails."""
        raw_post = details.raw_data.get("post", details.raw_data) if details.raw_data else {}
        original_post_id = raw_post.get("originalPostId")

        if original_post_id and original_post_id != video_id:
            return original_post_id, False
        else:
            return video_id, True

    def _verify_file_size_match(
        self, video_id: str, filename: str, local_size: int, web_size: int
    ) -> None:
        """Verify local and web file sizes match."""
        if web_size != local_size:
            raise GrokAPIError(
                f"File size mismatch for video: {video_id}\n"
                f"Local file: {filename}\n"
                f"Local size: {local_size}, Web size: {web_size}"
            )

    def _build_video_match_result(
        self,
        parent_id: str,
        video_id: str,
        is_parent_video: bool,
        details: PostDetails,
        local_size: int,
    ) -> VideoMatchResult:
        """Build VideoMatchResult from components."""
        new_filename = f"grok-video_{parent_id}_{video_id}.mp4"
        return VideoMatchResult(
            parent_id=parent_id,
            video_id=video_id,
            is_parent_video=is_parent_video,
            mode=details.mode,
            original_prompt=details.original_prompt,
            file_size=local_size,
            new_filename=new_filename,
        )

    def _extract_media_url(self, details: PostDetails, video_id: str, filename: str) -> str:
        """Extract media URL from PostDetails."""
        url = details.hd_media_url or details.media_url
        if not url:
            raise GrokAPIError(f"No media URL found for video: {video_id}\nLocal file: {filename}")
        return url

    @staticmethod
    def _parse_video_filename(filename: str) -> tuple[str | None, str | None]:
        """Parse video filename to extract UUIDs.

        Returns:
            (format_type, uuid) where format_type is 'old' or 'web', or (None, None).
        """
        # Try old format: grok-video-{parent_uuid}.mp4
        old_match = re.match(
            r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            filename,
        )
        if old_match:
            return "old", old_match.group(1)

        # Try web format: {video_uuid}.mp4 or {video_uuid}_hd.mp4
        web_match = re.match(
            r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
            r"(?:_hd)?(?:\s*\(\d+\))?\.mp4$",
            filename,
        )
        if web_match:
            return "web", web_match.group(1)

        return None, None
