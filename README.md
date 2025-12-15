# Grok Web Connector

Python client library for interacting with Grok Imagine web API.

## Features

- **SmartGrokClient** - Recommended client with HTTP-first, browser-fallback strategy
- **12 Core APIs** - list_posts, get_post_details, create_video, favorite/unfavorite, like/dislike, and more
- **Auto Cloudflare bypass** - Browser fallback handles challenges automatically
- **Video presets** - Normal, Fun, Spicy modes for video generation
- Type-safe with Pydantic models

## Installation

```bash
pip install git+https://github.com/user/grok-web-connector.git
```

## Quick Start

### Read-only operations (no browser needed)

```python
from grok_web import get_client

async with get_client() as client:
    posts = await client.list_posts(limit=10)  # HTTP (fast)
    details = await client.get_post_details(post_id)  # HTTP (fast)
```

### Video creation (with browser fallback)

```bash
# Step 1: Start Chrome with remote debugging (once)
# macOS:
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Windows:
chrome.exe --remote-debugging-port=9222
```

```python
from grok_web import get_client

async with get_client(browser_host="127.0.0.1", browser_port=9222) as client:
    posts = await client.list_posts()  # HTTP first, browser fallback
    video = await client.create_video(post_id, preset="fun")  # Browser fallback
```

## Architecture

### SmartGrokClient (Recommended)

```
┌─────────────────────────────────────────────────────────────┐
│                     SmartGrokClient                         │
├─────────────────────────────────────────────────────────────┤
│  All operations:                                            │
│    1. Try HTTP first (fast, lightweight)                   │
│    2. On Cloudflare challenge → fallback to browser        │
│                                                             │
│  Browser mode uses fetch() inside browser context,          │
│  NOT clicking UI buttons (except for video creation)        │
└─────────────────────────────────────────────────────────────┘
```

### How browser fallback works

| Operation | HTTP Path | Browser Fallback |
|-----------|-----------|------------------|
| list_posts | Playwright fetch | Browser-context fetch() |
| get_post_details | Playwright fetch | Browser-context fetch() |
| create_video | API call (often 403) | **Click UI buttons** |

**Key insight**: Browser fallback for read APIs uses JavaScript `fetch()` inside the browser context, which automatically uses the browser's authenticated session cookies. Only `create_video` actually clicks UI buttons.

## Authentication Setup

### 1. Extract Cookies from Browser

1. Open https://grok.com in Chrome
2. Open DevTools (F12) → Application → Cookies
3. Copy these 4 cookie values:
   - `sso`
   - `sso-rw`
   - `x-userid`
   - `cf_clearance`

### 2. Create Config File

Create `~/.grok-config.json`:

```json
{
  "cookies": {
    "sso": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "sso-rw": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "x-userid": "<redacted-user-id>",
    "cf_clearance": "..."
  }
}
```

## API Reference

### Read APIs

```python
# List posts (default: liked posts)
posts = await client.list_posts(limit=10, source="favorites")

# Get post details
details = await client.get_post_details(post_id)

# Get asset file size
size = await client.get_asset_file_size(asset_url)

# Validate authentication
is_valid = await client.validate_auth()

# Match local video to web video
match = await client.match_local_video("/path/to/video.mp4")
```

### Favorite/Unfavorite APIs (save to collection)

```python
# Favorite/unfavorite posts (add/remove from favorites)
await client.favorite_post(post_id)    # HTTP first, browser fallback
await client.unfavorite_post(post_id)  # HTTP first, browser fallback
```

### Social APIs (thumbs up/down)

```python
# Like/dislike posts (thumbs up/down - browser only)
await client.like_post(post_id)     # Give thumbs up
await client.dislike_post(post_id)  # Give thumbs down
```

### Video APIs

```python
# Simple: Create video with preset
result = await client.create_video(post_id, preset="fun")  # "normal", "fun", or "spicy"

# Full control: Use adjustment_prompt for custom instructions
# (camera movement, character actions, style - anything!)
result = await client.create_video(
    post_id,
    adjustment_prompt="she slowly turns her head, Dolly In, cinematic"
)

# Upgrade to HD - adds hd_media_url field to video (~2x file size)
# Useful: generate many videos, upgrade only the best ones
await client.upgrade_video(video_id)

# Delete a video (browser only)
await client.delete_video(video_id)
```

### Image APIs

```python
# Edit image to generate variations (browser only)
result = await client.edit_image(post_id, "add sunglasses")
print(result.image_urls)
```

## Video Presets

| Preset | Description |
|--------|-------------|
| `normal` | Default style |
| `fun` | More dynamic, playful |
| `spicy` | Most dramatic effects |

## Adjustment Prompt (Video Generation)

The `adjustment_prompt` parameter controls how videos are generated from images. This is the same as typing in the Grok Imagine UI text box after selecting an image.

**You can specify ANY video adjustments**, not just camera movement:

| Category | Examples |
|----------|----------|
| **Camera** | "Static Shot", "Orbit", "Pan Left", "Dolly In", "Zoom Out" |
| **Motion** | "she turns her head", "wind blowing hair", "waves crashing" |
| **Combined** | "camera zooms in while he walks forward" |
| **Style** | "slow motion", "cinematic lighting" |

**Best practice formula**: `"Subject + Motion + Camera, Style..."`

```python
# Camera control
await client.create_video(post_id, adjustment_prompt="Static Shot")
await client.create_video(post_id, adjustment_prompt="slow orbit")

# Character actions
await client.create_video(post_id, adjustment_prompt="she slowly turns her head")
await client.create_video(post_id, adjustment_prompt="the character walks forward")

# Combined (recommended)
await client.create_video(
    post_id,
    adjustment_prompt="Woman walks through forest, Pan Left, cinematic lighting"
)
```

When `adjustment_prompt` is provided, it overrides `preset` and uses 'custom' mode.

See **[grok-imagine-expert/docs/CAMERA_CONTROL.md](https://github.com/user/grok-imagine-expert/blob/main/docs/CAMERA_CONTROL.md)** for camera-specific examples with tested demo URLs.

## Client Options

### Using get_client() (Recommended)

```python
from grok_web import get_client

# Via factory function (recommended entry point)
async with get_client(browser_host="127.0.0.1", browser_port=9222) as client:
    posts = await client.list_posts()
    await client.favorite_post(posts[0].id)
    video = await client.create_video(posts[0].id, preset="fun")
```

## Error Handling

```python
from grok_web import GrokAuthError, GrokAPIError, GrokNotFoundError

try:
    result = await client.create_video(post_id)
except GrokAuthError:
    # Cloudflare challenge or 403
    # If using SmartGrokClient without browser config, will raise this
    print("Need browser fallback - set browser_host and browser_port")
except GrokNotFoundError:
    print("Post not found")
except GrokAPIError as e:
    print(f"API error: {e}")
```

## Troubleshooting

### HTTP returns 403 / Cloudflare challenge

**Solution**: Use browser fallback

```python
# Add browser config
client = get_client(browser_host="127.0.0.1", browser_port=9222)
```

### Video creation blocked

The direct API (`create_video_from_image`) is often blocked with 403. SmartGrokClient automatically falls back to browser UI automation.

### cf_clearance expired

If you're not using browser fallback and cf_clearance expires:
1. Open Chrome, go to https://grok.com
2. Copy new `cf_clearance` cookie value
3. Update `~/.grok-config.json`

Or just use browser fallback which handles this automatically.

## License

MIT
