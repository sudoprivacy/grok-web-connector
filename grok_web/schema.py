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
            "Source references. Local file paths (uploads), 'post:<uuid>' "
            "(existing Grok IMAGE post, triggers img2vid), 'video:<uuid>' "
            "(existing Grok VIDEO post, triggers video-extend), or "
            "'file:<uuid>' (previously uploaded via client.upload_images "
            "— skips re-upload). Max 5 for images/uploads; only first "
            "'video:' ref is used."
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
        "desc": "Max seconds to wait for generation.",
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
    "max_scroll": {
        "desc": "Max scroll attempts for more images.",
        "type": "int",
        "default": 5,
    },
    "thumbnail_selector": {
        "desc": "Callback for selecting images. Python API only.",
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
    "timeout",
    "wait_for_video",
    "verify_final",
]

IMAGE_KEYS = [
    "images",
    "prompt",
    "aspect_ratio",
    "min_success",
    "max_scroll",
    "timeout",
    "thumbnail_selector",
]

EDIT_KEYS = [
    "post_id",
    "edit_prompt",
    "timeout",
]

UPLOAD_KEYS = [
    "images",
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


def validate_params(params: dict, keys: list[str]) -> dict:
    """Validate and clean params dict against a schema.

    - Warns on unknown keys (not in schema).
    - Applies defaults from PARAMS for missing keys.
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
            cleaned[key] = params[key]
        elif "default" in PARAMS[key]:
            cleaned[key] = PARAMS[key]["default"]

    return cleaned
