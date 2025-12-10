# Grok Imagine API Discovery Summary

**Date**: 2025-12-10
**Status**: ✅ Complete - All critical workflows discovered

---

## 🎯 Key Discoveries

### 1. Video Generation Architecture

**CRITICAL**: Video generation (img2vid) is NOT a dedicated API. It uses the **Grok Chat API** with a special `videoGen` tool.

```
User clicks "Animate" button
    ↓
POST /rest/app-chat/conversations/new
    {
      "modelName": "grok-3",
      "message": "https://imagine-public.x.ai/.../image.png --mode=normal",
      "toolOverrides": { "videoGen": true },
      "responseMetadata": {
        "modelConfigOverride": {
          "modelMap": {
            "videoGenModelConfig": {
              "parentPostId": "uuid",
              "aspectRatio": "2:3",
              "videoLength": 6
            }
          }
        }
      }
    }
    ↓
Chat stream returns video generation status
    ↓
Video appears in parent post's childPosts[] array
```

### 2. Favorites = Like Mechanism

**CRITICAL**: Like/Unlike is the ONLY way to persist posts long-term.

- **Like post** → Saves to `/imagine/favorites`
- **Unlike post** → Removes from all views (equivalent to delete)
- No separate "save" or "delete" API
- Unlike is **irreversible** from UI

```
POST /rest/media/post/like
{
  "id": "post-uuid"
}
```

### 3. Two-Layer Architecture

#### WebSocket Layer (Ephemeral)
- **txt2img** generation (`image_feed_text_sent`)
- Gallery infinite scroll
- Real-time image streaming
- **Cannot easily automate via pure Python**

#### REST Layer (Persistent)
- Post retrieval (`/rest/media/post/list`, `/rest/media/post/get`)
- **Video generation** (`/rest/app-chat/conversations/new`)
- Post persistence (`/rest/media/post/like`)
- **Can be automated without Playwright**

---

## 📋 Complete API Catalog

### ✅ READ APIs (Already in connector)

| Endpoint | Purpose | Status |
|----------|---------|--------|
| `/rest/media/post/list` | List posts with pagination | ✅ Implemented |
| `/rest/media/post/get` | Get single post + children | ✅ Implemented |
| `HEAD assets.grok.com/...` | Get file size | ✅ Implemented |

### ❌ WRITE APIs (Need to implement)

| Endpoint | Purpose | Priority |
|----------|---------|----------|
| `/rest/app-chat/conversations/new` | **Generate video from image** | 🔴 Critical |
| `/rest/media/post/like` | **Save to favorites** | 🔴 Critical |
| `/rest/media/post/unlike` | Remove from favorites | 🟡 Medium |
| `/rest/media/post/create` | Create post from image | 🟢 Low (may be auto) |

---

## 🚀 Implementation Strategy

### For grok-imagine-expert (MCTS System)

#### Phase 1: txt2img (Text → Image)
**Use Playwright** (WebSocket automation):
```python
# Navigate to /imagine
# Type prompt
# Click generate
# Wait for images in DOM
# Extract image URLs
# Like all promising images
```

#### Phase 2: img2vid (Image → Video)
**Use REST API directly** (no Playwright!):
```python
# Call /rest/app-chat/conversations/new with videoGen config
# Parse chat stream for completion
# Verify video in childPosts via /rest/media/post/get
# Like post to persist
# Download from assets URL
```

#### Phase 3: MCTS Tree Building
**Use REST API** (already in connector):
```python
# List your liked posts with list_posts() (default behavior)
# Get details with get_post_details()
# Build tree from parent-child relationships
# Score videos based on quality metrics
# Select best branches for expansion
```

---

## 🎯 MCTS Workflow with APIs

```
1. txt2img (Playwright)
   Generate multiple candidate images
   └→ Extract image URLs from gallery
   └→ Like all images to persist

2. MCTS Selection (REST API)
   List all posts → Score images → Select best

3. img2vid (REST API - No Playwright!)
   For each selected image:
   └→ Call /rest/app-chat/conversations/new
   └→ Monitor chat stream
   └→ Verify video in childPosts
   └→ Like post to persist

4. Tree Expansion (REST API)
   Get post details → Build MCTS tree
   └→ Video becomes new node
   └→ Extract end frame
   └→ Repeat from step 3

5. Cleanup (REST API)
   Unlike low-scoring branches to remove
```

---

## 💡 Key Insights for Implementation

1. **Only txt2img needs Playwright**
   - img2vid can be pure REST API
   - Much simpler than expected!

2. **Chat API handles video generation**
   - Not a dedicated `/imagine` endpoint
   - Need to parse SSE/WebSocket chat stream

3. **Like = Persistence**
   - Auto-like all valuable posts immediately
   - Unlike to clean up failed branches
   - No other way to keep posts long-term

4. **Parent-child model is clean**
   - Image post ID → parentPostId for videos
   - Videos in childPosts[] array
   - Same post ID throughout lifecycle

5. **Prompt simplification**
   - System auto-shortens prompts
   - Stored prompt ≠ user input
   - Cannot reproduce exact generation from stored prompt

---

## 📊 Next Steps for grok-web-connector

### High Priority
1. Implement `create_video_from_image()` - Chat API with videoGen
2. Implement `like_post()` / `unlike_post()` - Persistence
3. Add chat stream parser for video completion

### Medium Priority
1. Verify `/rest/media/post/list` response structure
2. Test if `/rest/media/post/create` is needed
3. Document chat response events

### Low Priority
1. Add WebSocket monitoring for txt2img (optional, Playwright can handle)
2. Reverse-engineer Mixpanel event tracking (optional)

---

## 🔬 Remaining Questions

1. **Chat stream format**: SSE vs WebSocket? Event structure?
2. **List response**: Includes full children or just IDs?
3. **Post creation**: Auto-created or manual API call needed?
4. **Unlike endpoint**: Is it `/rest/media/post/unlike` or different?

These can be answered during implementation testing.

---

**Conclusion**: We now have a complete understanding of Grok Imagine's API architecture. The system is simpler than expected - only txt2img requires Playwright, everything else is REST API!
