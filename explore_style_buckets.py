#!/usr/bin/env python3
"""
Batch generate videos with different stable_ids to explore style buckets.

Usage:
    python explore_style_buckets.py

This script will:
1. Generate 10 videos, each with a unique stable_id
2. Save results to explore_style_buckets_results.json
3. Print URLs for manual style comparison
"""

import asyncio
import json
from datetime import datetime

from grok_web import NodriverClient

PARENT_POST_ID = "9ac51419-65c8-467c-958e-97e9f1abadfa"
NUM_VIDEOS = 10
OUTPUT_FILE = "explore_style_buckets_results.json"


async def generate_with_stable_id(client: NodriverClient, stable_id: str, idx: int) -> dict:
    """Generate one video with a specific stable_id."""
    print(f"\n[{idx}/{NUM_VIDEOS}] stable_id: {stable_id[:30]}...")

    result = await client.create_video_via_ui(
        parent_post_id=PARENT_POST_ID,
        stable_id=stable_id,
    )

    status = "✅" if not result.moderated else "❌ moderated"
    print(f"       {status} video: {result.video_id}")

    return {
        "index": idx,
        "stable_id": stable_id,
        "video_id": result.video_id,
        "moderated": result.moderated,
        "url": f"https://grok.com/imagine/post/{result.video_id}" if not result.moderated else None,
    }


async def main():
    print("=" * 60)
    print("Exploring Style Buckets")
    print("=" * 60)
    print(f"Generating {NUM_VIDEOS} videos with different stable_ids...")
    print(f"Parent post: {PARENT_POST_ID}")

    results = []

    async with NodriverClient(host="127.0.0.1", port=9222) as client:
        for i in range(1, NUM_VIDEOS + 1):
            stable_id = NodriverClient.generate_stable_id()
            result = await generate_with_stable_id(client, stable_id, i)
            results.append(result)
            await asyncio.sleep(2)

    # Save results
    output = {
        "timestamp": datetime.now().isoformat(),
        "parent_post_id": PARENT_POST_ID,
        "num_videos": NUM_VIDEOS,
        "results": results,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✅ Results saved to {OUTPUT_FILE}")

    # Summary
    successful = [r for r in results if not r["moderated"]]
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(
        f"Total: {len(results)}, Successful: {len(successful)}, Moderated: {len(results) - len(successful)}"
    )

    print("\n📹 Videos for style comparison:")
    for r in successful:
        print(f"  #{r['index']:2d}. {r['url']}")
        print(f"       stable_id: {r['stable_id'][:40]}...")

    print("\n🔍 Look for videos with different styles:")
    print("   - Camera: zoom in vs hover vs zoom out")
    print("   - Motion: minimal vs head movement vs body movement")
    print("   - Timing: immediate vs delayed start")


if __name__ == "__main__":
    asyncio.run(main())
