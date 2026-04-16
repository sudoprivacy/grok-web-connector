"""Prompt parser for @N image references.

Parses prompts like "zoom into @1, pan to @2" into typed segments
for the UI automation layer to execute.
"""

from __future__ import annotations

import re


def parse_prompt(prompt: str, images: list[str]) -> list[dict]:
    """Parse prompt with @N image references into segments.

    Args:
        prompt: Text with optional @1, @2... references.
        images: List of image sources (used to validate indices).

    Returns:
        List of segments::

            [{"type": "text", "value": "zoom into "},
             {"type": "ref", "index": 1},
             {"type": "text", "value": ", pan to "},
             {"type": "ref", "index": 2}]

    Raises:
        ValueError: If @N index is out of range for the images list.
    """
    if not prompt:
        return []

    segments: list[dict] = []
    last_end = 0

    for match in re.finditer(r"@(\d+)", prompt):
        # Text before this reference
        if match.start() > last_end:
            segments.append({"type": "text", "value": prompt[last_end : match.start()]})

        index = int(match.group(1))
        if index < 1 or index > len(images):
            raise ValueError(
                f"@{index} is out of range: only {len(images)} image(s) provided "
                f"(use @1 to @{len(images)})"
            )

        segments.append({"type": "ref", "index": index})
        last_end = match.end()

    # Trailing text
    if last_end < len(prompt):
        segments.append({"type": "text", "value": prompt[last_end:]})

    return segments


def classify_image_source(source: str) -> tuple[str, str]:
    """Classify an image source string.

    Args:
        source: Image source — either 'post:<uuid>' or a file path.

    Returns:
        Tuple of (source_type, value):
        - ('post', '<uuid>') for existing Grok posts
        - ('file', '<path>') for local file paths

    Examples:
        >>> classify_image_source('post:8ddd91f6-abcd-1234-5678-abcdef012345')
        ('post', '8ddd91f6-abcd-1234-5678-abcdef012345')
        >>> classify_image_source('./frame1.jpg')
        ('file', './frame1.jpg')
    """
    if source.startswith("post:"):
        return ("post", source[5:])
    return ("file", source)
