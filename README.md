# Grok Web Connector

Python client library for interacting with Grok Imagine web API.

## Features

- **4 Core APIs** for comprehensive Grok Imagine interaction
- Cookie-based authentication (no password required)
- Automatic generation mode detection (img2vid, txt2vid, upload2vid)
- Type-safe with Pydantic models

## Installation

```bash
# From GitHub
pip install git+https://github.com/elfenlieds7/grok-web-connector.git

# Development mode
cd /path/to/grok-web-connector
pip install -e .
```

## Quick Start

```python
from grok_web import GrokClient, GenerationMode

client = GrokClient()

# 1. List all posts
posts = client.list_posts(limit=10)
for p in posts:
    print(f"{p.id}: {p.mode.value} ({p.video_count} videos)")

# 2. Get details for a specific post
details = client.get_post_details("0c5c5864-fadb-440b-a52b-e441dab973d3")
print(f"Mode: {details.mode.value}")
print(f"Children: {details.video_count}")

# 3. Get video file size
for child in details.children:
    if child.hd_media_url:
        size = client.get_asset_file_size(child.hd_media_url)
        print(f"{child.id}: {size} bytes")
```

## Authentication Setup

### 1. Extract Cookies from Browser

1. Open https://grok.com in Chrome
2. Open DevTools (F12) → Application → Cookies → https://grok.com
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
    "cf_clearance": "REDACTED_CF_CLEARANCE..."
  }
}
```

**Important**: Add `~/.grok-config.json` to your global `.gitignore`!

---

## API Reference

### 1. `list_posts()` - Scan and Get Overview

```python
posts = client.list_posts(limit=40, source=None)
```

**Parameters:**
- `limit`: Maximum posts to return (default: 40)
- `source`: Filter type (`None` for all, `"MEDIA_POST_SOURCE_LIKED"` for liked)

**Returns:** `list[PostSummary]`
- `id`: Post UUID
- `mode`: Generation mode (see below)
- `prompt_preview`: First 100 chars of prompt
- `video_count`: Number of child videos
- `created_at`: Creation timestamp
- `web_url`: Computed web URL

### 2. `get_post_details()` - Explore Single Post

```python
details = client.get_post_details("0c5c5864-fadb-440b-a52b-e441dab973d3")
```

**Returns:** `PostDetails`
- All parent post metadata
- `children`: List of `ChildVideo` objects with full metadata
- `mode`: Detected generation mode
- `raw_data`: Original API response (for debugging)

### 3. `get_asset_file_size()` - Get Asset File Size

```python
size = client.get_asset_file_size(child.hd_media_url)
```

Get file size in bytes from `assets.grok.com` URL via HEAD request.

**Important**: This method handles the special headers required:
- `Referer: https://grok.com/`
- `Origin: https://grok.com`

Without these headers, requests return 403 Forbidden.

### 4. `validate_auth()` - Check Authentication

```python
if not client.validate_auth():
    print("Cookies expired! Please update ~/.grok-config.json")
```

---

## Generation Modes

Grok Imagine supports 3 video generation modes:

| Mode | Enum Value | Workflow |
|------|------------|----------|
| **Grok-Image→Video** | `img2vid` | Text prompt → Grok generates image → Animate to video |
| **Text-to-Video** | `txt2vid` | Text prompt → Video directly |
| **Upload-Image→Video** | `upload2vid` | Upload external image → Animate to video |

### Mode Detection Logic

```python
from grok_web import GenerationMode

# Detection is automatic based on metadata:
# - MEDIA_POST_TYPE_VIDEO + mode=text       → TEXT_TO_VIDEO
# - MEDIA_POST_TYPE_IMAGE + prompt exists   → GROK_IMAGE_TO_VIDEO
# - MEDIA_POST_TYPE_IMAGE + no prompt       → UPLOAD_IMAGE_TO_VIDEO
```

---

## API Endpoints (Internal)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/rest/media/post/list` | POST | List user's posts |
| `/rest/media/post/get` | POST | Get single post details |

### Request Format

```json
// list
{"limit": 40, "filter": {"source": "MEDIA_POST_SOURCE_LIKED"}}

// get
{"id": "uuid-here"}
```

### Response Structure

```json
{
  "post": {
    "id": "parent-uuid",
    "mediaType": "MEDIA_POST_TYPE_IMAGE",
    "prompt": "A woman with flowing hair...",
    "modelName": "imagine_x_1",
    "childPosts": [
      {
        "id": "child-uuid",
        "mediaType": "MEDIA_POST_TYPE_VIDEO",
        "originalPrompt": "Gentle wind blowing...",
        "hdMediaUrl": "https://assets.grok.com/...",
        "resolution": {"width": 848, "height": 480}
      }
    ]
  }
}
```

---

## Metadata by Generation Mode

### Mode 1: Grok-Image→Video (`img2vid`)

| Field | Parent | Child |
|-------|--------|-------|
| `mediaType` | `MEDIA_POST_TYPE_IMAGE` | `MEDIA_POST_TYPE_VIDEO` |
| `prompt` | Image generation prompt | - |
| `originalPrompt` | - | Video edit prompt |
| `modelName` | `imagine_x_1` | varies |
| `mode` | - | `custom` |

### Mode 2: Text-to-Video (`txt2vid`)

| Field | Parent | Child |
|-------|--------|-------|
| `mediaType` | `MEDIA_POST_TYPE_VIDEO` | `MEDIA_POST_TYPE_VIDEO` |
| `originalPrompt` | Video prompt | Same as parent |
| `modelName` | `imagine_h_1` | varies |
| `mode` | `text` | `text` |

### Mode 3: Upload-Image→Video (`upload2vid`)

| Field | Parent | Child |
|-------|--------|-------|
| `mediaType` | `MEDIA_POST_TYPE_IMAGE` | `MEDIA_POST_TYPE_VIDEO` |
| `prompt` | *(none - uploaded)* | - |
| `originalPrompt` | - | Video prompt |
| `modelName` | `imagine_h_1` | varies |
| `mode` | - | `custom` |

---

## Asset URL Pattern

```
https://assets.grok.com/users/{userId}/generated/{videoId}/generated_video.mp4
https://assets.grok.com/users/{userId}/generated/{videoId}/generated_video_hd.mp4
```

**Key insight**: The `{videoId}` in the URL is the **child video UUID**, not the parent post UUID.

### Accessing Assets

Assets require special headers:

```python
headers = {
    "Referer": "https://grok.com/",
    "Origin": "https://grok.com",
}
# Without these → 403 Forbidden
```

The `get_asset_file_size()` method handles this automatically.

---

## Local File to Web Matching

Downloaded videos follow this naming pattern:
```
grok-video-{PARENT_UUID}.mp4
grok-video-{PARENT_UUID} (1).mp4
grok-video-{PARENT_UUID} (2).mp4
```

**Matching strategy**:
1. Extract parent UUID from filename
2. Call `get_post_details(parent_uuid)` to get all children
3. For each child, call `get_asset_file_size()` to get web file size
4. Match local file size to web file size (exact match)

```python
import os
from grok_web import GrokClient

client = GrokClient()
local_size = os.path.getsize("/path/to/grok-video-xxx.mp4")

details = client.get_post_details("xxx")
for child in details.children:
    if child.hd_media_url:
        web_size = client.get_asset_file_size(child.hd_media_url)
        if web_size == local_size:
            print(f"Match! Local file → {child.id}")
```

---

## Error Handling

```python
from grok_web import GrokClient, GrokAuthError, GrokAPIError, GrokNotFoundError

client = GrokClient()

try:
    details = client.get_post_details("invalid-uuid")
except GrokAuthError:
    print("Cookies expired. Update ~/.grok-config.json")
except GrokNotFoundError:
    print("Post not found")
except GrokAPIError as e:
    print(f"API error: {e}")
```

---

## Cookie Expiration

Cookies typically last several weeks. When they expire:

1. Re-extract from browser (DevTools → Application → Cookies)
2. Update `~/.grok-config.json`
3. Verify with `client.validate_auth()`

---

## Data Models

### `PostSummary`
Lightweight post info for list operations.

### `PostDetails`
Full post info including all children and raw API response.

### `ChildVideo`
Video metadata with computed properties like `best_video_url`.

### `GenerationMode`
Enum: `GROK_IMAGE_TO_VIDEO`, `TEXT_TO_VIDEO`, `UPLOAD_IMAGE_TO_VIDEO`, `UNKNOWN`

---

## License

MIT
