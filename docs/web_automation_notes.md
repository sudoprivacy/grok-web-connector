# Grok Imagine Web Automation Notes

**Purpose**: Document findings about Grok Imagine web interface behavior for automation development.

**Status**: Work in progress - will be deleted once fully integrated into code/docs.

---

## 4 Generation Pipelines

| Pipeline | Input | Output | Has Post URL | Notes |
|----------|-------|--------|--------------|-------|
| **txt2img** | text prompt + image checkbox | Gallery (infinite scroll) | ✅ Each image has post URL | Starting point for long videos |
| **img2vid** | Grok-generated image | Video post | ✅ Yes | Same post as txt2img + children |
| **txt2vid** | text prompt + video checkbox | Video post | ✅ Yes | Parent is video |
| **upload2vid** | Upload external image | Video post | ✅ Yes | - |

### Key Insight: txt2img → img2vid Lifecycle

```
txt2img generates image → creates post (parent_id = {image-id})
                            ↓
                          children = []
                            ↓
User generates video on this image → children.push(video)
                            ↓
                          Now it's img2vid
```

**Detection logic**:
- `txt2img`: MEDIA_POST_TYPE_IMAGE + prompt + children.length == 0
- `img2vid`: MEDIA_POST_TYPE_IMAGE + prompt + children.length > 0

---

## Action 1: Scroll Down in txt2img Gallery

**Date**: 2025-12-09
**Raw data**: `C:\Users\songym\cursor-projects\grok-downloaded-video-local-organizer\grok-curl-rawl.tmp`

### Observed Behavior

- **Gallery UI**: Infinite scroll, 4 images per row (depends on browser width)
- **Loading mechanism**: WebSocket (`location: "imagine-websocket"`)
- **No traditional REST API** for loading more images

### Captured Events (Mixpanel Analytics)

```javascript
// 1. Load more images triggered
event: "image_feed_load_more_images"
location: "imagine-websocket"

// 2. New image generation started (multiple times)
event: "image_feed_image_generation_started"
location: "imagine-websocket"
job_id: "bb97c33f-41ff-42ff-8f65-dc642e46ab19"
request_id: "bbebd7ce-bcfb-4b35-9822-2a57f975bc22"
image_id: ""
```

### Technical Details

- **WebSocket connection**: Used for real-time image pushing
- **Chrome DevTools limitation**: "Copy as cURL" cannot capture WebSocket messages
- **No REST endpoints** observed in this action

### Implications for Automation

- Cannot use simple REST API calls to trigger scroll/load more
- Need to:
  1. Either use Playwright to scroll and wait for images to appear
  2. Or reverse-engineer WebSocket protocol (complex, fragile)
- Recommendation: Use Playwright for UI interaction

---

## Action 2: Send txt2img Prompt

**Date**: 2025-12-09
**Raw data**: Same file as Action 1 (combined capture)

### Observed Behavior

**Event**: `image_feed_text_sent`
- **Location**: `imagine-websocket`
- **Mechanism**: WebSocket (no REST API)

### Image Generation Triggered

**Event**: `image_feed_image_generation_started` (fired multiple times, one per image)

**Properties**:
```javascript
{
  job_id: "382af782-5751-44ae-a485-82de3b84b498",  // Unique per image
  request_id: "c4d31a17-1f3f-4120-9424-b345474ba3cf",  // Shared across batch
  image_id: "",  // Empty initially
  location: "imagine-websocket"
}
```

### Key Findings

- **No REST API** for sending prompts - uses WebSocket
- **Batch generation**: Multiple images generated per prompt (4-8 images per batch based on settings)
- **Job tracking**: Each image has unique `job_id`, batch shares `request_id`

### Implications for Automation

- Cannot trigger generation via simple HTTP request
- Need to:
  1. Use Playwright to type prompt and click generate
  2. Or reverse-engineer WebSocket protocol (requires session setup, auth, etc.)
- Recommendation: Playwright automation

---

## Action 3: Click into Single Image

**Date**: 2025-12-09
**Raw data**: Same file (combined capture)

### Observed Behavior

**Event**: `image_feed_image_selected`
- **Location**: `imagine-grid-card`
- **Properties**:
  ```javascript
  {
    source: "unknown",
    model_name: "imagine_x_1"
  }
  ```

### Clicked Post URLs
1. `https://grok.com/imagine/post/e6ca853e-dfc6-4678-a0f0-6ebbf0415932-5`
2. `https://grok.com/imagine/post/c9f3735d-acaf-4236-8021-cc324ea45d16-2`

### Navigation Flow (from Referer headers)
```
/imagine/favorites → /imagine/post/{id}-{suffix} → /imagine → /imagine/post/{id2}-{suffix}
```

### ⚠️ CRITICAL FINDING: No `/rest/media/post/get` API Call

**Expectation**: Clicking an image should call `/rest/media/post/get` to fetch details.

**Reality**: No such API call detected!

**Possible explanations**:
1. **Post details already in `/rest/media/post/list` response** (most likely)
   - List API may return full post objects with children
   - UI caches data client-side
2. **WebSocket delivery**: Post details pushed via WebSocket
3. **Initial page load**: Data embedded in HTML (not captured by cURL)
4. **Later API call**: May happen after page interaction (not yet captured)

**Next step needed**: Examine actual `/rest/media/post/list` response to see if it includes full post details

---

## Action 4: Video Generation from Image (img2vid) ✅

**Date**: 2025-12-10
**Image**: `https://imagine-public.x.ai/imagine-public/images/64d57dc0-4131-41a4-ac3a-67acc1869021.png`
**Parent Post ID**: `64d57dc0-4131-41a4-ac3a-67acc1869021`

### Discovery: Video Generation via Chat API

**CRITICAL FINDING**: Video generation is NOT a dedicated REST API, it's triggered through the **Grok chat interface** using a special tool.

### API Sequence

#### 1. Create Post from Image (Optional?)

**Endpoint**: `POST /rest/media/post/create`

```json
{
  "mediaType": "MEDIA_POST_TYPE_IMAGE",
  "mediaUrl": "https://imagine-public.x.ai/imagine-public/images/64d57dc0-4131-41a4-ac3a-67acc1869021.png"
}
```

**Purpose**: Creates a post entry for the image (may be auto-created, need to verify)

#### 2. Trigger Video Generation via Chat

**Endpoint**: `POST /rest/app-chat/conversations/new`

```json
{
  "temporary": true,
  "modelName": "grok-3",
  "message": "https://imagine-public.x.ai/imagine-public/images/64d57dc0-4131-41a4-ac3a-67acc1869021.png  --mode=normal",
  "toolOverrides": {
    "videoGen": true
  },
  "responseMetadata": {
    "experiments": [],
    "modelConfigOverride": {
      "modelMap": {
        "videoGenModelConfig": {
          "parentPostId": "64d57dc0-4131-41a4-ac3a-67acc1869021",
          "aspectRatio": "2:3",
          "videoLength": 6
        }
      }
    }
  }
}
```

**Key Parameters**:
- `message`: Image URL with optional `--mode=normal` flag
- `toolOverrides.videoGen`: Enable video generation tool
- `videoGenModelConfig`:
  - `parentPostId`: Image post UUID (links video to parent)
  - `aspectRatio`: `"2:3"`, `"16:9"`, etc.
  - `videoLength`: Duration in seconds (typically 6 or 15)

#### 3. Like Post (Save to Favorites)

**Endpoint**: `POST /rest/media/post/like`

```json
{
  "id": "64d57dc0-4131-41a4-ac3a-67acc1869021"
}
```

**Purpose**:
- Adds post to favorites (`/imagine/favorites`)
- **This is the ONLY way to keep posts accessible long-term**
- Unlike removes post from favorites (equivalent to deletion from user's view)

### Key Findings

1. **Video generation uses chat API**:
   - Not a dedicated `/imagine` API
   - Uses Grok-3 chat model with `videoGen` tool override
   - Message format: image URL + optional mode flags

2. **Parent-child relationship**:
   - Image post ID becomes `parentPostId` for videos
   - Videos are stored as `childPosts[]` in parent post
   - Same post ID throughout lifecycle (txt2img → img2vid)

3. **Video parameters**:
   - `aspectRatio`: Controls video dimensions
   - `videoLength`: 6 or 15 seconds typical
   - `mode`: Can specify generation mode flags

4. **Favorites = Like**:
   - Like/Unlike is the persistence mechanism
   - No separate "save" or "delete" API
   - Unlike = remove from all lists (not recoverable from UI)

### Architecture Implication

**For MCTS Implementation**:
- Must use chat API endpoint, not imagine endpoint
- Need to construct proper chat message format
- Monitor chat response stream for video generation completion
- Like all valuable posts to keep them in favorites
- Track parentPostId to maintain tree structure

---

---

## API Endpoints Discovered

### 📖 READ APIs (Already in connector)

#### `/rest/media/post/list` - List User's Posts

**Method**: POST
**Content-Type**: application/json

**Request Payloads Observed**:
```json
// Initial load (no cursor)
{"limit":40,"filter":{"source":"MEDIA_POST_SOURCE_LIKED"}}

// Pagination (with cursor)
{"limit":40,"cursor":"1765195806652","filter":{"source":"MEDIA_POST_SOURCE_LIKED"}}
```

**Pagination**:
- `cursor`: Timestamp in milliseconds (Unix epoch)
- Used for infinite scroll - each page returns posts older than cursor

**Filters**:
- `source: "MEDIA_POST_SOURCE_LIKED"` - Only liked/favorited posts
- `source: null` or omitted - All user's posts (assumption)

**Status**: ✅ Implemented in grok-web-connector

---

#### `/rest/media/post/get` - Get Single Post Details

**Method**: POST
**Content-Type**: application/json

**Request Payload**:
```json
{"id": "post-uuid-here"}
```

**Returns**: Full post metadata + childPosts array

**Status**: ✅ Implemented in grok-web-connector (but may not be called by UI - data might be in list response)

---

### ✏️ WRITE APIs (NEW - Not yet in connector)

#### `/rest/media/post/create` - Create Post from Image

**Method**: POST
**Content-Type**: application/json

**Request Payload**:
```json
{
  "mediaType": "MEDIA_POST_TYPE_IMAGE",
  "mediaUrl": "https://imagine-public.x.ai/imagine-public/images/{uuid}.png"
}
```

**Purpose**: Creates a post entry for an image
**Returns**: Post ID (assumption)
**Status**: ⚠️ Not implemented - Need to verify if needed (may be auto-created)

---

#### `/rest/app-chat/conversations/new` - Generate Video (via Chat)

**Method**: POST
**Content-Type**: application/json

**Request Payload** (img2vid):
```json
{
  "temporary": true,
  "modelName": "grok-3",
  "message": "https://imagine-public.x.ai/imagine-public/images/{uuid}.png  --mode=normal",
  "toolOverrides": {
    "videoGen": true
  },
  "responseMetadata": {
    "experiments": [],
    "modelConfigOverride": {
      "modelMap": {
        "videoGenModelConfig": {
          "parentPostId": "parent-post-uuid",
          "aspectRatio": "2:3",
          "videoLength": 6
        }
      }
    }
  }
}
```

**Parameters**:
- `parentPostId`: Image post UUID
- `aspectRatio`: `"2:3"`, `"16:9"`, etc.
- `videoLength`: 6 or 15 seconds

**Returns**: Chat stream response with video generation status
**Status**: ❌ Not implemented - Critical for MCTS

---

#### `/rest/media/post/like` - Like Post (Save to Favorites)

**Method**: POST
**Content-Type**: application/json

**Request Payload**:
```json
{"id": "post-uuid"}
```

**Purpose**: Adds post to favorites, prevents deletion
**Inverse**: `/rest/media/post/unlike` (assumption - not captured)
**Status**: ❌ Not implemented - Critical for persistence

---

## Summary & Next Steps

### ✅ What We Know (Complete Discovery)

1. **txt2img generation**: WebSocket-based (`image_feed_text_sent`), no REST API
2. **Image gallery**: WebSocket for infinite scroll
3. **Clicking images**: No `/rest/media/post/get` called (data in list response)
4. **Video generation (img2vid)**: Uses chat API `/rest/app-chat/conversations/new` with `videoGen` tool
5. **Persistence**: Like/Unlike via `/rest/media/post/like` (only way to keep posts)
6. **Pagination**: Timestamp-based cursor for `/rest/media/post/list`

### 🎯 Architectural Clarity

**Two-Layer System**:

1. **WebSocket Layer** (Ephemeral UI):
   - txt2img generation (`imagine-websocket`)
   - Gallery infinite scroll
   - Real-time image streaming
   - **Cannot be easily automated via Python**

2. **REST Layer** (Persistent Data):
   - Post retrieval (`/rest/media/post/list`, `/rest/media/post/get`)
   - Video generation (`/rest/app-chat/conversations/new`)
   - Post persistence (`/rest/media/post/like`)
   - **Can be automated**

### ⚠️ Remaining Questions

1. **Response structure** of `/rest/media/post/list`:
   - Does it include full children?
   - Or need separate `/rest/media/post/get`?

2. **Chat response stream**:
   - How to parse chat SSE/WebSocket for video completion?
   - What events indicate success vs failure?

3. **Post creation timing**:
   - Is `/rest/media/post/create` required?
   - Or auto-created during txt2img?

### 🚀 Recommended Implementation Strategy

**Hybrid Approach** (Playwright + REST API):

#### For txt2img (Text → Image):
- **Use Playwright** for WebSocket automation:
  - Navigate to `/imagine`
  - Type prompt in textarea
  - Click generate button
  - Monitor DOM for image appearance (or use CDP to listen to WebSocket)
  - Extract image URLs from gallery cards
  - Like posts to save them

#### For img2vid (Image → Video):
- **Use REST API directly** (no Playwright needed!):
  - Call `/rest/app-chat/conversations/new` with video generation payload
  - Parse chat response stream for completion
  - Call `/rest/media/post/get` to verify video in childPosts
  - Like post to persist
  - Download video from assets URL

#### For Post Management:
- **Use REST API** (already in connector):
  - `list_posts()` - retrieve all posts
  - `get_post_details()` - get full metadata + children
  - `get_asset_file_size()` - check file size before download

#### New APIs to Add to connector:
1. ❌ `create_video_from_image(image_url, parent_post_id, aspect_ratio, video_length)` - img2vid via chat API
2. ❌ `like_post(post_id)` - save to favorites
3. ❌ `unlike_post(post_id)` - remove from favorites (assumption)
4. ⚠️ `create_post_from_image(image_url)` - may not be needed

**Key Insight**: Only txt2img requires Playwright. img2vid can be pure REST API!

### Critical Events to Monitor (for automation)

**Mixpanel WebSocket Events** - Must track to distinguish success vs failure:

1. **Generation Success Path**:
   - `image_feed_text_sent` → Prompt submitted
   - `image_feed_image_generation_started` → Processing begins
   - `image_feed_image_generated` → Image ready
   - **No moderation event** = Success ✅

2. **Generation Failure Path** (Moderated):
   - `image_feed_text_sent` → Prompt submitted
   - `image_feed_image_generation_started` → Processing begins
   - `image_feed_image_generated` → Image generated
   - `image_feed_image_moderated` ⚠️ → **Content rejected**
   - Image will NOT be available in gallery

3. **Implementation Note**:
   - Cannot use WebSocket directly from Python easily
   - Use Playwright CDP (Chrome DevTools Protocol) to listen to browser events
   - Or poll `/rest/media/post/list` to check if new post appeared
   - Must handle moderation gracefully in MCTS scoring (mark as failed branch)

---

## Notes

- All requests will be Windows cURL format (using `^` for line continuation)
- Focus on `grok.com/rest/*` and `grok.com/api/*` endpoints
- Ignore analytics/tracking requests (mixpanel, google-analytics)
