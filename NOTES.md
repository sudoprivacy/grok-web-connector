# Grok Web Connector - Technical Notes

> Internal documentation for development. See README.md for usage.

---

## Architecture Overview

### Two-Layer System

| Layer | What | Automatable? |
|-------|------|--------------|
| **WebSocket** | txt2img generation, gallery scroll | ❌ Need Playwright |
| **REST API** | Post management, img2vid, like/unlike | ✅ Pure Python |

**Key insight**: Only `txt2img` requires browser automation. Everything else is REST API.

---

## API Endpoints

### Implemented (8 APIs)

| # | API | Endpoint | Tested |
|---|-----|----------|--------|
| 1 | `list_posts()` | POST `/rest/media/post/list` | ✅ |
| 2 | `get_post_details()` | POST `/rest/media/post/get` | ✅ |
| 3 | `get_asset_file_size()` | HEAD `assets.grok.com/...` | ✅ |
| 4 | `validate_auth()` | (uses list_posts) | ✅ |
| 5 | `match_local_video()` | (uses get_post_details + HEAD) | ✅ |
| 6 | `like_post()` | POST `/rest/media/post/like` | ❌ |
| 7 | `unlike_post()` | POST `/rest/media/post/unlike` | ❌ |
| 8 | `create_video_from_image()` | POST `/rest/app-chat/conversations/new` | ❌ |

### Not Yet Implemented

| API | Endpoint | Priority | Notes |
|-----|----------|----------|-------|
| `upscale_video()` | ? | High | Upgrade to HD, needed for final output |
| `edit_image()` | POST `/rest/app-chat/conversations/new` | Low | Uses `modelName: "imagine-image-edit"` |

---

## Post Metadata Structure

From `get_post_details()` response:

```json
{
  "post": {
    "id": "uuid",
    "userId": "user-uuid",
    "createTime": "2025-12-10T08:10:50.073380Z",
    "prompt": "original user prompt",
    "originalPrompt": "same as prompt",
    "mediaType": "MEDIA_POST_TYPE_IMAGE",
    "mediaUrl": "https://imagine-public.x.ai/.../image.png",
    "mimeType": "image/png",
    "resolution": { "width": 832, "height": 1248 },
    "modelName": "imagine_x_1",
    "thumbnailImageUrl": "https://imagine-public.x.ai/cdn-cgi/image/...",
    "userInteractionStatus": { "likeStatus": true },
    "availableActions": ["LIKE", "SHARE", "DOWNLOAD", "DELETE"],
    "childPosts": [
      {
        "id": "video-uuid",
        "mediaType": "MEDIA_POST_TYPE_VIDEO",
        "mediaUrl": "https://assets.grok.com/.../generated_video.mp4",
        "hdMediaUrl": "https://assets.grok.com/.../generated_video_hd.mp4",
        "resolution": { "width": 464, "height": 688 },
        "videoDuration": 6,
        "modelName": "imagine_xdit_1",
        "mode": "normal",
        "availableActions": ["LIKE", "SHARE", "DOWNLOAD", "DELETE", "UPSCALE_VIDEO"]
      }
    ]
  }
}
```

### Key Fields

| Field | Parent (Image) | Child (Video) | Notes |
|-------|----------------|---------------|-------|
| `modelName` | `imagine_x_1` | `imagine_xdit_1` | Different models for img vs vid |
| `resolution` | 832×1248 | 464×688 | Video is lower res until upscaled |
| `mediaUrl` | imagine-public.x.ai | assets.grok.com | Different CDN domains |
| `hdMediaUrl` | N/A | assets.grok.com | Only after upscale |
| `videoDuration` | N/A | 6 | Seconds |
| `availableActions` | no UPSCALE | has UPSCALE_VIDEO | Can detect if upscale available |
| `userInteractionStatus.likeStatus` | true/false | - | Only on parent |

### HD vs Normal Video

```
mediaUrl:   .../generated_video.mp4     (~1.5 MB)
hdMediaUrl: .../generated_video_hd.mp4  (~3.1 MB, 2x larger)
```

- `hdMediaUrl` only exists after calling upscale
- Both URLs coexist after upscale (can download either)

---

## Video Generation (img2vid)

Uses **Chat API** with `videoGen` tool override:

```json
POST /rest/app-chat/conversations/new
{
  "temporary": true,
  "modelName": "grok-3",
  "message": "https://imagine-public.x.ai/.../image.png  --mode=normal",
  "toolOverrides": { "videoGen": true },
  "responseMetadata": {
    "modelConfigOverride": {
      "modelMap": {
        "videoGenModelConfig": {
          "parentPostId": "image-post-uuid",
          "aspectRatio": "2:3",
          "videoLength": 6
        }
      }
    }
  }
}
```

**Parameters**:
- `parentPostId`: Links video to parent image post
- `aspectRatio`: `"2:3"`, `"16:9"`, etc. (auto from image)
- `videoLength`: 6 or 15 seconds
- `mode`: `"normal"` (default), others add extra prompts

**Flow**:
1. Call chat API → returns immediately
2. Poll `get_post_details(parentPostId)` until `childPosts.length > 0`
3. Call `like_post()` to persist
4. Download from `mediaUrl` or `hdMediaUrl`

---

## Favorites = Like System

**CRITICAL**: Like/Unlike is the ONLY persistence mechanism.

| Action | Effect |
|--------|--------|
| `like_post(id)` | Saves to `/imagine/favorites`, persists indefinitely |
| `unlike_post(id)` | Removes from all views, **irreversible from UI** |

- `list_posts()` default returns liked posts only
- `list_posts(source=None)` returns all public posts (not just yours)
- Posts must be liked to appear in favorites
- `userInteractionStatus.likeStatus` indicates current state

---

## Image Edit API (未实现)

```json
POST /rest/app-chat/conversations/new
{
  "modelName": "imagine-image-edit",
  "message": "edit prompt here",
  "enableImageGeneration": true,
  "imageGenerationCount": 2,
  "enableImageStreaming": true,
  "responseMetadata": {
    "modelConfigOverride": {
      "modelMap": {
        "imageEditModelConfig": {
          "imageReference": "https://imagine-public.x.ai/.../image.png"
        },
        "imageEditModel": "imagine"
      }
    }
  }
}
```

- Generates 2 edited versions
- Progressive streaming (blurry → clear)
- Often gets moderated

---

## MCTS Workflow

```
1. txt2img (Playwright)
   └→ Generate images from prompt
   └→ Like all promising candidates

2. Selection (REST API)
   └→ list_posts() to get candidates
   └→ Score and rank

3. img2vid (REST API)
   └→ create_video_from_image() for selected
   └→ Poll until complete
   └→ Like to persist

4. Expansion (REST API)
   └→ Extract end frame from video
   └→ Use as new image for next iteration
   └→ Repeat step 3

5. Cleanup (REST API)
   └→ unlike_post() for low-scoring branches
```

---

## Cookie & Auth Notes

- `cf_clearance` binds to browser TLS fingerprint
- `GrokClient` (curl_cffi) works on macOS/Linux
- `PlaywrightClient` needed if curl_cffi gets 403
- Cookies expire periodically, need manual refresh

---

## TODO

- [ ] Implement `upscale_video()` API
- [ ] Test `like_post()`, `unlike_post()`, `create_video_from_image()`
- [ ] Add chat stream parser for real-time status
- [ ] Document rate limits
