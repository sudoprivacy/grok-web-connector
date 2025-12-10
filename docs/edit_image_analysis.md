# Edit Image API Analysis

**Discovery Date**: 2025-12-10
**Feature**: Image editing via chat API (new Grok Imagine feature)

---

## Capture 1: Moderated Result

**File**: `grok-downloaded-video-local-organizer/grok-edit-image.tmp` (lines 1-59)
**Post ID**: `6f261828-e8d4-4d8f-abee-3c8b95d79e72`
**User Prompt**: Unknown (message field shows 5 spaces: "     ")
**Result**: Both images moderated (blurry, not clickable)

### UI Flow

1. Click "Edit Image" button on post page
2. Edit interface appears below original image
3. Two placeholder images appear immediately
4. Images stream from blurry to clear (progressive generation)
5. Both images appeared moderated in this case

### API Request

**Endpoint**: `POST /rest/app-chat/conversations/new`

**Referer**: `https://grok.com/imagine/post/6f261828-e8d4-4d8f-abee-3c8b95d79e72`

**Payload** (decoded from line 27):

```json
{
  "temporary": true,
  "modelName": "imagine-image-edit",
  "message": "     ",
  "enableImageGeneration": true,
  "returnImageBytes": false,
  "returnRawGrokInXaiRequest": false,
  "enableImageStreaming": true,
  "imageGenerationCount": 2,
  "forceConcise": false,
  "toolOverrides": {
    "imageGen": true
  },
  "enableSideBySide": true,
  "sendFinalMetadata": true,
  "isReasoning": false,
  "disableTextFollowUps": true,
  "responseMetadata": {
    "modelConfigOverride": {
      "modelMap": {
        "imageEditModelConfig": {
          "imageReference": "https://imagine-public.x.ai/imagine-public/images/6f261828-e8d4-4d8f-abee-3c8b95d79e72.png"
        },
        "imageEditModel": "imagine"
      }
    }
  },
  "disableMemory": false,
  "forceSideBySide": false
}
```

### Key Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `modelName` | `"imagine-image-edit"` | Dedicated image editing model (not grok-3) |
| `message` | `"     "` (5 spaces) | User prompt - EMPTY in this capture |
| `imageGenerationCount` | `2` | Always generates 2 edited versions |
| `enableImageStreaming` | `true` | Progressive generation (blurry → clear) |
| `enableSideBySide` | `true` | Side-by-side comparison view |
| `imageReference` | Original image URL | Source image for editing |
| `imageEditModel` | `"imagine"` | Base model for editing |

### Mixpanel Events

**Line 28-43**: Input change event (`$mp_input_change`)
- Captures user typing in textarea
- Event type: `change`

**Line 44-59**: Click events (`$mp_click`)
- Target: "Make video" button (`aria-label="Make video"`)
- Multiple image clicks in masonry grid

### Questions

1. **Where is the user prompt?**
   - User reported entering "戴上眼罩" (add eye mask)
   - But `message` field only contains spaces
   - Possible: Prompt in different request? Or capture timing issue?

2. **Why moderated?**
   - Empty prompt?
   - Sensitive content in prompt?
   - Original image issue?
   - Stricter moderation for edits?

---

## Capture 2: Successful Result

(To be added after second capture)

---

## Comparison & Findings

(To be analyzed after both captures)
