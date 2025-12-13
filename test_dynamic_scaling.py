"""Test dynamic worker scaling and job queueing while pool is running.

This is an integration test - requires real Chrome and Grok credentials.
Run manually: python test_dynamic_scaling.py
"""

import asyncio
from datetime import datetime

ORBIT_POST = "9ac51419-65c8-467c-958e-97e9f1abadfa"

# Simple commands for quick testing
COMMANDS = ["Zoom In", "Zoom Out", "Dolly In", "Dolly Out"]


async def main():
    from grok_web.pool import BrowserWorkerPool

    print("=" * 60)
    print("Dynamic Worker Scaling Test")
    print("=" * 60)
    print(f"Started: {datetime.now().isoformat()}")
    print()

    async with BrowserWorkerPool(
        num_workers=2,
        max_retries=3,
        headless=False,
        close_chrome=True,
    ) as pool:
        print(f"Initial workers: {pool.worker_count}")

        # Phase 1: Submit 2 jobs
        print("\n[Phase 1] Submit 2 jobs with 2 workers")
        job_ids = []
        for cmd in COMMANDS[:2]:
            job_id = await pool.submit(
                "create_video_via_ui",
                parent_post_id=ORBIT_POST,
                adjustment_prompt=cmd,
            )
            job_ids.append(job_id)
            print(f"  Submitted: {cmd}")

        # Wait for workers to pick up jobs
        await asyncio.sleep(5)
        print(f"  Queue size: {pool.get_status()['queue_size']}")

        # Phase 2: Add worker while running
        print("\n[Phase 2] Add worker 2 while jobs running")
        new_id = await pool.add_worker()
        print(f"  Added worker {new_id}, total: {pool.worker_count}")

        # Phase 3: Queue more jobs - new worker should pick them up
        print("\n[Phase 3] Queue 2 more jobs")
        for cmd in COMMANDS[2:]:
            job_id = await pool.submit(
                "create_video_via_ui",
                parent_post_id=ORBIT_POST,
                adjustment_prompt=cmd,
            )
            job_ids.append(job_id)
            print(f"  Submitted: {cmd}")

        # Phase 4: Wait for all jobs
        print("\n[Phase 4] Waiting for all jobs...")
        try:
            results = await pool.wait_all(timeout=300)
            success = sum(1 for r in results.values() if r.success)
            fail = sum(1 for r in results.values() if not r.success)
            print(f"  Done! Success: {success}, Failed: {fail}")
        except asyncio.TimeoutError:
            print("  Timeout!")

        # Phase 5: Remove worker (pool idle now)
        print("\n[Phase 5] Remove worker 2")
        await pool.remove_worker(new_id, wait=False)
        print(f"  Removed, remaining: {pool.worker_count}")

        # Show final stats
        print("\n[Final Stats]")
        for w_id, w_info in pool.get_status()["workers"].items():
            s = w_info["stats"]
            print(f"  Worker {w_id}: {s['success']} success, {s['fail']} fail")

    print("\n" + "=" * 60)
    print("Pool exited - Chrome terminated")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
