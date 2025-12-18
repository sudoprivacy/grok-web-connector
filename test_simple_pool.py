#!/usr/bin/env python3
"""Simple test to verify worker pool is working."""
import asyncio
from grok_web.pool import BrowserWorkerPool


async def main():
    parent_id = "4f783d0c-004f-4fcc-86a2-c9daab90f950"

    print("Starting worker pool with 3 workers...")

    async with BrowserWorkerPool(
        num_workers=3,
        max_retries=0,
        headless=False,
    ) as pool:
        print("Worker pool started!")

        # Submit 3 jobs
        job_ids = []
        for i in range(3):
            job_id = await pool.submit(
                "create_video",
                prompt="",
                source_post_id=parent_id,
                timeout=120,
            )
            job_ids.append(job_id)
            print(f"Submitted job {i+1}: {job_id[:8]}...")

        print(f"\nWaiting for {len(job_ids)} jobs...")
        results = await pool.wait_all(timeout=300)

        print(f"\nGot {len(results)} results:")
        for job_id in job_ids:
            result = pool.get_result(job_id)
            if result:
                if result.success:
                    print(f"  ✅ {job_id[:8]}: Success - {result.data.get('video_id', 'no id')}")
                else:
                    print(f"  ❌ {job_id[:8]}: Failed - {result.error}")
            else:
                print(f"  ⚠️ {job_id[:8]}: No result")

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
