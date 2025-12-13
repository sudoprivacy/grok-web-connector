"""Test BrowserWorkerPool with camera commands."""

import asyncio
import json
from datetime import datetime

# Test images
ORBIT_POST = "9ac51419-65c8-467c-958e-97e9f1abadfa"
STATIC_POST = "e396bb74-3204-4eb5-bcec-035d24af9eaa"

# Camera commands to test
ORBIT_COMMANDS = [
    "Orbit",
    "Orbit 360°",
    "360° clockwise orbit",
    "360° counterclockwise orbit",
    "slow orbit",
    "orbit around subject",
    "Pan Left",
    "Pan Left, locked distance",
    "Pan Left, maintain distance",
    "Pan Right",
    "Pan Right, fixed distance from subject",
    "Dolly In",
    "Dolly Out",
    "Zoom In",
    "Zoom Out",
    "Crane Shot",
    "Handheld",
]

STATIC_COMMANDS = [
    "Static Shot",
    "Locked Shot",
    "Locked Off Shot",
    "Fixed Frame",
    "Immobile Shot",
    "Tripod Shot",
    "no camera movement",
    "tripod, no camera shake",
    "stable horizon",
]


async def get_existing_videos(client, post_id: str) -> dict[str, list]:
    """Get existing video children with their prompts."""
    try:
        children = await client.get_post_children(post_id)
        videos_by_prompt = {}
        for child in children:
            if hasattr(child, "adjustment_prompt") and child.adjustment_prompt:
                prompt = child.adjustment_prompt
                if prompt not in videos_by_prompt:
                    videos_by_prompt[prompt] = []
                videos_by_prompt[prompt].append(
                    {
                        "video_id": child.post_id,
                        "url": f"https://grok.com/imagine/post/{child.post_id}",
                    }
                )
        return videos_by_prompt
    except Exception as e:
        print(f"Error getting children for {post_id}: {e}")
        return {}


async def main():
    from grok_web import get_client
    from grok_web.pool import BrowserWorkerPool

    print("=" * 60)
    print("BrowserWorkerPool Camera Command Test")
    print("=" * 60)
    print(f"Started: {datetime.now().isoformat()}")
    print()

    # Step 1: Get existing videos to avoid duplicates
    print("Step 1: Checking existing videos...")
    async with get_client() as client:
        orbit_existing = await get_existing_videos(client, ORBIT_POST)
        static_existing = await get_existing_videos(client, STATIC_POST)

    print(f"  ORBIT_POST: {len(orbit_existing)} unique prompts found")
    print(f"  STATIC_POST: {len(static_existing)} unique prompts found")

    # Step 2: Identify commands that need testing
    orbit_needed = [cmd for cmd in ORBIT_COMMANDS if cmd not in orbit_existing]
    static_needed = [cmd for cmd in STATIC_COMMANDS if cmd not in static_existing]

    print()
    print("Step 2: Commands needing test:")
    print(f"  ORBIT commands: {len(orbit_needed)} remaining")
    for cmd in orbit_needed:
        print(f"    - {cmd}")
    print(f"  STATIC commands: {len(static_needed)} remaining")
    for cmd in static_needed:
        print(f"    - {cmd}")

    total_jobs = len(orbit_needed) + len(static_needed)
    if total_jobs == 0:
        print("\nAll commands already tested!")
        # Save existing results
        results = {
            "timestamp": datetime.now().isoformat(),
            "orbit_videos": orbit_existing,
            "static_videos": static_existing,
            "all_complete": True,
        }
        with open("camera_pool_results.json", "w") as f:
            json.dump(results, f, indent=2)
        return

    print()
    print(f"Step 3: Running {total_jobs} jobs with BrowserWorkerPool...")
    print("  Workers: 3")
    print("  Max retries: 5")
    print()

    # Step 3: Run with BrowserWorkerPool
    results = {
        "timestamp": datetime.now().isoformat(),
        "orbit_existing": orbit_existing,
        "static_existing": static_existing,
        "new_results": [],
        "failures": [],
    }

    async with BrowserWorkerPool(
        num_workers=3,
        state_file="camera_pool_state.json",
        max_retries=5,  # Max 5 retries per job
        headless=False,
    ) as pool:
        # Submit ORBIT jobs
        job_ids = []
        job_info = {}  # job_id -> (command, post_id)

        for cmd in orbit_needed:
            job_id = await pool.submit(
                "create_video_via_ui",
                parent_post_id=ORBIT_POST,
                adjustment_prompt=cmd,  # Camera command goes here!
            )
            job_ids.append(job_id)
            job_info[job_id] = (cmd, ORBIT_POST)
            print(f"  Submitted: {cmd} -> {job_id[:8]}...")

        for cmd in static_needed:
            job_id = await pool.submit(
                "create_video_via_ui",
                parent_post_id=STATIC_POST,
                adjustment_prompt=cmd,  # Camera command goes here!
            )
            job_ids.append(job_id)
            job_info[job_id] = (cmd, STATIC_POST)
            print(f"  Submitted: {cmd} -> {job_id[:8]}...")

        print()
        print("Waiting for jobs to complete...")
        print()

        # Wait for all jobs
        try:
            all_results = await pool.wait_all(timeout=1800)  # 30 min timeout
        except asyncio.TimeoutError:
            print("Timeout! Getting partial results...")
            all_results = {}
            for job_id in job_ids:
                result = pool.get_result(job_id)
                if result:
                    all_results[job_id] = result

        # Process results
        for job_id, result in all_results.items():
            cmd, post_id = job_info.get(job_id, ("unknown", "unknown"))
            if result.success:
                video_data = result.data or {}
                video_id = video_data.get("video_id", "unknown")
                print(f"  ✓ {cmd}: {video_id}")
                results["new_results"].append(
                    {
                        "command": cmd,
                        "post_id": post_id,
                        "video_id": video_id,
                        "url": f"https://grok.com/imagine/post/{video_id}",
                        "worker_id": result.worker_id,
                    }
                )
            else:
                print(f"  ✗ {cmd}: {result.error}")
                results["failures"].append(
                    {
                        "command": cmd,
                        "post_id": post_id,
                        "error": result.error,
                        "worker_id": result.worker_id,
                    }
                )

        # Get pool status
        status = pool.get_status()
        results["pool_status"] = status

    # Save results
    results["completed_at"] = datetime.now().isoformat()
    with open("camera_pool_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print()
    print("=" * 60)
    print("Summary:")
    print(f"  Successes: {len(results['new_results'])}")
    print(f"  Failures: {len(results['failures'])}")
    print("  Results saved to: camera_pool_results.json")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
