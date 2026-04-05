# Grok Web Connector

Python client library for interacting with Grok Imagine web API.

## Features

- **GrokClient** - Browser automation client via Chrome DevTools Protocol
- **BrowserWorkerPool** - Parallel task execution with automatic retry and progress persistence
- **15+ Core APIs** - list_posts, get_post_details, create_video, create_image, upload_image, download_video, and more
- **3 Video Creation Modes** - img2vid, txt2vid, upload2vid (local image to video)
- **Auto Cloudflare bypass** - Browser handles challenges automatically
- **Video presets** - Normal, Fun, Spicy modes for video generation
- **Dynamic task dispatch** - Zero-maintenance worker pool (new client methods auto-available)
- Type-safe with Pydantic models

## Project Structure

```
grok-web-connector/
├── grok_web/                    # Main package
│   ├── __init__.py              # Public API exports
│   ├── client.py                # GrokClient
│   ├── _internal.py             # Response parsing (DRY sync/async)
│   ├── browser.py               # Chrome/CDP management
│   ├── auth.py                  # HTTP authentication
│   ├── auth_manager.py          # Cookie management CLI
│   ├── models.py                # Pydantic data models
│   ├── exceptions.py            # Custom exceptions
│   ├── selectors.py             # UI element selectors
│   ├── actions/                 # Atomic UI actions (ax_tree-first)
│   │   ├── navigation.py        # navigate_to_post, is_on_post_page
│   │   ├── post_menu.py         # open_post_menu, click_menu_item
│   │   ├── post_media.py        # get_media_view, switch_to_image/video
│   │   ├── post_image.py        # get_thumbnails, select_thumbnail
│   │   ├── post_video.py        # get_video_thumbnails, select_video_thumbnail
│   │   └── network_monitor.py   # CDPMonitor context manager
│   └── pool/                    # BrowserWorkerPool
│       ├── __init__.py
│       ├── worker.py            # Worker process
│       └── worker_pool.py       # Pool manager
├── examples/                    # Example scripts
└── tests/                       # Test suite
```

## Installation

```bash
pip install git+https://github.com/elfenlieds7/grok-web-connector.git
```

## Quick Start

```python
from grok_web import get_client

async with get_client() as client:
    # Read operations
    posts = await client.list_posts(limit=10)
    details = await client.get_post_details(post_id)

    # Video generation (auto-detects mode)
    video = await client.create_video(source_post_id=post_id, preset="fun")

    # Image generation
    result = await client.create_image("a sunset over mountains")
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        GrokClient                            │
├─────────────────────────────────────────────────────────────┤
│  Browser automation via ai-dev-browser/CDP                   │
│                                                              │
│  Read APIs: fetch() inside browser context                   │
│  Write APIs: UI automation (ax_tree-first, CSS fallback)     │
└─────────────────────────────────────────────────────────────┘
```

| Operation | Method |
|-----------|--------|
| list_posts | Browser-context fetch() |
| get_post_details | Browser-context fetch() |
| create_video (img2vid) | UI automation |
| create_video (txt2vid) | UI automation |
| create_video (upload2vid) | UI automation |
| create_image | UI automation |
| upload_image | File input injection |
| extend_video | UI automation |
| edit_image | UI automation |

## Authentication

Authentication is **fully automatic**:

1. **First use**: Browser opens, you log in once, cookies are saved to `~/.grok-config.json`
2. **Subsequent uses**: Cookies are loaded automatically
3. **Auto-refresh**: Cookies are updated after each successful browser operation

```bash
# Optional: Check authentication status
python -m grok_web.auth_manager status

# Optional: Clear saved authentication
python -m grok_web.auth_manager clear
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
await client.favorite_post(post_id)
await client.unfavorite_post(post_id)
```

### Social APIs (thumbs up/down)

```python
await client.like_post(post_id)
await client.dislike_post(post_id)
```

### Video APIs

```python
# === Mode 1: img2vid (from existing Grok post) ===
result = await client.create_video(source_post_id=post_id, preset="fun")

# With custom prompt for camera/motion control
result = await client.create_video(
    source_post_id=post_id,
    prompt="she slowly turns her head, Dolly In, cinematic"
)

# Generate from specific image thumbnail
result = await client.create_video(
    source_post_id=post_id, preset="fun", thumbnail_index=2
)

# === Mode 2: txt2vid (text to video) ===
result = await client.create_video("a cat wearing sunglasses in space")

# === Mode 3: upload2vid (local image to video) ===
result = await client.create_video(
    "slow zoom in",
    source_image_path="~/photos/portrait.jpg"
)

# Extend video (generate continuation frames)
extend_result = await client.extend_video(video_id)

# Upgrade to HD
await client.upgrade_video(video_id)

# Download video to local file
await client.download_video(video_id, "output.mp4", parent_post_id=post_id)

# Delete a video
await client.delete_video(video_id)
```

### Image APIs

```python
# Create image from text (txt2img)
result = await client.create_image("a sunset over mountains")

# Edit existing image
result = await client.edit_image(post_id, "add sunglasses")

# Upload local image (creates a new post)
new_post_id = await client.upload_image("~/photos/portrait.jpg")

# Delete an image variant
await client.delete_image(post_id, thumbnail_index=2)
```

### Thumbnail Selection

```python
# Image thumbnails
thumbnails = await client.get_thumbnails(post_id)
# [{"index": 1, "name": "Thumbnail 1", "ref": "..."}, ...]
await client.select_thumbnail(post_id, index=2)

# Video thumbnails
video_thumbs = await client.get_video_thumbnails(post_id)
await client.select_video_thumbnail(post_id, index=1)
```

### Image-Video Relationship

```python
# Get which videos came from which image variant
details = await client.get_post_details(post_id)

# Group videos by source image
by_source = details.videos_by_source()
# {"abc-123": [video1, video2], "def-456": [video3]}

# Find which image a specific video came from
source_id = details.find_video_source(video_id)

# Get full map with image URLs
image_map = await client.get_image_video_map(post_id)
# [{"post_id": "abc", "media_url": "https://...", "videos": [...]}, ...]
```

### URL Patterns

| URL Type | Pattern |
|----------|---------|
| Web page | `https://grok.com/imagine/post/{video_id}` |
| HD video | `https://imagine-public.x.ai/imagine-public/share-videos/{video_id}_hd.mp4` |
| SD video | `https://imagine-public.x.ai/imagine-public/share-videos/{video_id}.mp4` |

## Video Presets

| Preset | Description |
|--------|-------------|
| `normal` | Default style |
| `fun` | More dynamic, playful |
| `spicy` | Most dramatic effects |

## Prompt Parameter (Video Generation)

The `prompt` parameter controls how videos are generated. For img2vid, this is the same as typing in the Grok Imagine UI text box after selecting an image.

| Category | Examples |
|----------|----------|
| **Camera** | "Static Shot", "Orbit", "Pan Left", "Dolly In", "Zoom Out" |
| **Motion** | "she turns her head", "wind blowing hair", "waves crashing" |
| **Combined** | "camera zooms in while he walks forward" |
| **Style** | "slow motion", "cinematic lighting" |

**Best practice formula**: `"Subject + Motion + Camera, Style..."`

When `prompt` is provided with `source_post_id`, it overrides `preset` and uses 'custom' mode.

## Client Options

```python
from grok_web import get_client

async with get_client(browser_host="127.0.0.1", browser_port=9222) as client:
    posts = await client.list_posts()
```

## Batch Operations (BrowserWorkerPool)

```python
from grok_web import BrowserWorkerPool

async with BrowserWorkerPool(num_workers=3) as pool:
    job_ids = []
    for post_id in parent_posts:
        job_id = await pool.submit(
            "create_video",
            prompt="orbit camera",
            source_post_id=post_id,
            timeout=300
        )
        job_ids.append(job_id)

    results = await pool.wait()

    for job_id in job_ids:
        result = results[job_id]
        if result.success:
            video_id = result.data["video_id"]
```

**Dynamic Method Dispatch**: `task_type` maps directly to client method names.
Add new methods to `GrokClient`, they're automatically available in the pool.

```python
async with BrowserWorkerPool(
    num_workers=3,
    max_retries=5,
    fail_condition=lambda r: r.get("moderated", False),
    state_file="progress.json"
) as pool:
    job_id = await pool.submit("create_video", ...)
```

## Error Handling

```python
from grok_web import GrokAuthError, GrokAPIError, GrokNotFoundError

try:
    result = await client.create_video(post_id)
except GrokAuthError:
    print("Authentication failed - ensure Chrome is running")
except GrokNotFoundError:
    print("Post not found")
except GrokAPIError as e:
    print(f"API error: {e}")
```

## License

MIT
