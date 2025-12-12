# Statsig Stable ID & Video Style Control

This document explains how to control video generation styles using Statsig stable_id.

## Background

Grok's video generation uses Statsig for A/B testing, which determines the video generation "style" (camera motion, subject motion, timing, etc.) based on a `stable_id`.

### Key Concepts

| Term | Description |
|------|-------------|
| `stable_id` | 94-character base64 string stored in localStorage. Determines your A/B bucket. |
| `x-statsig-id` | Request header containing crypto signature of (stable_id + request data). |
| Style bucket | Server-side assignment based on stable_id. Same stable_id = same style. |

## Using Custom Stable IDs

The `NodriverClient` supports generating and injecting custom stable_ids to control video styles:

```python
from grok_web import NodriverClient

async with NodriverClient(host="127.0.0.1", port=9222) as client:
    # Generate a new stable_id
    new_stable_id = NodriverClient.generate_stable_id()

    # Option 1: Inject and generate video in one call
    result = await client.create_video_via_ui(
        parent_post_id="abc-123",
        stable_id=new_stable_id,
    )

    # Option 2: Inject stable_id first, then generate multiple videos
    await client.set_stable_id(new_stable_id)
    result1 = await client.create_video_via_ui(parent_post_id="abc-123")
    result2 = await client.create_video_via_ui(parent_post_id="abc-123")
    # Both videos will have the same style

    # Check current stable_id
    current_id = await client.get_stable_id()
```

## API Reference

### `NodriverClient.generate_stable_id() -> str`

Static method. Generates a valid Statsig stable_id.

```python
stable_id = NodriverClient.generate_stable_id()
# Returns: 94-character base64 string like "PLV6AzP3uq1/fbC0U5Sj6PeT..."
```

### `await client.get_stable_id() -> str | None`

Returns the current stable_id from localStorage, or None if not set.

### `await client.set_stable_id(stable_id, reload_page=True) -> bool`

Injects a custom stable_id into localStorage.

- `stable_id`: The stable_id to inject
- `reload_page`: Whether to reload page after injection (default: True)
- Returns: True if stable_id was successfully injected and kept

### `await client.create_video_via_ui(..., stable_id=None)`

Generate video with optional custom stable_id.

- `stable_id`: Optional. If provided, injects this stable_id before generation.

## Observed Style Patterns

Based on experiments with the same source image:

| Stable ID Pattern | Camera Behavior | Subject Motion |
|-------------------|-----------------|----------------|
| Pattern A | Gradual zoom in | Minimal movement |
| Pattern B | Hover 3s → zoom in | Head movement (up/down) |
| Pattern C | Zoom in → zoom out | Minimal movement |

Note: Actual style variations depend on the source image content and what movements are physically plausible.

## Technical Details

### Stable ID Format

```python
# Generation algorithm (matches Statsig SDK):
import base64, os
stable_id = base64.b64encode(os.urandom(70)).decode().rstrip("=")
# Result: 94-character base64 string
```

### How It Works

1. SDK checks `localStorage.getItem('STATSIG_LOCAL_STORAGE_STABLE_ID')`
2. If found, uses it for A/B bucket calculation
3. If not found, generates new one (but may not persist it)
4. Server extracts stable_id from signed `x-statsig-id` header
5. Same stable_id → deterministic style assignment

### Limitations

- Cannot inject stable_id via API (server validates crypto signature)
- Must use browser automation (NodriverClient) for stable_id control
- Style bucket assignment is server-side; we can only discover patterns empirically
