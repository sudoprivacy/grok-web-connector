"""Atomic UI actions for Grok Imagine.

Grok-specific building blocks that compose ai-dev-browser primitives
(snapshot.find, ax.click_by_ref) into Grok UI operations.

Usage:
    from grok_web.actions.navigation import navigate_to_post
    from grok_web.actions.network_monitor import CDPMonitor
    from grok_web.actions.imagine_input import upload_image, set_mode, click_submit

Note: ``actions/post_menu`` was removed in v0.19.27 — the 2026-06
Grok UI redesign emptied the "..." menu, and every action moved to
inline post-page buttons driven directly from
``GrokClient._click_inline_post_button``.
"""
