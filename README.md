# Grok Web Connector

Python client library for interacting with Grok Imagine web API.

## Features

- Cookie-based authentication (no password required)
- Fetch post details by UUID
- List user's posts
- Type-safe with Pydantic models
- Shared library for multiple projects

## Installation

### Development Mode (Editable Install)

```bash
cd /Users/songym/cursor-projects/grok-web-connector
pip install -e .
```

This allows you to edit the code and see changes immediately in all projects using it.

### From Another Project

```bash
pip install -e /Users/songym/cursor-projects/grok-web-connector
```

## Usage

### 1. Extract Cookies from Browser

1. Open https://grok.com in your browser
2. Open Developer Tools (F12)
3. Go to Application → Cookies → https://grok.com
4. Copy the following cookie values:
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

**Important**: Add `~/.grok-config.json` to your `.gitignore`!

### 3. Use the Client

```python
from grok_web import GrokClient

# Initialize client (automatically loads from ~/.grok-config.json)
client = GrokClient()

# Get post by UUID
post = client.get_post("0c5c5864-fadb-440b-a52b-e441dab973d3")
print(f"Post URL: {post.url}")
print(f"Video URL: {post.video_url}")

# List your posts
posts = client.list_posts(limit=10)
for post in posts:
    print(f"{post.id}: {post.prompt}")
```

## API Reference

### `GrokClient`

#### Methods

- `get_post(post_id: str) -> GrokPost` - Get post details by UUID
- `list_posts(limit: int = 40) -> list[GrokPost]` - List user's posts
- `get_video_url_from_filename(filename: str) -> str` - Construct Grok URL from video filename

### `GrokPost` (Pydantic Model)

- `id: str` - Post UUID
- `url: str` - Web URL (https://grok.com/imagine/post/{id})
- `prompt: str | None` - Generation prompt
- `video_url: str | None` - Direct video download URL
- `created_at: datetime | None` - Creation timestamp

## Grok Imagine Generation Modes

Grok Imagine supports three distinct video generation modes, each with different metadata structures.

### Mode Comparison Table

| Field | Grok-Image→Video | Text-to-Video | Upload-Image→Video |
|-------|------------------|---------------|---------------------|
| **Parent Post** |
| `mediaType` | `MEDIA_POST_TYPE_IMAGE` | `MEDIA_POST_TYPE_VIDEO` | `MEDIA_POST_TYPE_IMAGE` |
| `prompt` | ✅ (generation prompt) | ❌ | ❌ |
| `originalPrompt` | ❌ | ✅ (video prompt) | ❌ |
| `modelName` | `imagine_x_1` | `imagine_h_1` | `imagine_h_1` |
| `mediaUrl` | ✅ (generated image) | ✅ (video) | ✅ (uploaded image) |
| `hdMediaUrl` | ✅ | ✅ | ✅ |
| `thumbnailUrl` | ✅ | ✅ | ✅ |
| `hasChildPosts` | `true` | `true` | `true` |
| **Child Posts (Videos)** |
| `mode` | `custom` | `text` | `custom` |
| `mediaType` | `MEDIA_POST_TYPE_VIDEO` | `MEDIA_POST_TYPE_VIDEO` | `MEDIA_POST_TYPE_VIDEO` |
| `originalPrompt` | ✅ (video edit prompt) | ✅ (same as parent) | ✅ (video prompt) |
| `resolution` | `{width, height}` | `{width, height}` | `{width, height}` |
| `duration` | ✅ (milliseconds) | ✅ (milliseconds) | ✅ (milliseconds) |

### Mode 1: Grok-Image→Video

Workflow: Text prompt → Generated image → Animate to video

**Example URL**: `https://grok.com/imagine/post/0c5c5864-fadb-440b-a52b-e441dab973d3`

**Parent Post Fields**:
```json
{
  "mediaType": "MEDIA_POST_TYPE_IMAGE",
  "prompt": "A woman with long flowing hair...",
  "modelName": "imagine_x_1",
  "mediaUrl": "https://assets.grok.com/users/.../image.png",
  "hdMediaUrl": "https://assets.grok.com/users/.../image_hd.png",
  "thumbnailUrl": "https://assets.grok.com/users/.../thumbnail.webp",
  "hasChildPosts": true,
  "childPosts": [...]
}
```

**Child Post (Video) Fields**:
```json
{
  "mediaType": "MEDIA_POST_TYPE_VIDEO",
  "mode": "custom",
  "originalPrompt": "Gentle wind blowing through hair...",
  "mediaUrl": "https://assets.grok.com/users/.../video.mp4",
  "hdMediaUrl": "https://assets.grok.com/users/.../video_hd.mp4",
  "thumbnailUrl": "https://assets.grok.com/users/.../thumb.webp",
  "resolution": {"width": 848, "height": 480},
  "duration": 5000
}
```

### Mode 2: Text-to-Video

Workflow: Text prompt → Video directly (no intermediate image)

**Example URL**: `https://grok.com/imagine/post/2a57075a-f11a-4f9e-a828-5957caa55cd8`

**Parent Post Fields**:
```json
{
  "mediaType": "MEDIA_POST_TYPE_VIDEO",
  "mode": "text",
  "originalPrompt": "A serene mountain landscape...",
  "modelName": "imagine_h_1",
  "mediaUrl": "https://assets.grok.com/users/.../video.mp4",
  "hdMediaUrl": "https://assets.grok.com/users/.../video_hd.mp4",
  "thumbnailUrl": "https://assets.grok.com/users/.../thumb.webp",
  "resolution": {"width": 1280, "height": 720},
  "duration": 5000,
  "hasChildPosts": true,
  "childPosts": [...]
}
```

**Child Post (Video Variants) Fields**:
```json
{
  "mediaType": "MEDIA_POST_TYPE_VIDEO",
  "mode": "text",
  "originalPrompt": "A serene mountain landscape...",
  "resolution": {"width": 1280, "height": 720},
  "duration": 5000
}
```

### Mode 3: Upload-Image→Video

Workflow: Upload external image → Animate to video

**Example URL**: `https://grok.com/imagine/post/69fc3666-65aa-45a5-bc45-678d579b9182`

**Parent Post Fields**:
```json
{
  "mediaType": "MEDIA_POST_TYPE_IMAGE",
  "modelName": "imagine_h_1",
  "mediaUrl": "https://assets.grok.com/users/.../uploaded_image.png",
  "hdMediaUrl": "https://assets.grok.com/users/.../uploaded_image_hd.png",
  "thumbnailUrl": "https://assets.grok.com/users/.../thumb.webp",
  "hasChildPosts": true,
  "childPosts": [...]
}
```

**Note**: No `prompt` field for uploaded images (since the image wasn't generated by Grok).

**Child Post (Video) Fields**:
```json
{
  "mediaType": "MEDIA_POST_TYPE_VIDEO",
  "mode": "custom",
  "originalPrompt": "Camera slowly zooms in...",
  "modelName": "imagine_h_1",
  "mediaUrl": "https://assets.grok.com/users/.../video.mp4",
  "hdMediaUrl": "https://assets.grok.com/users/.../video_hd.mp4",
  "resolution": {"width": 848, "height": 480},
  "duration": 5000
}
```

### Key Insights

1. **UUID Mapping**: Downloaded video filename UUID = Parent post ID (not the video's own ID)
2. **Multiple Videos**: A single parent post can have multiple `childPosts` (video variants)
3. **Mode Detection**:
   - Check `mediaType` first: `IMAGE` vs `VIDEO`
   - For IMAGE parents, check if `prompt` exists (Grok-generated) or not (uploaded)
   - For VIDEO parents, it's Text-to-Video mode
4. **Model Names**:
   - `imagine_x_1`: Used for Grok-generated images
   - `imagine_h_1`: Used for videos and uploaded images

### Common Fields (All Modes)

**Always Available**:
- `id` (UUID)
- `userId`
- `createdAt` (ISO 8601 timestamp)
- `mediaType`
- `mediaUrl`
- `thumbnailUrl`

**Sometimes Available**:
- `hdMediaUrl` (high-definition version)
- `prompt` (only for Grok-generated images)
- `originalPrompt` (for videos)
- `resolution` (for videos: `{width, height}`)
- `duration` (for videos, in milliseconds)
- `modelName`
- `mode` (`custom` or `text`)

## Error Handling

```python
from grok_web import GrokClient, GrokAuthError, GrokAPIError

client = GrokClient()

try:
    post = client.get_post("invalid-uuid")
except GrokAuthError:
    print("Authentication failed. Check your cookies.")
except GrokAPIError as e:
    print(f"API error: {e}")
```

## Cookie Expiration

Cookies typically last several weeks to months. If authentication fails:
1. Re-extract cookies from browser
2. Update `~/.grok-config.json`
3. Retry

## License

MIT
