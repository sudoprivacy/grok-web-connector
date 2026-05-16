"""Dump rate-limit / quota / submit-disabled banner DOM evidence from a
running Grok Imagine tab — for users who hit a fail-fast that connector
couldn't classify (no rate-limit / quota phrase matched the wide-net
selector + text walk).

Workflow:
  1. Don't close the browser when you see a rate-limit banner.
  2. From the connector repo:  python -m scripts.dump_grok_banner
  3. Forward workbench/grok_banner_dump.txt to the connector maintainer.
  4. Maintainer extends the phrase dictionary in client.py so the next
     hit raises a typed GrokRateLimitError / GrokQuotaExceededError.

Connects to whichever Chrome is on the debug port via the connector's
own auto-discovery. Doesn't burn quota — only reads DOM that's already
on screen.
"""

from __future__ import annotations

import argparse
import asyncio
import json as _json
from pathlib import Path

from ai_dev_browser import connect_browser, find_debug_chromes, is_port_in_use

WORKBENCH = Path("workbench")
WORKBENCH.mkdir(exist_ok=True)
OUT = WORKBENCH / "grok_banner_dump.txt"


# Wide DOM scan — pierces shadow roots, captures (a) any visible element
# matching banner-shaped selectors and (b) any visible element whose
# direct-text-content matches a rate-limit / quota / wait keyword.
_DUMP_JS = r"""
(() => {
    // Walk every element, including shadow roots.
    const all = [];
    const walk = (root) => {
        let els;
        try { els = root.querySelectorAll('*'); }
        catch (e) { return; }
        for (const el of els) {
            all.push(el);
            if (el.shadowRoot) walk(el.shadowRoot);
        }
    };
    walk(document);

    const visible = el => {
        try {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        } catch (e) { return false; }
    };

    const BANNER_SELECTORS = [
        '[role="alert"]', '[role="status"]', '[role="tooltip"]',
        '[role="dialog"]', '[role="banner"]',
        '[class*="toast" i]', '[class*="banner" i]',
        '[class*="notification" i]', '[class*="error" i]',
        '[class*="alert" i]', '[class*="message" i]',
        '[class*="popover" i]', '[class*="warning" i]',
        '[class*="hint" i]', '[class*="dialog" i]',
        '[class*="tooltip" i]', '[class*="rate" i]',
        '[class*="limit" i]',
    ];
    const banners = [];
    for (const sel of BANNER_SELECTORS) {
        try {
            for (const el of all) {
                if (!visible(el) || !el.matches || !el.matches(sel)) continue;
                const text = (el.innerText || '').trim();
                if (!text || text.length > 500) continue;
                banners.push({
                    selector: sel,
                    tag: el.tagName,
                    cls: (el.className || '').toString().slice(0, 100),
                    role: el.getAttribute('role') || '',
                    aria: el.getAttribute('aria-label') || '',
                    text: text.slice(0, 300),
                });
                if (banners.length > 30) break;
            }
        } catch (e) { /* invalid selector / cross-origin shadow */ }
        if (banners.length > 30) break;
    }

    const KEYWORDS = [
        'rate', 'limit', 'quota', 'try again', 'too many', 'wait',
        'throttle', 'minute', 'hour', 'second', 'exceed', 'exhaust',
        'reach', 'maximum', 'subscription', 'upgrade',
        '稍后', '稍候', '频率', '频次', '限制', '限次', '上限', '已达',
        '配额', '额度', '用完', '用尽', '分钟', '小时', '请等', '请稍',
        '太多', '超出', '重试',
    ];
    const re = new RegExp(
        KEYWORDS.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'),
        'i'
    );
    const text_matches = [];
    for (const el of all) {
        if (!visible(el)) continue;
        // Direct text content only — Array.from(childNodes).filter(text)
        // — so we don't double-count nested elements.
        const text = Array.from(el.childNodes || [])
            .filter(n => n.nodeType === 3)
            .map(n => n.textContent.trim())
            .join(' ').trim();
        if (!text || text.length < 4 || text.length > 300) continue;
        if (!re.test(text)) continue;
        text_matches.push({
            tag: el.tagName,
            cls: (el.className || '').toString().slice(0, 100),
            role: el.getAttribute('role') || '',
            aria: el.getAttribute('aria-label') || '',
            text: text.slice(0, 300),
        });
        if (text_matches.length >= 30) break;
    }

    // Submit button + disabled state, for cross-reference.
    const STRICT = new Set(['submit', '提交', 'send', '发送']);
    const norm = s => (s || '').trim().toLowerCase();
    const submit = all.find(el =>
        el.tagName === 'BUTTON' && visible(el) &&
        (STRICT.has(norm(el.getAttribute && el.getAttribute('aria-label')))
         || STRICT.has(norm(el.innerText)))
    );

    return JSON.stringify({
        url: location.href,
        title: document.title,
        submit: submit ? {
            aria: (submit.getAttribute('aria-label') || '').trim(),
            disabled: !!submit.disabled,
        } : null,
        banners,
        text_matches,
    });
})()
"""


async def dump_one_tab(port: int, tab) -> dict | None:
    try:
        raw = await tab.evaluate(_DUMP_JS)
        data = _json.loads(raw) if isinstance(raw, str) else raw
        return data
    except Exception as e:
        print(f"  [port={port}] evaluate error: {e}")
        return None


def format_dump(port: int, ws: str | None, data: dict) -> str:
    lines = [f"\n{'=' * 70}\n"]
    lines.append(f"port: {port}\n")
    lines.append(f"workspace: {ws or '<none>'}\n")
    lines.append(f"url: {data.get('url')}\n")
    lines.append(f"title: {data.get('title')}\n")
    s = data.get("submit") or {}
    lines.append(f"submit: aria={s.get('aria')!r}  disabled={s.get('disabled')}\n")
    banners = data.get("banners") or []
    lines.append(f"\n--- selector banners ({len(banners)}) ---\n")
    for b in banners:
        lines.append(
            f"  [{b['selector']}]  <{b['tag']}>  role={b['role']!r}  "
            f"aria={b['aria']!r}\n"
            f"      cls: {b['cls']!r}\n"
            f"      text: {b['text']!r}\n"
        )
    matches = data.get("text_matches") or []
    lines.append(f"\n--- keyword text matches ({len(matches)}) ---\n")
    for m in matches:
        lines.append(
            f"  <{m['tag']}>  role={m['role']!r}  aria={m['aria']!r}\n"
            f"      cls: {m['cls']!r}\n"
            f"      text: {m['text']!r}\n"
        )
    return "".join(lines)


def _enumerate_debug_ports(
    port_filter: int | None,
    extra_scan_range: tuple[int, int],
) -> list[tuple[int, int | None, str | None]]:
    """Find debug-ready Chromes across BOTH namespaces.

    ``find_debug_chromes()`` filters to ai-dev-browser's managed
    namespace (``~/.ai-dev-browser/profiles/...``), which excludes
    connector's own default Chrome since v0.19.15 (default profile
    lives at ``~/.grok-web-connector/profiles/<profile>/`` to dodge
    cross-agent ``browser_cleanup()`` friendly fire). So we ALSO
    probe the workspace port range directly via
    ``is_port_in_use`` — any listening port whose connect_browser
    handshake succeeds is treated as a candidate.

    De-duplicates by port. Workspace tag string returned from
    find_debug_chromes is preserved when available; bare-port
    probes get ``ws=None``.
    """
    seen: dict[int, tuple[int, int | None, str | None]] = {}

    # 1. Managed namespace (fastest, returns workspace tag).
    try:
        for port, pid, ws in find_debug_chromes():
            seen[port] = (port, pid, ws)
    except Exception:
        pass

    # 2. Direct port scan over ai-dev-browser default port range.
    low, high = extra_scan_range
    for port in range(low, high + 1):
        if port in seen:
            continue
        if is_port_in_use(port=port):
            seen[port] = (port, None, None)

    chromes = sorted(seen.values(), key=lambda c: c[0])
    if port_filter is not None:
        chromes = [c for c in chromes if c[0] == port_filter]
    return chromes


async def main(
    port_filter: int | None = None,
    scan_range: tuple[int, int] = (9350, 9450),
) -> None:
    chromes = _enumerate_debug_ports(port_filter, scan_range)
    if not chromes:
        print("No debug-ready Chrome found.")
        if port_filter is not None:
            print(f"  (filtered to port={port_filter})")
        print(
            "Make sure your Grok Chrome is running with --remote-debugging-port.\n"
            "Note: this script scans BOTH ai-dev-browser managed namespace "
            "AND the workspace port range (9350-9450 by default). If your "
            "Chrome is on a port outside that range, pass --port=<N>."
        )
        return

    print(
        f"Found {len(chromes)} debug-ready chrome(s) "
        f"(includes non-managed namespace); scanning grok.com tabs..."
    )
    all_dumps: list[str] = []
    grok_tab_count = 0

    for port, _pid, ws in chromes:
        try:
            browser = await connect_browser(port=port)
        except Exception as e:
            print(f"  [port={port}] connect failed: {e}")
            continue
        try:
            targets = getattr(browser, "targets", None) or []
            for target in targets:
                if getattr(target, "type_", "") != "page":
                    continue
                url = getattr(target, "url", "") or ""
                if "grok.com" not in url:
                    continue
                grok_tab_count += 1
                print(f"  [port={port}] {url[:90]}")
                data = await dump_one_tab(port, target)
                if data:
                    all_dumps.append(format_dump(port, ws, data))
        except Exception as e:
            print(f"  [port={port}] enumeration error: {e}")

    if grok_tab_count == 0:
        print("No grok.com tabs found in any debug-ready chrome.")
        return

    OUT.write_text(
        "Grok banner DOM dump\nGenerated by scripts/dump_grok_banner.py\n" + "".join(all_dumps),
        encoding="utf-8",
    )
    print(f"\nDump: {OUT}")
    print(
        f"\nForward {OUT} to the connector maintainer to extend the "
        f"rate-limit / quota phrase dictionaries in grok_web/client.py "
        f"(_probe_submit_state classifier)."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Restrict scan to this debug port. Default: scan all "
            "debug-ready chromes across BOTH ai-dev-browser managed "
            "namespace AND the workspace port range. Pass --port=<N> "
            "if your Chrome listens outside the default scan range."
        ),
    )
    parser.add_argument(
        "--scan-range",
        type=str,
        default="9350-9450",
        help="Port range to scan directly (format: low-high). Default: 9350-9450.",
    )
    args = parser.parse_args()
    try:
        low_s, high_s = args.scan_range.split("-")
        scan_range = (int(low_s), int(high_s))
    except (ValueError, AttributeError) as e:
        print(f"Invalid --scan-range: {args.scan_range!r} (expected 'low-high')")
        raise SystemExit(2) from e
    # If user explicitly pinned --port, include it in the direct scan
    # range so non-default ports (e.g. 9202 per the v0.19.19 bug report)
    # are also probed.
    if args.port is not None:
        scan_range = (
            min(scan_range[0], args.port),
            max(scan_range[1], args.port),
        )
    asyncio.run(main(args.port, scan_range))
