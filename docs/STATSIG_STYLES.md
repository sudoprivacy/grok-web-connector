# Statsig Stable ID & Video Style Control

This document explains how to control video generation styles using Statsig stable_id.

## Current Status (2025-12-12)

**Video style variation via stable_id is currently NOT effective.**

After extensive testing, we found that all videos now have "camera zoom in" behavior regardless of:
- Different stable_ids
- Different geographic locations (tested: Japan, Taiwan, Korea via VPN)
- Manual vs automated generation

This suggests Grok has updated their video generation model globally to use "camera zoom in" as the default/only camera behavior. The stable_id injection code still works technically, but does not produce different styles.

## Background

Grok's video generation uses Statsig for A/B testing, which determines the video generation "style" (camera motion, subject motion, timing, etc.) based on a `stable_id`.

### Key Concepts

| Term | Description |
|------|-------------|
| `stable_id` | 94-character base64 string stored in localStorage. Determines your A/B bucket. |
| `x-statsig-id` | Request header containing crypto signature of (stable_id + request data). |
| Style bucket | Server-side assignment based on stable_id. Same stable_id = same style. |

## Using Custom Stable IDs

The `GrokClient` supports generating and injecting custom stable_ids to control video styles:

```python
from grok_web import GrokClient

async with GrokClient(host="127.0.0.1", port=9222) as client:
    # Generate a new stable_id
    new_stable_id = GrokClient.generate_stable_id()

    # Option 1: Inject stable_id and generate via unified API
    await client.set_stable_id(new_stable_id)
    result = await client.create_video(source_post_id="abc-123")

    # Option 2: Inject stable_id first, then generate multiple videos
    await client.set_stable_id(new_stable_id)
    result1 = await client.create_video(source_post_id="abc-123")
    result2 = await client.create_video(source_post_id="abc-123")
    # Both videos will have the same style

    # Check current stable_id
    current_id = await client.get_stable_id()
```

## API Reference

### `GrokClient.generate_stable_id() -> str`

Static method. Generates a valid Statsig stable_id.

```python
stable_id = GrokClient.generate_stable_id()
# Returns: 94-character base64 string like "PLV6AzP3uq1/fbC0U5Sj6PeT..."
```

### `await client.get_stable_id() -> str | None`

Returns the current stable_id from localStorage, or None if not set.

### `await client.set_stable_id(stable_id, reload_page=True) -> bool`

Injects a custom stable_id into localStorage.

- `stable_id`: The stable_id to inject
- `reload_page`: Whether to reload page after injection (default: True)
- Returns: True if stable_id was successfully injected and kept

### `await client.create_video(...)`

Generate video via the unified API. Stable_id is picked up automatically from localStorage.

## Observed Style Patterns (Historical)

> **Note**: As of 2025-12-12, these style variations are no longer observed. All videos now show "camera zoom in" behavior. This section is kept for historical reference.

Based on experiments with the same source image (before the model update):

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
- Must use browser automation (GrokClient) for stable_id control
- Style bucket assignment is server-side; we can only discover patterns empirically

## Investigation Notes (2025-12-12)

We conducted extensive research to understand why video styles changed. Key findings:

### What We Tested
- 10+ different stable_ids on the same image
- VPN connections to Taiwan and Korea
- Client-side country code modification
- Request header analysis

### What We Found
1. **Video request does not contain country/geo data** - The server determines location from IP address
2. **Cloudflare routes requests based on network topology**, not just IP geolocation
3. **Mixpanel analytics tracks `country: JP/KR/etc`** but this is not used for style assignment
4. **The `experiments: []` field in requests is empty** - all experiment assignment is server-side

### Conclusion
The "camera zoom in" behavior appears to be a global model/configuration update, not a geo-specific change. The stable_id mechanism still works for A/B bucket assignment, but all available buckets now produce the same camera behavior.
