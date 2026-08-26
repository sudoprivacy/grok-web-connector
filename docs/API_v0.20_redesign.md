# v0.20 — Orthogonal generation API (migration guide)

The `images=[...]` param carried **four different roles** distinguished by string
prefixes (`post:` / `ref:` / `video:` / local path), and each generation method
multiplexed 3–5 hidden modes from it. v0.20 splits those into **single-purpose,
role-explicit methods** so a caller reads one method name and knows exactly what
it does. The old overloaded params still work but emit `DeprecationWarning` and
will be removed in v0.21.

## The role split

| Role | Old (overloaded) | New (single-purpose) |
|---|---|---|
| text → image | `create_image({"prompt": p})` | **unchanged** — `create_image` is now txt2img ONLY |
| 1 Grok image → new image | `create_image({"images": ["post:id"]})` / `edit_image({"images": ["post:id"], ...})` | **`img2img({"source": "post:id", "prompt": p})`** |
| N Grok images → 1 composed | `create_image({"images": ["post:a","post:b"]})` | **`compose({"sources": ["a","b"], "prompt": p})`** (warm-gated) |
| region inpaint | `precise_edit(...)` | **unchanged** (already single-purpose) |
| text → video | `create_video({"prompt": p})` | **unchanged** — `create_video` is now txt2vid ONLY |
| 1 Grok image → video | `create_video({"images": ["post:id"]})` / `animate_post(...)` | **`animate({"frame": "post:id", "prompt": p})`** |
| saved Reference → video | `create_video({"images": ["ref:id"]})` | **`reference_video({"references": ["id"], "prompt": p})`** |
| extend a video | `create_video({"images": ["video:id"]})` | `extend_video({"video_id": "id"})` (already existed) |
| upload local → video | `create_video({"images": ["./a.jpg"]})` | `animate({"frame": "./a.jpg"})` (a source may be a local path) |

## Deprecations (still work, warn, removed v0.21)
- `create_image(images=...)` → use `img2img` / `compose`
- `create_video(images=...)` → use `animate` / `reference_video` / `extend_video`
- `edit_image(...)` → use `img2img`
- `animate_post(...)` → use `animate`
- Lower-level twins (`edit_current`, `extend_current`, `generate_video_from_current`)
  are now documented as internal/advanced; prefer the role-methods above.

## New method contracts

```python
# 1 image -> new image (in-Grok, works cold). source: 'post:<id>' | <id> | local path
await client.img2img({"source": "post:<id>", "prompt": "...", "quality": "v2"})

# N Grok images -> 1 composed image (in-Grok). WARM-GATED: needs a warm
# conversations/new token (do any create_video first) — else raises a hint.
await client.compose({"sources": ["<id_a>", "<id_b>"], "prompt": "..."})

# 1 image -> video (img2vid). frame: 'post:<id>' | <id> | local path
await client.animate({"frame": "post:<id>", "prompt": "slow zoom", "duration": "6s"})

# saved Reference(s) -> video (character consistency). references: reference ids
await client.reference_video({"references": ["<ref_id>"], "prompt": "...", "duration": "6s"})
```

Every new method takes a single `params` dict (same convention as
`create_image`/`create_video`), validated against its schema KEYS; returns the
same `ImageGenerationResult` / `VideoGenerationResult` as before.

## Why the split matches the transports
`ref:`→video vs `post:`→image were never the same operation (saved Reference =
`referenceToVideo`; a Grok image as an img2img source = `imageToImage`). `compose`
is warm-gated because its `conversations/new` POST needs a frontend-signed
statsig token; `img2img`/`animate` are not. Splitting the methods makes those
real differences visible in the API instead of hidden behind one `images=` slot.
