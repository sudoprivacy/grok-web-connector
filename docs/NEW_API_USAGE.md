# New API Usage Examples

**Version**: 0.3.0+
**Date**: 2025-12-10

## New Write APIs

Three new write APIs have been added to enable full MCTS workflow automation:

1. `like_post()` - Save posts to favorites
2. `unlike_post()` - Remove posts from favorites
3. `create_video_from_image()` - Generate videos from images

---

## API 6: like_post() - Save to Favorites

**Purpose**: Save a post to `/imagine/favorites` for long-term persistence.

This is the **ONLY way** to keep posts accessible long-term. Posts not liked will eventually disappear from all views.

### Usage

```python
from grok_web import GrokPlaywrightClient

with GrokPlaywrightClient() as client:
    # Like a post to save it
    success = client.like_post("64d57dc0-4131-41a4-ac3a-67acc1869021")
    print(f"Post saved: {success}")  # True
```

### Use Cases

- **After txt2img generation**: Like all promising images immediately
- **After img2vid generation**: Like the parent post to keep videos
- **MCTS tree building**: Like all nodes in the exploration tree
- **Quality filtering**: Only like high-scoring results

### Notes

- Can be called multiple times on same post (idempotent)
- Post appears in `/imagine/favorites` filter
- Post persists indefinitely until unliked

---

## API 7: unlike_post() - Remove from Favorites

**Purpose**: Remove a post from favorites (equivalent to deletion).

⚠️ **WARNING**: This is irreversible from the UI. Post cannot be recovered after unliking.

### Usage

```python
from grok_web import GrokPlaywrightClient

with GrokPlaywrightClient() as client:
    # Unlike to delete a post
    success = client.unlike_post("64d57dc0-4131-41a4-ac3a-67acc1869021")
    print(f"Post removed: {success}")  # True
```

### Use Cases

- **MCTS pruning**: Remove low-scoring branches
- **Cleanup**: Delete failed generations
- **Storage management**: Remove old/unused content

### Notes

- Post disappears from all views immediately
- Cannot be undone from UI (API may still access if post ID known)
- Use cautiously - consider archiving elsewhere first

---

## API 8: create_video_from_image() - Generate Video

**Purpose**: Trigger video generation (img2vid) from an existing image using Grok's chat API.

This is the **core MCTS operation** for expanding the tree.

### Usage

```python
from grok_web import GrokPlaywrightClient
import time

with GrokPlaywrightClient() as client:
    # 1. Trigger video generation
    response = client.create_video_from_image(
        image_url="https://imagine-public.x.ai/imagine-public/images/64d57dc0-4131-41a4-ac3a-67acc1869021.png",
        parent_post_id="64d57dc0-4131-41a4-ac3a-67acc1869021",
        aspect_ratio="2:3",
        video_length=6
    )

    print(f"Video generation started: {response}")

    # 2. Wait for generation (polling)
    for i in range(60):  # Max 60 attempts (10 minutes)
        time.sleep(10)  # Check every 10 seconds

        details = client.get_post_details(parent_post_id)
        if details.video_count > 0:
            print(f"Video generated! {details.video_count} videos found")

            # 3. Like the post to persist the video
            client.like_post(parent_post_id)

            # 4. Access the video
            for child in details.children:
                print(f"Video URL: {child.hd_media_url or child.media_url}")
            break
    else:
        print("Timeout waiting for video generation")
```

### Parameters

| Parameter | Type | Default | Options | Description |
|-----------|------|---------|---------|-------------|
| `image_url` | str | - | - | Full URL to image on `imagine-public.x.ai` |
| `parent_post_id` | str | - | - | Parent image post UUID |
| `aspect_ratio` | str | `"2:3"` | `"2:3"`, `"16:9"`, etc. | Video dimensions |
| `video_length` | int | `6` | `6`, `15` | Duration in seconds |

### Response

Returns a chat API response dict containing:
- `conversationId`: Chat conversation UUID
- `message`: Initial message sent
- Additional metadata

**Note**: The response does NOT contain the generated video. You must:
1. Wait for generation to complete (polling)
2. Call `get_post_details(parent_post_id)` to check `video_count`
3. Access videos from `childPosts[]` array

### Use Cases

- **MCTS expansion**: Generate videos from promising image nodes
- **Batch generation**: Queue multiple videos from selected images
- **Testing**: Generate videos with different parameters

### Notes

- Video generation is **asynchronous** - requires polling
- Typical generation time: 30 seconds to 5 minutes
- Failed generations may not add to `childPosts[]` (moderation)
- Always `like_post()` after successful generation

---

## Complete MCTS Workflow Example

```python
from grok_web import GrokPlaywrightClient
import time

def mcts_expand_node(image_url: str, parent_post_id: str):
    """Expand a MCTS node by generating video from image."""

    with GrokPlaywrightClient() as client:
        print(f"Expanding node: {parent_post_id}")

        # 1. Trigger video generation
        response = client.create_video_from_image(
            image_url=image_url,
            parent_post_id=parent_post_id,
            aspect_ratio="2:3",
            video_length=6
        )
        print("Video generation triggered")

        # 2. Poll for completion
        max_attempts = 60  # 10 minutes
        for attempt in range(max_attempts):
            time.sleep(10)

            details = client.get_post_details(parent_post_id)

            if details.video_count > 0:
                print(f"✅ Video generated ({details.video_count} videos)")

                # 3. Like to persist
                client.like_post(parent_post_id)
                print(f"✅ Post liked (saved to favorites)")

                # 4. Return video metadata for MCTS scoring
                videos = []
                for child in details.children:
                    videos.append({
                        "video_id": child.id,
                        "url": child.hd_media_url or child.media_url,
                        "duration": child.duration,
                        "resolution": child.resolution
                    })

                return {"success": True, "videos": videos}

            print(f"Waiting... ({attempt + 1}/{max_attempts})")

        print("❌ Timeout - video generation failed or moderated")
        return {"success": False, "reason": "timeout"}

# Usage
result = mcts_expand_node(
    image_url="https://imagine-public.x.ai/imagine-public/images/uuid.png",
    parent_post_id="uuid"
)

if result["success"]:
    print(f"Videos: {result['videos']}")
else:
    print(f"Failed: {result['reason']}")
```

---

## Async Client Support

All three new APIs are also available in `GrokAsyncPlaywrightClient`:

```python
from grok_web import GrokAsyncPlaywrightClient
import asyncio

async def async_video_generation():
    async with GrokAsyncPlaywrightClient() as client:
        # Trigger generation
        response = await client.create_video_from_image(
            image_url="https://imagine-public.x.ai/...",
            parent_post_id="uuid",
            aspect_ratio="2:3",
            video_length=6
        )

        # Poll for completion
        for _ in range(60):
            await asyncio.sleep(10)

            details = await client.get_post_details("uuid")
            if details.video_count > 0:
                await client.like_post("uuid")
                return details.children

        return None

# Run
videos = asyncio.run(async_video_generation())
```

---

## Error Handling

```python
from grok_web import GrokPlaywrightClient
from grok_web.exceptions import GrokAuthError, GrokAPIError

with GrokPlaywrightClient() as client:
    try:
        response = client.create_video_from_image(
            image_url="https://...",
            parent_post_id="uuid"
        )
    except GrokAuthError as e:
        print(f"Authentication failed: {e}")
        print("Check ~/.grok-config.json cookies")
    except GrokAPIError as e:
        print(f"API error: {e}")
```

---

## Best Practices

1. **Always like after generation**:
   ```python
   # After txt2img
   client.like_post(image_post_id)

   # After img2vid
   client.like_post(parent_post_id)  # Same ID!
   ```

2. **Handle moderation gracefully**:
   ```python
   # Video may be moderated (not appear in childPosts)
   if details.video_count == 0:
       print("Video moderated or failed")
       # Mark MCTS node as failed
   ```

3. **Batch operations with rate limiting**:
   ```python
   import time

   for image_id in selected_images:
       client.create_video_from_image(...)
       time.sleep(5)  # Rate limit: ~12 requests/minute
   ```

4. **Use async for parallel operations**:
   ```python
   async def generate_many():
       async with GrokAsyncPlaywrightClient() as client:
           tasks = [
               client.create_video_from_image(url, id, ...)
               for url, id in images
           ]
           return await asyncio.gather(*tasks)
   ```

---

## Next Steps

- Integrate with MCTS tree building in `grok-imagine-expert`
- Implement scoring system for video quality
- Add retry logic for failed generations
- Monitor chat response stream for real-time status
