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
