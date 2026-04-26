"""Data models for Grok Web Connector."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

# =============================================================================
# Generation mode constants
# =============================================================================

MODE_TXT2IMG = "txt2img"
MODE_IMG2VID = "img2vid"
MODE_TXT2VID = "txt2vid"
MODE_UPLOAD2VID = "upload2vid"
MODE_UNKNOWN = "unknown"


class ChildPost(BaseModel):
    """A child post (image or video) in a post's childPosts array.

    In Grok, everything is a post. A root image post can have child posts
    that are either edited image variants or generated videos.
    ``original_post_id`` points to the immediate parent (the post this was
    generated from), which may be the root or an edited image.
    """

    id: str = Field(..., description="Child post UUID")
    media_type: str = Field(..., description="MEDIA_POST_TYPE_IMAGE or MEDIA_POST_TYPE_VIDEO")
    original_post_id: str = Field(
        ..., description="Post this was generated from (immediate parent)"
    )

    # Prompts
    original_prompt: str | None = Field(None, description="Generation/edit prompt")
    prompt: str | None = Field(None, description="Image generation prompt")

    # URLs
    media_url: str | None = Field(None, description="Media URL (image or video)")
    hd_media_url: str | None = Field(None, description="HD media URL")
    thumbnail_url: str | None = Field(None, description="Thumbnail image URL")

    # Metadata
    created_at: datetime | None = Field(None, description="Creation timestamp (UTC)")
    resolution: dict[str, int] | None = Field(None, description="Media resolution {width, height}")
    duration: int | None = Field(
        None,
        description=(
            "Duration in seconds (videos only). Reflects the segment length "
            "from ``videoDuration`` on the post — for videos produced by "
            "extend_video this is the newly-added tail segment, not cumulative "
            "chain length. To get cumulative, use VideoExtendResult."
            "cumulative_duration_s on the extend result directly."
        ),
    )
    model_name: str | None = Field(None, description="Model used (e.g., imagine_h_1)")
    mode: str | None = Field(None, description="Generation mode: 'custom', 'normal', etc.")

    @computed_field
    @property
    def is_image(self) -> bool:
        """True if this is an image post."""
        return self.media_type == "MEDIA_POST_TYPE_IMAGE"

    @computed_field
    @property
    def is_video(self) -> bool:
        """True if this is a video post."""
        return self.media_type == "MEDIA_POST_TYPE_VIDEO"

    @computed_field
    @property
    def web_url(self) -> str:
        """Direct web URL to this post."""
        return f"https://grok.com/imagine/post/{self.id}"

    @computed_field
    @property
    def parent_web_url(self) -> str:
        """Web URL to the parent post."""
        return f"https://grok.com/imagine/post/{self.original_post_id}"

    @computed_field
    @property
    def best_media_url(self) -> str | None:
        """Best available media URL (HD preferred)."""
        return self.hd_media_url or self.media_url


# Backward compat
ChildVideo = ChildPost


class PostSummary(BaseModel):
    """Summary of a post for list_posts() response."""

    id: str = Field(..., description="Post UUID")
    mode: str = Field(
        ..., description="Generation mode (txt2img, img2vid, txt2vid, upload2vid, unknown)"
    )

    # Preview info
    prompt_preview: str | None = Field(None, description="First 100 chars of prompt")
    video_count: int = Field(0, description="Number of child videos")

    # Timestamps
    created_at: datetime | None = Field(None, description="Creation timestamp (UTC)")

    # Media type
    media_type: str | None = Field(
        None, description="MEDIA_POST_TYPE_IMAGE or MEDIA_POST_TYPE_VIDEO"
    )

    # Raw data for debugging
    raw_data: dict[str, Any] | None = Field(None, description="Raw API response for this post")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for this post."""
        return f"https://grok.com/imagine/post/{self.id}"


class PostDetails(BaseModel):
    """Full details of a post for get_post_details() response."""

    id: str = Field(..., description="Post UUID")
    user_id: str | None = Field(None, description="Owner's user UUID")
    mode: str = Field(
        ..., description="Detected generation mode (txt2img, img2vid, txt2vid, upload2vid, unknown)"
    )

    # Parent post info
    media_type: str | None = Field(
        None, description="MEDIA_POST_TYPE_IMAGE or MEDIA_POST_TYPE_VIDEO"
    )
    prompt: str | None = Field(None, description="Image generation prompt (for img2vid mode)")
    original_prompt: str | None = Field(None, description="Video prompt (for txt2vid mode)")

    # URLs
    media_url: str | None = Field(None, description="Parent media URL (image or video)")
    hd_media_url: str | None = Field(None, description="HD media URL")
    thumbnail_url: str | None = Field(None, description="Thumbnail URL")

    # Metadata
    created_at: datetime | None = Field(None, description="Creation timestamp (UTC)")
    resolution: dict[str, int] | None = Field(None, description="Media resolution")
    model_name: str | None = Field(None, description="Model used (e.g., imagine_x_1)")

    # Child posts (images and videos, flat list from API)
    children: list[ChildPost] = Field(
        default_factory=list, description="All child posts (images and videos)"
    )

    # Original post ID (None for root posts)
    original_post_id: str | None = Field(None, description="Parent post ID (None if root)")

    # Raw data for debugging
    raw_data: dict[str, Any] | None = Field(None, description="Raw API response")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for this post."""
        return f"https://grok.com/imagine/post/{self.id}"

    @computed_field
    @property
    def is_root(self) -> bool:
        """True if this is a root post (no parent)."""
        return self.original_post_id is None

    @computed_field
    @property
    def video_count(self) -> int:
        """Number of child video posts."""
        return sum(1 for c in self.children if c.is_video)

    @computed_field
    @property
    def image_count(self) -> int:
        """Number of child image posts (edited variants)."""
        return sum(1 for c in self.children if c.is_image)

    @property
    def has_children(self) -> bool:
        """Check if this post has any child posts."""
        return len(self.children) > 0

    @property
    def image_children(self) -> list["ChildPost"]:
        """Child image posts (edited variants only, not the root image)."""
        return [c for c in self.children if c.is_image]

    @property
    def video_children(self) -> list["ChildPost"]:
        """Child video posts."""
        return [c for c in self.children if c.is_video]

    @property
    def all_images(self) -> list["ChildPost"]:
        """All images in this post's edit tree, root first.

        Returns ``[root_as_ChildPost, *image_children]``. In the new
        (post-2026) REST schema, ``images[]`` from the server already
        contains both root and edits; ``_parse_post_details`` unions
        it with ``childPosts`` to populate ``children``. We reconstruct
        ``root_as_child`` from the parent ``PostDetails`` fields so
        this method returns a consistent list regardless of whether
        the root was represented as a bare field or a ``ChildPost``.
        """
        root_as_child = ChildPost(
            id=self.id,
            media_type=self.media_type or "MEDIA_POST_TYPE_IMAGE",
            original_post_id=self.original_post_id or self.id,
            original_prompt=self.original_prompt,
            prompt=self.prompt,
            media_url=self.media_url,
            hd_media_url=self.hd_media_url,
            thumbnail_url=self.thumbnail_url,
            created_at=self.created_at,
            resolution=self.resolution,
            model_name=self.model_name,
        )
        return [root_as_child] + self.image_children

    def videos_by_parent_image(self) -> dict[str, list["ChildPost"]]:
        """Group child videos by which image they were generated from.

        Returns:
            Dict mapping parent image post_id → list of video ChildPosts.
            e.g., {"root-id": [vid1, vid2], "edited-img-id": [vid3]}
        """
        groups: dict[str, list[ChildPost]] = {}
        for child in self.children:
            if child.is_video:
                groups.setdefault(child.original_post_id, []).append(child)
        return groups

    def find_parent_image(self, video_id: str) -> str | None:
        """Find which image a video was generated from.

        Args:
            video_id: The child video post UUID

        Returns:
            The parent image post_id, or None if video not found
        """
        for child in self.children:
            if child.id == video_id:
                return child.original_post_id
        return None

    # =========================================================================
    # Unified navigation (works from ANY id in the edit tree)
    # =========================================================================
    #
    # Grok's edit tree is a DAG of posts linked by ``original_post_id``.
    # Nodes can be images (MEDIA_POST_TYPE_IMAGE) or videos
    # (MEDIA_POST_TYPE_VIDEO). Both can have children:
    #   * image -> image   (edit)
    #   * image -> video   (img2vid)
    #   * video -> video   (video-extend)
    # The helpers below treat all of these uniformly — give them any id
    # in the tree and they navigate by ``original_post_id`` links,
    # without caring whether each link is an edit, a generation, or an
    # extension.
    # =========================================================================

    def find(self, post_id: str) -> "ChildPost | None":
        """Look up any post (image or video) in this tree by id.

        Returns the root itself as a synthetic ``ChildPost`` if
        ``post_id == self.id``, otherwise searches ``children``.
        Returns ``None`` if not found.
        """
        if post_id == self.id:
            return ChildPost(
                id=self.id,
                media_type=self.media_type or "MEDIA_POST_TYPE_IMAGE",
                original_post_id=self.original_post_id or self.id,
                original_prompt=self.original_prompt,
                prompt=self.prompt,
                media_url=self.media_url,
                hd_media_url=self.hd_media_url,
                thumbnail_url=self.thumbnail_url,
                created_at=self.created_at,
                resolution=self.resolution,
                model_name=self.model_name,
            )
        for c in self.children:
            if c.id == post_id:
                return c
        return None

    def parent_of(self, post_id: str) -> "ChildPost | None":
        """Return the ChildPost that ``post_id`` was generated from.

        Works for any node in the tree — image edits, videos, or the
        root (returns None for the root since it has no parent WITHIN
        this post; see ``PostDetails.original_post_id`` for the parent
        across posts).
        """
        node = self.find(post_id)
        if node is None:
            return None
        if node.original_post_id == node.id:
            return None  # root has no self-parent
        return self.find(node.original_post_id)

    def children_of(self, post_id: str) -> list["ChildPost"]:
        """Return everything (images + videos) directly generated from
        ``post_id``.

        Covers the two common questions:
          * "What edits were made on top of this image?" → filter result
            by ``is_image``
          * "What videos were generated from this image?" → filter by
            ``is_video`` (equivalent to
            ``videos_by_parent_image()[post_id]``)
        """
        return [c for c in self.children if c.original_post_id == post_id]

    def siblings_of(self, post_id: str) -> list["ChildPost"]:
        """Return other posts that share the same immediate parent as
        ``post_id`` (i.e. other edits/videos from the same source).
        """
        node = self.find(post_id)
        if node is None:
            return []
        return [
            c
            for c in self.children
            if c.original_post_id == node.original_post_id and c.id != post_id
        ]

    def ancestors_of(self, post_id: str) -> list["ChildPost"]:
        """Walk up the edit chain from ``post_id`` to the root.

        Returns the ancestors in order (immediate parent first, root
        last). Empty if ``post_id`` is the root or not found. Guards
        against cycles: stops if a repeat id is seen.
        """
        chain: list[ChildPost] = []
        seen: set[str] = set()
        cur = self.parent_of(post_id)
        while cur is not None and cur.id not in seen:
            chain.append(cur)
            seen.add(cur.id)
            cur = self.parent_of(cur.id)
        return chain

    def is_extension(self, post_id: str) -> bool:
        """True if ``post_id`` is a video-extend — i.e. a video whose
        parent is another video (rather than an image).

        False for images, for videos generated directly from images
        (regular img2vid / upload2vid), and for posts not in this tree.
        """
        node = self.find(post_id)
        if node is None or not node.is_video:
            return False
        parent = self.parent_of(post_id)
        return parent is not None and parent.is_video

    def extensions_of(self, post_id: str) -> list["ChildPost"]:
        """Return videos generated as extensions of ``post_id``.

        Equivalent to ``[c for c in children_of(post_id) if c.is_video]``
        when ``post_id`` itself is a video. For an image post this
        returns [] (images can't be directly extended — you'd extend a
        video derived from the image).
        """
        node = self.find(post_id)
        if node is None or not node.is_video:
            return []
        return [c for c in self.children_of(post_id) if c.is_video]

    def extension_chain(self, post_id: str) -> list["ChildPost"]:
        """Walk the video-extend chain starting at ``post_id``.

        Returns ``[post_id_as_ChildPost, first_extension, second_extension, ...]``
        following the earliest video-child at each step. Empty list if
        ``post_id`` is not a video or not in this tree. Useful for
        reconstructing a linear extend sequence
        (v0 -> v0_extended -> v0_extended_again -> ...).
        """
        start = self.find(post_id)
        if start is None or not start.is_video:
            return []
        chain: list[ChildPost] = [start]
        seen: set[str] = {start.id}
        cur = start
        while True:
            exts = self.extensions_of(cur.id)
            if not exts:
                break
            nxt = exts[0]
            if nxt.id in seen:
                break  # cycle guard
            chain.append(nxt)
            seen.add(nxt.id)
            cur = nxt
        return chain

    def descendants_of(self, post_id: str) -> list["ChildPost"]:
        """Return the full subtree under ``post_id`` in BFS order.

        Includes direct children, their children, etc. Does not
        include ``post_id`` itself.
        """
        out: list[ChildPost] = []
        seen: set[str] = {post_id}
        frontier = self.children_of(post_id)
        while frontier:
            next_frontier: list[ChildPost] = []
            for node in frontier:
                if node.id in seen:
                    continue
                seen.add(node.id)
                out.append(node)
                next_frontier.extend(self.children_of(node.id))
            frontier = next_frontier
        return out


class ImageVideoMapping(BaseModel):
    """Mapping of a source image to its generated videos."""

    post_id: str = Field(..., description="Source image post ID")
    media_url: str | None = Field(None, description="Source image URL")
    videos: list[ChildPost] = Field(
        default_factory=list, description="Videos generated from this image"
    )


class GrokCookies(BaseModel):
    """Authentication cookies for Grok API."""

    sso: str = Field(..., description="SSO JWT token")
    sso_rw: str = Field(..., alias="sso-rw", description="SSO read-write JWT token")
    x_userid: str = Field(..., alias="x-userid", description="User ID")
    cf_clearance: str = Field(..., description="Cloudflare clearance token")

    model_config = ConfigDict(populate_by_name=True)

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for requests library."""
        return {
            "sso": self.sso,
            "sso-rw": self.sso_rw,
            "x-userid": self.x_userid,
            "cf_clearance": self.cf_clearance,
        }


class VideoMatchResult(BaseModel):
    """Result of matching a local video file to its web counterpart."""

    # Identifiers
    parent_id: str = Field(..., description="Parent post UUID")
    video_id: str = Field(..., description="Video UUID (child or parent for txt2vid)")
    is_parent_video: bool = Field(
        False, description="True if this is a txt2vid parent video (not a child)"
    )

    # Metadata
    mode: str = Field(
        ..., description="Generation mode (txt2img, img2vid, txt2vid, upload2vid, unknown)"
    )
    original_prompt: str | None = Field(None, description="Video generation prompt")
    file_size: int = Field(..., description="File size in bytes")

    # Generated filename
    new_filename: str = Field(..., description="New filename following naming convention")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for this video's parent post."""
        return f"https://grok.com/imagine/post/{self.parent_id}"


class VideoGenerationResult(BaseModel):
    """Result of create_video_from_image() API call."""

    # Core identifiers
    video_id: str = Field(..., description="Generated video UUID")
    source_post_id: str | None = Field(
        None,
        description=(
            "The post this video was generated from — i.e. the id you "
            "passed to create_video() as the 'post:<uuid>' source (or "
            "the parent returned in the NDJSON for other paths). This "
            "is also the page the video's web_url points to."
        ),
    )
    parent_post_id: str = Field(
        ...,
        description=(
            "The post id the video is conceptually linked to (same as "
            "source_post_id for most flows). NOTE: this is NOT always "
            "Grok's internal chain-parent — for videos derived from an "
            "edit_image output, Grok actually roots the video under the "
            "edit chain's ORIGINAL source image, not the edited image. "
            "If you need Grok's authoritative chain parent, inspect "
            "get_post_details(video_id).original_post_id. Prefer "
            "download_video(video_id) directly — it no longer needs "
            "you to pass a parent."
        ),
    )

    # Generation status
    moderated: bool = Field(
        False,
        description=(
            "True if content was flagged by moderation. "
            "NOTE: reflects only the IMMEDIATE NDJSON response (prompt/"
            "reference-image moderation). Grok also runs a second "
            "post-render moderation pass whose verdict is NOT in this "
            "field. To catch post-render moderation, pass "
            "verify_final=True to create_video() or call "
            "client.check_video_moderated(video_id) afterwards."
        ),
    )
    progress: int = Field(100, description="Generation progress (100 = complete)")

    # Metadata
    mode: str = Field("normal", description="Generation mode (normal, custom, etc.)")
    model_name: str | None = Field(None, description="Model used (e.g., imagine_xdit_1)")
    image_reference: str | None = Field(None, description="Source image URL")

    # Conversation info (for debugging)
    conversation_id: str | None = Field(None, description="Chat conversation UUID")

    # Uploaded asset references (populated on upload2vid flows). Use these
    # as "file:<id>" entries in a follow-up create_video() call to retry
    # generation without re-uploading the images.
    image_file_ids: list[str] = Field(
        default_factory=list,
        description=(
            "fileMetadataIds of uploaded images (upload2vid only). Pass back "
            "as images=['file:<id>', ...] to retry generation without "
            "re-uploading (e.g., after moderated=True on the output video)."
        ),
    )

    # Style control (for MCTS pipeline)
    statsig_id: str | None = Field(
        None,
        description=(
            "Style seed (x-statsig-id) used for this generation. "
            "IMPORTANT: Same statsig_id produces ~99% similar video styles "
            "(camera motion, character movement, animation timing). "
            "Save this value to reproduce similar styles in future generations. "
            "Format: 94-char Base64 encoding 70 random bytes."
        ),
    )
    duration_s: int | None = Field(
        None,
        description=(
            "Actual output video length in seconds, as reported by Grok's "
            "post metadata after render. This is the authoritative value — "
            "don't rely on the `duration` parameter you passed in, since "
            "Grok occasionally re-segments (e.g. a 6s request can come back "
            "as 8s). None if the post metadata wasn't available at return "
            "time (race condition on the txt2vid pre-wait path) or if the "
            "request was moderated."
        ),
    )
    is_persisted: bool | None = Field(
        None,
        description=(
            "Whether ``video_id`` resolves to a real, fetchable post on "
            "Grok's side. Populated by a post-call ``get_post_details`` "
            "probe (~150ms): ``True`` if the id is fetchable, ``False`` "
            "if it 404s, ``None`` if the probe was skipped or errored.\n\n"
            "Why this matters: in moderated NSFW gens, Grok's NDJSON "
            "frequently streams a ``videoId`` that's just a per-stream "
            "identifier — no real post is persisted under it, so any "
            "downstream ``get_post_details`` / ``download_video`` call "
            "with that id will 404. ``is_persisted=False`` is the "
            "explicit signal: the id is unusable, don't bother trying. "
            "For ``moderated=True + is_persisted=False`` the call is "
            "fully terminal (Grok rejected the gen, no recovery)."
        ),
    )

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for the parent post (video will appear there)."""
        return f"https://grok.com/imagine/post/{self.parent_post_id}"

    @property
    def is_complete(self) -> bool:
        """True iff Grok reported progress=100 before we returned.

        A ``False`` value can mean either *failed* or *still in-flight*
        — discriminate with :attr:`in_progress` / :attr:`moderated`.
        """
        return self.progress == 100

    @property
    def in_progress(self) -> bool:
        """True iff generation is still running on Grok's side.

        We hit our polling timeout before progress reached 100, but
        ``video_id`` was already assigned and nothing was moderated —
        the video should finish on Grok's side. Use
        :meth:`GrokClient.wait_for_video_completion` to resume polling,
        or re-query via :meth:`GrokClient.get_post_details` later.
        """
        return bool(self.video_id) and self.progress < 100 and not self.moderated

    @property
    def success(self) -> bool:
        """True iff the video finished rendering AND was not moderated.

        Returns ``False`` for moderated results AND for results where we
        timed out while generation was still in-flight (``progress < 100``
        but ``video_id`` already assigned). Check :attr:`in_progress` if
        you need to distinguish "failed / moderated" from "still going —
        we gave up early". See :meth:`GrokClient.wait_for_video_completion`
        to recover the latter without re-submitting.
        """
        return self.progress == 100 and not self.moderated


class VideoExtendResult(BaseModel):
    """Result of extend_video() — extends a video with continuation frames."""

    video_id: str = Field(..., description="New extended video UUID")
    source_video_id: str = Field(
        ...,
        description=(
            "The video_id you passed to extend_video() — the source clip "
            "this extension was generated from. Same role as "
            "VideoGenerationResult.source_post_id but preserved under "
            "this name for extend flows since the source is always a "
            "video, not an image."
        ),
    )
    parent_post_id: str = Field(
        ...,
        description=(
            "The post id the extended video is conceptually linked to "
            "(usually the same as source_video_id). NOTE: this is NOT "
            "always Grok's internal chain-parent. If you need Grok's "
            "authoritative chain parent, inspect "
            "get_post_details(video_id).original_post_id. Prefer "
            "download_video(video_id) directly — it no longer needs "
            "you to pass a parent."
        ),
    )
    moderated: bool = Field(False, description="True if content was flagged by moderation")
    progress: int = Field(100, description="Generation progress (100 = complete)")
    mode: str = Field("extend", description="Generation mode")
    model_name: str | None = Field(None, description="Model used")
    conversation_id: str | None = Field(None, description="Chat conversation UUID")
    statsig_id: str | None = Field(
        None,
        description="Style seed (x-statsig-id) used for this generation",
    )
    seed_start_requested: float | None = Field(
        None,
        description=(
            "Seed offset (seconds) the caller asked for. None when the "
            "classic tail-extend path was used (no filmstrip drag)."
        ),
    )
    seed_start_actual: float | None = Field(
        None,
        description=(
            "Seed offset (seconds) where the filmstrip handle actually "
            "landed, read back from the UI's inline width% after the "
            "drag. Precision ~0.01s. Compare against seed_start_requested "
            "to verify drift."
        ),
    )
    seed_start_displayed: int | None = Field(
        None,
        description=(
            "Seed offset as shown in the UI's M:SS display (integer "
            "seconds). Useful for logs / user-facing messages; "
            "seed_start_actual is preferred for precision."
        ),
    )
    duration_s: int | None = Field(
        None,
        description=(
            "Actual length (seconds) of THIS extension, as reported by "
            "Grok's post metadata after render. Grok occasionally "
            "re-segments, so this may differ from the `duration` "
            "parameter you passed in. None if the post metadata wasn't "
            "ready at return time, or if the request was moderated."
        ),
    )
    cumulative_duration_s: float | None = Field(
        None,
        description=(
            "Total chain length from root (seconds) — equals "
            "videoExtensionStartTime + videoDuration on the new video's "
            "post metadata. Use this to implement chain-length checks "
            "(e.g. Grok's ~30s cap): "
            "``can_extend_more = result.cumulative_duration_s < 30``. "
            "For the first extension of a chain, this equals duration_s "
            "itself. None if post metadata wasn't ready or moderated."
        ),
    )
    is_persisted: bool | None = Field(
        None,
        description=(
            "Whether ``video_id`` resolves to a real, fetchable post on "
            "Grok's side. See :class:`VideoGenerationResult.is_persisted` "
            "for full semantics — same field for the same reason."
        ),
    )

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for the extended video's parent post."""
        return f"https://grok.com/imagine/post/{self.source_video_id}"

    @property
    def is_complete(self) -> bool:
        """True iff Grok reported progress=100 before we returned.

        A ``False`` value can mean either *failed* or *still in-flight*
        — discriminate with :attr:`in_progress` / :attr:`moderated`.
        """
        return self.progress == 100

    @property
    def in_progress(self) -> bool:
        """True iff the extension is still running on Grok's side.

        We hit our polling timeout before progress reached 100, but
        ``video_id`` was already assigned and nothing was moderated.
        Resume polling via
        :meth:`GrokClient.wait_for_video_completion` (pass ``video_id``).
        """
        return bool(self.video_id) and self.progress < 100 and not self.moderated

    @property
    def success(self) -> bool:
        """True iff the extension finished AND was not moderated.

        Returns ``False`` for moderated results AND for partial results
        where we timed out while generation was still in-flight. Check
        :attr:`in_progress` to distinguish, and use
        :meth:`GrokClient.wait_for_video_completion` to resume.
        """
        return self.progress == 100 and not self.moderated


class ImageEditResult(BaseModel):
    """Result of edit_image_via_ui() API call."""

    # Source info
    post_id: str = Field(..., description="Original post UUID that was edited")
    edit_prompt: str = Field(..., description="Edit prompt used")

    # Generated images (each with id, url, moderated status)
    images: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "List of generated images. Each dict carries: image_id, "
            "post_id (alias for image_id — the edit output IS a Grok "
            "post, both UUIDs are equal), image_url, moderated, "
            "progress. Feed post_id into "
            "``create_video({'images': ['post:<uuid>'], ...})`` to "
            "chain into img2vid."
        ),
    )

    # Conversation info (for debugging)
    conversation_id: str | None = Field(None, description="Chat conversation UUID")

    @computed_field
    @property
    def image_urls(self) -> list[str]:
        """URLs of successfully generated (non-moderated) images."""
        urls = []
        for img in self.images:
            if img.get("moderated") or not img.get("image_url"):
                continue
            url = img["image_url"]
            # Handle both full URLs and relative paths
            if url.startswith("http"):
                urls.append(url)
            else:
                urls.append(f"https://assets.grok.com/{url}")
        return urls

    @computed_field
    @property
    def moderated_count(self) -> int:
        """Number of images that were moderated."""
        return sum(1 for img in self.images if img.get("moderated"))

    @computed_field
    @property
    def r_rated_count(self) -> int:
        """Number of images flagged as R-rated (adult content)."""
        return sum(1 for img in self.images if img.get("r_rated"))

    @computed_field
    @property
    def success_count(self) -> int:
        """Number of successfully generated (non-moderated) images."""
        return len(self.images) - self.moderated_count

    @computed_field
    @property
    def total_count(self) -> int:
        """Total images generated (successful + moderated)."""
        return len(self.images)

    def has_enough_success(self, min_count: int = 1) -> bool:
        """Check if at least min_count images were generated successfully."""
        return self.success_count >= min_count

    @property
    def success(self) -> bool:
        """Check if at least one image was generated successfully."""
        return self.success_count > 0

    @computed_field
    @property
    def post_ids(self) -> list[str]:
        """Post IDs of successfully generated images (for saving via favorite_post())."""
        return [
            img["post_id"] for img in self.images if not img.get("moderated") and img.get("post_id")
        ]


class ImageGenerationResult(BaseModel):
    """Result of create_image() API call (text-to-image generation).

    IMPORTANT: Generated images are temporary and NOT automatically saved.
    The gallery disappears on page refresh. To persist an image, you must
    manually favorite/save it using favorite_post() with the post_id.
    """

    # Source info
    prompt: str = Field(..., description="Text prompt used for generation")

    # Generated images (each with id, url, moderated status, r_rated, etc.)
    images: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of generated images with keys: image_id, image_url, moderated, r_rated",
    )

    # Conversation info (for debugging)
    conversation_id: str | None = Field(None, description="Chat conversation UUID")

    # Post IDs collected via thumbnail_selector callback
    selected_post_ids: list[str] = Field(
        default_factory=list,
        description="Post IDs of images selected via thumbnail_selector callback",
    )

    @computed_field
    @property
    def image_urls(self) -> list[str]:
        """URLs of successfully generated (non-moderated) images."""
        urls = []
        for img in self.images:
            if img.get("moderated") or not img.get("image_url"):
                continue
            url = img["image_url"]
            # Handle both full URLs and relative paths
            if url.startswith("http"):
                urls.append(url)
            else:
                urls.append(f"https://assets.grok.com/{url}")
        return urls

    @computed_field
    @property
    def moderated_count(self) -> int:
        """Number of images that were moderated."""
        return sum(1 for img in self.images if img.get("moderated"))

    @computed_field
    @property
    def r_rated_count(self) -> int:
        """Number of images flagged as R-rated (adult content)."""
        return sum(1 for img in self.images if img.get("r_rated"))

    @computed_field
    @property
    def success_count(self) -> int:
        """Number of successfully generated (non-moderated) images."""
        return len(self.images) - self.moderated_count

    @computed_field
    @property
    def total_count(self) -> int:
        """Total images generated (successful + moderated)."""
        return len(self.images)

    def has_enough_success(self, min_count: int = 1) -> bool:
        """Check if at least min_count images were generated successfully."""
        return self.success_count >= min_count

    @property
    def success(self) -> bool:
        """Check if at least one image was generated successfully."""
        return self.success_count > 0

    @computed_field
    @property
    def post_ids(self) -> list[str]:
        """Post IDs of successfully generated images (for saving via favorite_post())."""
        return [
            img["post_id"] for img in self.images if not img.get("moderated") and img.get("post_id")
        ]
