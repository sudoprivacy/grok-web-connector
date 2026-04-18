"""Direct REST helpers for Grok API.

Bypasses the UI for flows that can be driven by a raw POST to
/rest/app-chat/conversations/new. Requires:
1. A previously-uploaded `fileMetadataId` (captured during upload_image)
2. A fresh `x-statsig-id` header snitched from an ordinary page request

Without a live x-statsig-id the server returns 403 "Request rejected by
anti-bot rules" — the static DEFAULT_STATSIG_ID alone is insufficient.
"""

import asyncio
import json
import logging

from ai_dev_browser import cdp

logger = logging.getLogger(__name__)


class StatsigSnitch:
    """Passively captures per-endpoint x-statsig-id from Grok page requests.

    Grok's frontend signs each outbound API call with a rotating
    x-statsig-id that is BOTH per-request AND endpoint-scoped — a sid
    captured from /api/log_metric does NOT validate when replayed against
    /rest/app-chat/conversations/new (server returns 403 anti-bot).

    We therefore cache the latest sid seen PER endpoint path. Tokens
    appear valid for at least ~30s within an endpoint, so the "first
    UI-triggered video gen primes the cache, subsequent retries via
    direct REST" flow works.

    Usage:
        snitch = StatsigSnitch(tab)
        await snitch.install()
        # ... at least one UI-triggered gen must happen first ...
        sid = await snitch.get("/rest/app-chat/conversations/new")
    """

    _MIN_VALID_LEN = 40  # live tokens are ~100 chars
    # Endpoints we care about. Anything else is ignored to keep the cache small.
    _TARGET_PATHS: tuple[str, ...] = (
        "/rest/app-chat/conversations/new",
        "/rest/media/post/create",
    )

    def __init__(self, tab):
        self.tab = tab
        self._by_endpoint: dict[str, str] = {}
        self._installed = False

    async def install(self) -> None:
        if self._installed:
            return
        await self.tab.send(cdp.network.enable())

        snitch = self

        async def on_req(event):
            url = event.request.url
            matched = None
            for path in snitch._TARGET_PATHS:
                if path in url:
                    matched = path
                    break
            if matched is None:
                return
            headers = event.request.headers
            sid = None
            if hasattr(headers, "get") or isinstance(headers, dict):
                sid = headers.get("x-statsig-id") or headers.get("X-Statsig-Id")
            if sid and len(sid) >= snitch._MIN_VALID_LEN:
                snitch._by_endpoint[matched] = sid
                logger.debug("StatsigSnitch captured sid for %s (%d chars)", matched, len(sid))

        self.tab.add_handler(cdp.network.RequestWillBeSent, on_req)
        self._installed = True

    @property
    def latest(self) -> str | None:
        """Backward-compat alias for tests: returns any captured sid."""
        if not self._by_endpoint:
            return None
        # Prefer conversations/new since that is the main endpoint
        return self._by_endpoint.get(
            "/rest/app-chat/conversations/new",
            next(iter(self._by_endpoint.values())),
        )

    async def get(
        self,
        endpoint_path: str = "/rest/app-chat/conversations/new",
        timeout: float = 5.0,
    ) -> str | None:
        """Return the most recent x-statsig-id for a specific endpoint path.

        Args:
            endpoint_path: The endpoint whose sid to retrieve (must be one
                of the paths StatsigSnitch was configured to watch).
            timeout: How long to wait if the cache is currently empty.
        """
        if endpoint_path in self._by_endpoint:
            return self._by_endpoint[endpoint_path]

        start = asyncio.get_event_loop().time()
        while endpoint_path not in self._by_endpoint:
            if asyncio.get_event_loop().time() - start >= timeout:
                return None
            await asyncio.sleep(0.25)
        return self._by_endpoint.get(endpoint_path)


async def capture_upload_file_id(tab, upload_action) -> dict:
    """Run an upload action while watching for the /upload-file response
    and return its parsed JSON body (including fileMetadataId, fileUri).

    Args:
        tab: browser Tab
        upload_action: async callable that triggers an upload; its return
            value is ignored.

    Returns:
        Parsed JSON body of the last /rest/app-chat/upload-file response
        seen during the action. Raises RuntimeError if none was captured.
    """
    await tab.send(cdp.network.enable())

    state: dict = {"req_id": None}

    async def on_response(event):
        if "/rest/app-chat/upload-file" in event.response.url:
            state["req_id"] = event.request_id

    tab.add_handler(cdp.network.ResponseReceived, on_response)

    await upload_action()

    # Give the transport a moment to finish loading the response body
    await asyncio.sleep(1.0)

    if state["req_id"] is None:
        raise RuntimeError("Upload did not produce a /upload-file response")

    body_result = await tab.send(cdp.network.get_response_body(request_id=state["req_id"]))
    body_text = body_result[0] if isinstance(body_result, tuple) else body_result
    return json.loads(body_text)


def build_video_submit_payload(
    file_ids: list[str],
    file_uris: list[str],
    parent_post_id: str,
    prompt: str,
    duration: int,
    resolution: str,
    aspect_ratio: str | None,
) -> dict:
    """Construct the JSON payload for POST /rest/app-chat/conversations/new.

    Grok uses two different payload shapes for uploaded-image video gen:

    - **Single image** (one fileMetadataId):
      ``fileAttachments = [file_id]`` and
      ``message = "{asset_url}  {prompt} --mode=custom"``.

    - **Multiple images** (2+ fileMetadataIds):
      ``fileAttachments = None`` (the images travel in ``imageReferences``
      instead), ``isReferenceToVideo = True``, and ``@N`` refs in the
      prompt serialize in ``message`` as ``@{fileMetadataId}``.

    In both cases ``parentPostId`` must be a server-registered post.id
    obtained from a preceding ``/rest/media/post/create`` call — NOT the
    fileMetadataId and NOT a fresh client-side UUID (either returns
    HTTP 404 "Source post not found [imagine:invalid-parent-post]").
    """
    asset_prefix = "https://assets.grok.com"
    asset_urls = [f"{asset_prefix}/{uri}" for uri in file_uris]
    mode_tag = "--mode=custom" if prompt else "--mode=normal"

    common_cfg = {
        "parentPostId": parent_post_id,
        "aspectRatio": aspect_ratio or "2:3",
        "videoLength": duration,
        "resolutionName": resolution,
    }

    if len(file_ids) == 1:
        fid = file_ids[0]
        single_url = asset_urls[0]
        if prompt:
            message = f"{single_url}  {prompt} {mode_tag}"
        else:
            message = f"{single_url}  {mode_tag}"
        return {
            "temporary": True,
            "modelName": "grok-3",
            "message": message,
            "fileAttachments": [fid],
            "toolOverrides": {"videoGen": True},
            "enableSideBySide": True,
            "responseMetadata": {
                "experiments": [],
                "modelConfigOverride": {"modelMap": {"videoGenModelConfig": common_cfg}},
            },
        }

    # Multi-file: @N refs serialize to "@{file_id}"; images travel via
    # imageReferences; fileAttachments is null.
    if prompt:
        import re

        def _sub(m):
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(file_ids):
                return f"@{file_ids[idx]}"
            return m.group(0)

        prompt_serialized = re.sub(r"@(\d+)", _sub, prompt)
        message = f"{prompt_serialized} {mode_tag}"
    else:
        message = mode_tag

    multi_cfg = dict(common_cfg)
    multi_cfg["isReferenceToVideo"] = True
    multi_cfg["imageReferences"] = asset_urls

    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": message,
        "fileAttachments": None,
        "toolOverrides": {"videoGen": True},
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {"modelMap": {"videoGenModelConfig": multi_cfg}},
        },
    }


async def create_media_post(
    tab,
    statsig_id: str,
    prompt: str = "",
    media_type: str = "MEDIA_POST_TYPE_VIDEO",
) -> str:
    """POST /rest/media/post/create to obtain a parentPostId.

    Grok's UI pre-registers a post via this endpoint before calling
    conversations/new; the returned post.id is what becomes the
    parentPostId in the video-gen payload. Using a fresh random UUID
    instead gets HTTP 404 "Source post not found".

    Returns the new post's id (UUID string).
    """
    create_payload = {"mediaType": media_type, "prompt": prompt}
    await tab.evaluate(
        "window.__grokCreatePayload = " + json.dumps(json.dumps(create_payload)),
        await_promise=False,
    )
    await tab.evaluate(
        "window.__grokCreateHeaders = "
        + json.dumps(
            {
                "Content-Type": "application/json",
                "x-statsig-id": statsig_id,
            }
        ),
        await_promise=False,
    )
    result_json = await tab.evaluate(
        """
        (async () => {
          try {
            const resp = await fetch('/rest/media/post/create', {
              method: 'POST',
              headers: window.__grokCreateHeaders,
              body: window.__grokCreatePayload,
              credentials: 'include',
            });
            const text = await resp.text();
            return JSON.stringify({status: resp.status, body: text});
          } catch (e) {
            return JSON.stringify({status: 0, body: 'FETCH_ERR: ' + e.message});
          }
        })()
        """,
        await_promise=True,
        return_by_value=True,
    )
    result = json.loads(result_json) if isinstance(result_json, str) else result_json
    status = result.get("status", 0)
    body = result.get("body", "")
    if status != 200:
        raise RuntimeError(f"post/create failed: HTTP {status}. Body: {body[:300]}")
    parsed = json.loads(body)
    post_id = parsed.get("post", {}).get("id")
    if not post_id:
        raise RuntimeError(f"post/create returned no post.id: {body[:300]}")
    return post_id


async def direct_submit_video(
    tab,
    payload: dict,
    statsig_id: str,
    timeout: float = 300.0,
) -> str:
    """POST the video-generation payload directly via in-page fetch.

    Uses the tab's session cookies (credentials: "include") plus the
    provided x-statsig-id to pass Grok's anti-bot check. Waits for the
    complete NDJSON stream (Grok holds the HTTP response open until
    generation finishes, typically 20-60 s).

    The fetch runs as a detached JS task; Python polls ``window`` for
    its result every 1 s. This avoids ai-dev-browser's 30 s per-CDP-
    command timeout (a single ``tab.evaluate(..., await_promise=True)``
    blocking for the whole stream would hit ``COMMAND_TIMEOUT`` and
    then fail obscurely via the ``send_raw`` retry path's snake_case
    bug on the ``allowUnsafeEvalBlockedByCSP`` CDP parameter).

    Raises:
        RuntimeError: On non-2xx HTTP status or if the total wait
            exceeds ``timeout``.
    """
    import uuid

    request_id = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "x-xai-request-id": request_id,
        "x-statsig-id": statsig_id,
    }

    # Stash payload + headers on window so we never embed them in JS source.
    await tab.evaluate(
        "window.__grokSubmitPayload = " + json.dumps(json.dumps(payload)),
        await_promise=False,
    )
    await tab.evaluate(
        "window.__grokSubmitHeaders = " + json.dumps(headers),
        await_promise=False,
    )

    # Kick off the fetch as a detached promise (NOT awaited here — each CDP
    # command stays short so we don't trip COMMAND_TIMEOUT).
    await tab.evaluate(
        """
        window.__grokResult = null;
        window.__grokError = null;
        (async () => {
          try {
            const resp = await fetch('/rest/app-chat/conversations/new', {
              method: 'POST',
              headers: window.__grokSubmitHeaders,
              body: window.__grokSubmitPayload,
              credentials: 'include',
            });
            const text = await resp.text();
            window.__grokResult = JSON.stringify({status: resp.status, body: text});
          } catch (e) {
            window.__grokError = 'FETCH_ERR: ' + (e && e.message ? e.message : String(e));
          }
        })();
        """,
        await_promise=False,
    )

    # Poll. Each evaluate is sub-second so CDP never times out, regardless
    # of how long Grok takes to finish the NDJSON stream.
    start = asyncio.get_event_loop().time()
    while True:
        if asyncio.get_event_loop().time() - start > timeout:
            raise RuntimeError(f"direct_submit_video: timed out after {timeout}s")
        status = await tab.evaluate(
            "JSON.stringify({r: window.__grokResult, e: window.__grokError})",
            await_promise=False,
            return_by_value=True,
        )
        parsed = json.loads(status) if isinstance(status, str) else {}
        if parsed.get("e"):
            raise RuntimeError(parsed["e"])
        if parsed.get("r"):
            result_json = parsed["r"]
            break
        await asyncio.sleep(1.0)

    result = json.loads(result_json) if isinstance(result_json, str) else result_json
    status = result.get("status", 0)
    body = result.get("body", "")

    if status != 200:
        raise RuntimeError(f"Direct submit failed: HTTP {status}. Body: {body[:400]}")

    return body
