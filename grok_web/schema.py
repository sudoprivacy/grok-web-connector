"""SSOT parameter definitions for Grok Web Connector APIs.

Every parameter description string exists exactly once here.
Docstrings, CLI --help, and error messages all derive from PARAMS.

Update path when Grok changes: edit one entry in PARAMS →
docstrings, --help, validation errors all update automatically.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# Layer 1: Parameter definitions (each string written ONCE)
# =============================================================================

PARAMS: dict[str, dict[str, Any]] = {
    "images": {
        "desc": (
            "Image references. Each entry is a local file path, "
            "'post:<uuid>' (an existing Grok image post — connector "
            "downloads the media and re-uploads as a ref), 'video:<uuid>' "
            "(only for create_video → triggers video-extend), or "
            "'file:<uuid>' (only for create_video → previously uploaded "
            "via client.upload_images, skips re-upload). \n\n"
            "**Per-method semantics**:\n"
            "  • create_video: post: → img2vid (single source); video: → "
            "video-extend (single source); file:/local paths → upload2vid "
            "with @N marker support.\n"
            "  • create_image: each entry uploaded as a ref; @N in prompt "
            "maps to images[N-1] (1-based). Empty/omitted = txt2img.\n"
            "  • edit_image: images[0] is the post being edited (must be "
            "post:<uuid>); images[1:] are reference images. @1 = source, "
            "@2..@N+1 = refs.\n"
            "  • upload_images: each entry must be a local path."
        ),
        "type": "list[str]",
    },
    "prompt": {
        "desc": "Text prompt. Use @1, @2... to reference images by position in 'images' list.",
        "type": "str",
    },
    "mode": {
        "desc": "Generation mode: 'image' or 'video'.",
        "type": "str",
        "default": "video",
    },
    "resolution": {
        "desc": "Video resolution: '480p', '720p'.",
        "type": "str",
        "default": "720p",
    },
    "duration": {
        "desc": "Video duration: '6s', '10s'.",
        "type": "str",
        "default": "10s",
    },
    "aspect_ratio": {
        "desc": "Aspect ratio: '2:3', '3:2', '1:1', '9:16', '16:9', 'portrait', 'landscape', 'square'.",
        "type": "str",
        "default": "2:3",
    },
    "preset": {
        "desc": "Video style: 'normal', 'fun', 'spicy'.",
        "type": "str",
    },
    "timeout": {
        "desc": (
            "Max seconds to wait for generation. Per-endpoint defaults: "
            "create_image / edit_image = 300 (image gen is fast); "
            "create_video / extend_video = 600 (img2vid under queue "
            "pressure or NSFW routing regularly needs >300s); "
            "select_post = 6.0 (sidebar-correction wait, not page-load). "
            "If create_video / extend_video returns with ``in_progress=True`` "
            "you can resume polling via "
            "``client.wait_for_video_completion({'video_id': ..., 'timeout': N})`` "
            "without re-submitting the job."
        ),
        "type": "int",
        "default": 300,
    },
    "wait_for_video": {
        "desc": "Wait for video element to load (txt2vid only).",
        "type": "bool",
        "default": True,
    },
    "verify_final": {
        "desc": (
            "After generation, confirm post-render moderation via REST and "
            "OR the verdict into result.moderated. Grok moderates twice — "
            "once on the prompt/refs (reflected in the immediate response) "
            "and again after the video renders. Adds ~150ms. For finer "
            "control call client.check_video_moderated(video_id) directly."
        ),
        "type": "bool",
        "default": False,
    },
    "min_success": {
        "desc": "Minimum non-moderated images needed.",
        "type": "int",
        "default": 1,
    },
    "quality": {
        "desc": (
            "Generation quality preset: 'speed' (faster, lower quality, "
            "default — matches Grok Imagine's default) or 'quality' "
            "(slower, higher fidelity). Grok added this toggle in 2026-04; "
            "unknown values are passed through with a warning so future "
            "additions like 'ultra' don't hard-break."
        ),
        "type": "str",
        "default": "speed",
    },
    "max_scroll": {
        "desc": "Max scroll attempts for more images.",
        "type": "int",
        "default": 5,
    },
    "auto_favorite": {
        "desc": (
            "Auto-favorite the first N gallery images as persistent "
            "posts on the user's grok.com account. Default 0 — "
            "generated images already come back as ephemeral CDN URLs "
            "(result.images[].image_url, durable) without any account "
            "modification; setting N>=1 ALSO persists the first N as "
            "real favorites so they get a post_id usable in "
            "edit_image / create_video({'images': ['post:<uuid>']}). "
            "WARNING: this mutates grok.com account state — every "
            "favorite is visible in the user's favorites list and "
            "counts toward moderation history. Prefer keeping the "
            "default 0 for batch generation / smoke tests / CI / "
            "NSFW-adjacent exploration; opt in only when you need a "
            "post_id downstream. JSON/CLI equivalent of "
            "thumbnail_selector=auto_favorite_first_n(N)."
        ),
        "type": "int",
        "default": 0,
    },
    "thumbnail_selector": {
        "desc": (
            "ADVANCED / Python-only. Callable hook run after image "
            "generation completes; use for human-in-the-loop flows "
            "(signal_file_selector, timeout_selector) or custom "
            "selection logic. For JSON/CLI batch use, prefer "
            "auto_favorite. When both are set, thumbnail_selector "
            "wins. JSON values (dict/str) are ignored with a warning."
        ),
        "type": "callable",
    },
    "post_id": {
        "desc": "Target post UUID (for edit_image).",
        "type": "str",
    },
    "edit_prompt": {
        "desc": "Edit instruction (e.g., 'add sunglasses').",
        "type": "str",
    },
    "video_id": {
        "desc": "Target video UUID to extend (for extend_video).",
        "type": "str",
    },
    "seed_start": {
        "desc": (
            "Seconds from the start of the source video where the extension "
            "should seed. Video-extend only — requires 'video:<uuid>' ref in "
            "images (dict API) or use of client.extend_video. Grok anchors a "
            "fixed-length seed window at this position and generates "
            "'duration' more seconds after it. **Omitting (None)** pins the "
            "seed at video_id's own chain tail, which makes consecutive "
            "calls on the same video_id produce parallel branches (fanout) "
            "rather than silently walking Grok's chain tail. For serial "
            "extension, pass video_id=previous_result.video_id on each "
            "iteration. The drag is pixel-based so the actual value may "
            "drift slightly — see VideoExtendResult.seed_start_actual for "
            "what landed (UI shows integer seconds; internal precision is "
            "~0.01s). Valid range: 0 to source_video.duration. As of "
            "2026-04 the seed-window length is fixed at '6s' or '10s' per "
            "the 'duration' field; a future Grok revision may extend this "
            "set — the library passes unknown duration values through with "
            "a warning."
        ),
        "type": "float",
    },
    "preserve_source_favorite_state": {
        "desc": (
            "Opt-in cleanup of Grok's silent auto-favoriting on "
            "create_video / extend_video. When the UI click fires "
            "('制作视频' / '扩展'), Grok appends the source post/video "
            "to the user's favorites on each call — batches of N "
            "generations leave N-ish duplicate entries in the "
            "favorites tab. Setting this to True asks the connector "
            "to snapshot the source's favorite state before the call "
            "and revert AFTER (only when the source was confirmed "
            "NOT favorited pre-call — a revert in the other case "
            "risks removing a favorite the user placed themselves). "
            "Defaults to False to satisfy the mutation-opt-in rule: "
            "the connector never writes to the favorites list unless "
            "the caller explicitly asks."
        ),
        "type": "bool",
        "default": False,
    },
    "output_path": {
        "desc": (
            "Local destination path for download_video. Existing files "
            "are overwritten; parent directory must already exist. Accepts "
            "str or pathlib.Path."
        ),
        "type": "str | Path",
    },
    "prefer_hd": {
        "desc": (
            "If True (default), download_video prefers hdMediaUrl when "
            "available, falling back to mediaUrl. Set False to force the "
            "smaller mediaUrl asset."
        ),
        "type": "bool",
        "default": True,
    },
    "poll_interval": {
        "desc": (
            "Seconds between REST polls in wait_for_video_completion. "
            "Default 5.0; lower values poll faster (and burn more quota), "
            "higher values cost more wall-clock latency on the success edge."
        ),
        "type": "float",
        "default": 5.0,
    },
    # --- xAI REST API params (used by XAIClient, not GrokClient) ---
    "model": {
        "desc": (
            "xAI model name. Image: 'grok-imagine-image' ($0.02) or "
            "'grok-imagine-image-quality' ($0.05). Video: 'grok-imagine-video' "
            "($0.05/s) or 'grok-imagine-video-1.5' ($0.08/s)."
        ),
        "type": "str",
    },
    "output_count": {
        "desc": "Number of images to generate per request (1-10).",
        "type": "int",
        "default": 4,
    },
    "response_format": {
        "desc": "Image response format: 'url' (CDN link) or 'b64_json' (base64-encoded).",
        "type": "str",
        "default": "url",
    },
}

# =============================================================================
# Layer 2: API schemas (references to PARAMS keys)
# =============================================================================

VIDEO_KEYS = [
    "images",
    "prompt",
    "mode",
    "resolution",
    "duration",
    "aspect_ratio",
    "preset",
    "seed_start",
    "timeout",
    "wait_for_video",
    "verify_final",
    "preserve_source_favorite_state",
]

# Keys accepted by extend_video() — keyword args rather than a dict, but
# the docstring is still generated from this list for SSOT consistency.
EXTEND_KEYS = [
    "video_id",
    "seed_start",
    "duration",
    "prompt",
    "timeout",
    "preserve_source_favorite_state",
]

IMAGE_KEYS = [
    "images",
    "prompt",
    "aspect_ratio",
    "quality",
    "min_success",
    "max_scroll",
    "timeout",
    "auto_favorite",
    "thumbnail_selector",
]

EDIT_KEYS = [
    # Canonical (v0.18.0+) — semi-structured prompt + images
    "prompt",
    "images",
    # Legacy aliases (deprecated v0.18.0, removed v0.19.0)
    "post_id",
    "edit_prompt",
    "timeout",
]

UPLOAD_KEYS = [
    "images",
]

# Keys accepted by download_video({...}) — dict-style as of v0.19.0.
DOWNLOAD_KEYS = [
    "video_id",
    "output_path",
    "prefer_hd",
]

# Keys accepted by select_post({...}) — dict-style as of v0.19.0.
SELECT_POST_KEYS = [
    "post_id",
    "timeout",
]

# Keys accepted by wait_for_video_completion({...}) — dict-style as of v0.19.0.
WAIT_FOR_COMPLETION_KEYS = [
    "video_id",
    "timeout",
    "poll_interval",
]

# --- xAI REST API key groups (used by XAIClient) ---

API_IMAGE_KEYS = [
    "prompt",
    "model",
    "output_count",
    "aspect_ratio",
    "response_format",
    "timeout",
]

API_VIDEO_KEYS = [
    "prompt",
    "model",
    "images",
    "duration",
    "timeout",
    "poll_interval",
]

API_EDIT_KEYS = [
    "prompt",
    "model",
    "images",
    "response_format",
    "timeout",
]

# =============================================================================
# Layer 3: Utilities — everything derived from Layer 1 + 2
# =============================================================================


def get_schema(keys: list[str]) -> dict[str, dict[str, Any]]:
    """Build schema dict from key list.

    Returns:
        Dict mapping param name to its PARAMS entry.

    Raises:
        KeyError: If a key is not defined in PARAMS.
    """
    return {k: PARAMS[k] for k in keys}


def schema_to_docstring(keys: list[str]) -> str:
    """Generate a docstring Args section from PARAMS.

    Example output::

        images (list[str]): Image sources. Local file paths ...
        prompt (str): Text prompt. Use @1, @2...
        resolution (str, default '720p'): Video resolution: '480p', '720p'.
    """
    lines = []
    for key in keys:
        p = PARAMS[key]
        type_str = p.get("type", "Any")
        default = p.get("default")
        if default is not None:
            sig = f"{key} ({type_str}, default {default!r})"
        else:
            sig = f"{key} ({type_str})"
        lines.append(f"{sig}: {p['desc']}")
    return "\n".join(lines)


def schema_to_help(keys: list[str]) -> str:
    """Generate CLI help text from PARAMS.

    Example output::

        Supported parameters (pass via --params JSON):
          images       Image sources. Local file paths ...
          prompt       Text prompt. Use @1, @2...
          resolution   Video resolution: '480p', '720p'. [default: 720p]
    """
    lines = ["Supported parameters (pass via --params JSON):"]
    for key in keys:
        p = PARAMS[key]
        default = p.get("default")
        desc = p["desc"]
        if default is not None:
            desc += f" [default: {default}]"
        lines.append(f"  {key:<20s} {desc}")
    return "\n".join(lines)


def splice_schema_into_docstring(doc: str | None, keys: list[str]) -> str | None:
    """Replace ``<SCHEMA_ARGS>`` marker in a docstring with generated Args.

    The marker must appear on its own line; the leading whitespace of that
    line is used as indentation for the expanded block. Returns the modified
    docstring, or the original if the marker is absent.

    Example input::

        \"\"\"Do a thing.

        Args:
            params: Dict with keys:
                <SCHEMA_ARGS>

        Returns:
            ...
        \"\"\"

    After splicing with ``keys=VIDEO_KEYS``, the marker is replaced by the
    output of :func:`schema_to_docstring` at the same indentation.
    """
    if not doc or "<SCHEMA_ARGS>" not in doc:
        return doc
    lines = doc.split("\n")
    for i, line in enumerate(lines):
        idx = line.find("<SCHEMA_ARGS>")
        if idx == -1:
            continue
        indent = line[:idx]
        block = schema_to_docstring(keys)
        lines[i] = "\n".join(indent + ln for ln in block.split("\n"))
        break
    return "\n".join(lines)


def validate_params(params: dict, keys: list[str]) -> dict:
    """Validate and clean params dict against a schema.

    - Warns on unknown keys (not in schema).
    - Applies defaults from PARAMS for missing keys.
    - Drops ``"callable"``-typed params that aren't actually callable
      (common JSON/CLI footgun — e.g. passing a ``thumbnail_selector``
      string literal from ``--params`` deserializes to ``str`` and
      would crash downstream). Warns the caller so they know to use
      the JSON-friendly alternative instead.
    - Returns cleaned dict with defaults applied.
    """
    cleaned = {}

    # Warn on unknown keys
    valid_keys = set(keys)
    for k in params:
        if k not in valid_keys:
            valid_list = ", ".join(sorted(valid_keys))
            logger.warning(f"Unknown parameter '{k}' (valid: {valid_list})")

    # Apply defaults and copy provided values
    for key in keys:
        if key in params:
            value = params[key]
            expected_type = PARAMS[key].get("type")
            if expected_type == "callable" and value is not None and not callable(value):
                logger.warning(
                    f"Parameter '{key}' expects a Python callable but got "
                    f"{type(value).__name__} — dropping. If you're using "
                    f"the JSON/CLI API, look for a non-callable equivalent "
                    f"in the schema (e.g. auto_favorite instead of "
                    f"thumbnail_selector)."
                )
                if "default" in PARAMS[key]:
                    cleaned[key] = PARAMS[key]["default"]
                continue
            cleaned[key] = value
        elif "default" in PARAMS[key]:
            cleaned[key] = PARAMS[key]["default"]

    return cleaned
