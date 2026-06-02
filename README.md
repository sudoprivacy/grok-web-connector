# Grok Web Connector

Python client for [Grok Imagine](https://grok.com/imagine) — browser automation via CDP.

## Install

```bash
pip install git+https://github.com/sudoprivacy/grok-web-connector.git
```

## Quick Start

```python
from grok_web import get_client

async with get_client() as client:
    # Read
    posts = await client.list_posts(limit=10)
    details = await client.get_post_details(post_id)

    # Video (mode auto-detected from images)
    await client.create_video({"prompt": "a cat dancing"})                       # txt2vid
    await client.create_video({"images": ["post:" + pid], "prompt": "zoom in"})  # img2vid
    await client.create_video({"images": ["./frame.jpg"], "prompt": "orbit @1"}) # upload2vid

    # Image
    await client.create_image({"prompt": "sunset over mountains"})
    await client.edit_image({"post_id": pid, "edit_prompt": "add wings"})
```

## Auth

Automatic. First run opens browser for login, cookies saved to `~/.grok-config.json`. Subsequent runs reuse cookies.

```bash
python -m grok_web.auth_manager status   # check
python -m grok_web.auth_manager clear    # reset
```

## API

All public methods are on `GrokClient` (`grok_web/client.py`). Key ones:

| Category | Methods |
|----------|---------|
| Read | `list_posts`, `get_post_details`, `get_asset_file_size`, `validate_auth`, `match_local_video` |
| Video | `create_video(params)`, `extend_video`, `upgrade_video`, `download_video`, `delete_video` |
| Image | `create_image(params)`, `edit_image(params)`, `get_thumbnails`, `select_thumbnail` |
| Social | `favorite_post`, `unfavorite_post`, `like_post`, `dislike_post` |
| Navigation | `find_root_post`, `get_image_video_map`, `get_video_thumbnails` |

Generation methods take a `params: dict` — see `grok_web/schema.py` for available keys (`VIDEO_KEYS`, `IMAGE_KEYS`, `EDIT_KEYS`).

## Batch Processing

```python
from grok_web import BrowserWorkerPool

async with BrowserWorkerPool(num_workers=3) as pool:
    job_id = await pool.submit("create_video", {"images": ["post:" + pid], "prompt": "orbit"})
    results = await pool.wait()
```

## Project Structure

```
grok_web/
    client.py          # GrokClient — all API methods
    schema.py          # SSOT param definitions (add new Grok params here)
    prompt_parser.py   # @N image reference parsing
    models.py          # Pydantic response models
    actions/           # Atomic UI operations (ax_tree-first)
    pool/              # BrowserWorkerPool
```

## License

MIT
