# Grok Web Connector

Python client library for interacting with Grok Imagine web API.

## Features

- **SmartGrokClient** - Recommended client with HTTP-first, browser-fallback strategy
- **9 Core APIs** - list_posts, get_post_details, create_video, like/unlike, and more
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

### Write APIs

```python
# Like/unlike posts
await client.like_post(post_id)
await client.unlike_post(post_id)

# Create video with preset
result = await client.create_video(
    parent_post_id="abc-123",
    preset="fun",  # "normal", "fun", or "spicy"
)
print(result.video_id)
```

## Video Presets

| Preset | Description |
|--------|-------------|
| `normal` | Default style |
| `fun` | More dynamic, playful |
| `spicy` | Most dramatic effects |

## Client Options

### SmartGrokClient (Recommended)

```python
from grok_web import get_client, SmartGrokClient

# Via factory function
client = get_client(browser_host="127.0.0.1", browser_port=9222)

# Or directly
client = SmartGrokClient(browser_host="127.0.0.1", browser_port=9222)
```

### Other Clients (Advanced)

```python
from grok_web import NodriverClient, AsyncClient, GrokClient

# Full browser automation (all ops go through browser)
async with NodriverClient(host="127.0.0.1", port=9222) as client:
    ...

# Playwright HTTP client (requires valid cf_clearance)
async with AsyncClient() as client:
    ...

# curl_cffi sync client (may get 403)
client = GrokClient()
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
