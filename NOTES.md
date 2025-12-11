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
| 1 | `list_posts(include_raw_data=False)` | POST `/rest/media/post/list` | ✅ |
| 2 | `get_post_details()` | POST `/rest/media/post/get` | ✅ |
| 3 | `get_asset_file_size()` | HEAD `assets.grok.com/...` | ✅ |
| 4 | `validate_auth()` | (uses list_posts) | ✅ |
| 5 | `match_local_video()` | (uses get_post_details + HEAD) | ✅ |
| 6 | `like_post()` | POST `/rest/media/post/like` | ✅ |
| 7 | `unlike_post()` | POST `/rest/media/post/unlike` | ✅ |
| 8 | `create_video_from_image()` | POST `/rest/app-chat/conversations/new` | ✅ |

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
    "prompt": "Grok simplified prompt (NOT original input!)",
    "originalPrompt": "same as prompt (also simplified)",
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
        "mode": "normal | custom",
        "prompt": "",
        "originalPrompt": "user's img2vid edit prompt (only if mode=custom)",
        "thumbnailImageUrl": "https://assets.grok.com/.../preview_image.jpg",
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
| `userInteractionStatus` | ✅ has `likeStatus` | ❌ NOT FOUND | Only on parent! |

### Prompt Fields (重要！)

**Grok 会压缩/简化用户的 prompt！** API 返回的 `prompt` 已经是处理后的版本。

| 层级 | 字段 | 内容 |
|------|------|------|
| **Parent** | `prompt` | txt2img 提示词（**已被 Grok 简化**，非原始输入） |
| **Parent** | `originalPrompt` | 同上，两者相同 |
| **Child** | `prompt` | 始终为空 `""` |
| **Child** | `originalPrompt` | img2vid 编辑提示词（仅 `mode=custom` 时有值） |

**结论**：无法通过 API 找回用户原始输入的完整 prompt。

### Available Actions

| Action | Parent | Child | 说明 |
|--------|--------|-------|------|
| `MEDIA_POST_ACTION_TYPE_LIKE` | ✅ | ✅ | 收藏 |
| `MEDIA_POST_ACTION_TYPE_SHARE` | ✅ | ✅ | 分享 |
| `MEDIA_POST_ACTION_TYPE_DOWNLOAD` | ✅ | ✅ | 下载 |
| `MEDIA_POST_ACTION_TYPE_DELETE` | ✅ | ✅ | 删除 |
| `MEDIA_POST_ACTION_TYPE_UPSCALE_VIDEO` | ❌ | ✅ | 仅视频有 |

### Child Like 的坑！

**Child 没有独立的 like 状态**，点 Child 的 LIKE/UNLIKE 实际操作的是 Parent！

| 操作 | 效果 |
|------|------|
| 点 Child 的 Unlike | **整个 post（含所有 children）从 favorites 消失！** |
| 只想删除某个视频 | 应该用 **DELETE**，不是 UNLIKE |

### Thumbnail URLs

可用于 UI 预览，省带宽：

```
Parent: https://imagine-public.x.ai/cdn-cgi/image/width=500,fit=scale-down,format=auto/...
Child:  https://assets.grok.com/.../preview_image.jpg
```

### HD vs Normal Video

```
mediaUrl:   .../generated_video.mp4     (~1.5 MB)
hdMediaUrl: .../generated_video_hd.mp4  (~3.1 MB, 2x larger)
```

- `hdMediaUrl` only exists after calling upscale
- **Both URLs coexist after upscale** - can download either!
- **UI hides this**: After upscale, web UI only shows HD download button
- **API reveals both**: Use `mediaUrl` for quick preview, `hdMediaUrl` for final output

**MCTS optimization**: Download low-res for evaluation, HD only for final selected videos.

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

## x-statsig-id (Style Control)

**重要发现**：`x-statsig-id` 影响视频生成风格！

### 结构

| 属性 | 值 |
|------|-----|
| 原始长度 | 94 字符 (Base64) |
| 解码后 | 70 bytes 二进制 |
| 内容 | 随机/加密数据，无明显结构 |
| 服务端验证 | 几乎无（任意 70 bytes 都能用）|

### 行为

| 场景 | 效果 |
|------|------|
| 相同 x-statsig-id | 视频风格高度相似 (~99%)，如相同的 camera 运动、人物动作模式 |
| 不同 x-statsig-id | 视频风格可能不同 |
| 随机生成的 ID | 服务器接受，用于探索新风格 |

### API 支持

```python
# 探索新风格（不指定，自动生成随机 ID）
result = client.create_video_from_image(image_url, parent_id)
print(result.statsig_id)  # 返回使用的 ID

# 复现风格（指定已知的 ID）
result = client.create_video_from_image(
    image_url, parent_id,
    statsig_id="ztbNHMzMR1nE1m/ZUYn3/..."  # 之前成功的 ID
)
```

### MCTS Pipeline 应用

1. **风格探索**：不指定 statsig_id，生成多样化视频
2. **风格复现**：保存成功视频的 statsig_id，用于 fine-tuning
3. **风格聚类**：收集 statsig_id 与视频风格的对应关系

### 注意事项

- x-statsig-id **不影响 moderation**，moderation 是基于内容的
- 浏览器每次请求生成新的 ID（不是 Statsig 标准的 StableID）
- 生成方式：`base64.b64encode(os.urandom(70)).decode().rstrip('=')`

---

## Cookie & Auth Notes

- `cf_clearance` binds to browser TLS fingerprint
- `GrokClient` (curl_cffi) works on macOS/Linux
- `PlaywrightClient` needed if curl_cffi gets 403
- Cookies expire periodically, need manual refresh

---

## TODO

- [ ] Implement `upscale_video()` API
- [x] Test `like_post()`, `unlike_post()` - ✅ 2025-12-11
- [x] Test `create_video_from_image()` - ✅ 2025-12-11
- [x] Add `statsig_id` parameter for style control - ✅ 2025-12-11
- [ ] Add chat stream parser for real-time status
- [ ] Document rate limits
