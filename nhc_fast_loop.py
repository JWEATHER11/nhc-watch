#!/usr/bin/env python3
"""
nhc_fast_loop.py -- Continuous near-real-time wrapper around nhc_pipeline.py.

GitHub Actions caps a single job at 6 hours. This script runs a tight
polling loop (checking every ~25 seconds) for just under that limit, then
triggers a fresh run of itself via the GitHub API right before it would be
killed, creating an unbroken 24/7 chain -- no gap between one job ending
and the next one starting.

Every loop iteration just calls nhc_pipeline.main(), which already contains
all the fetch/compare/convert/AI-rewrite/deliver logic and its own dedupe
check (it does nothing if the advisory hasn't changed). This script adds
nothing new on top of that logic -- it only adds the "keep checking every
25 seconds, forever" wrapper, and commits state immediately after any real
send (not just at the end) so a crash mid-loop can't cause a duplicate
alert on the next run.
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import json

import nhc_pipeline

POLL_INTERVAL_SEC = 25
MAX_RUNTIME_SEC = 5.75 * 3600  # 5h45m -- leaves a buffer before GitHub's 6h hard cap
START_TIME = time.time()


def commit_state_if_changed():
    """Commits pipeline_state.json immediately after any real send, so a
    crash mid-loop never causes us to lose track of what's already been
    sent (which would risk a duplicate alert on the next run)."""
    try:
        subprocess.run(["git", "config", "user.name", "nhc-fast-watch-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "pipeline_state.json"], check=True)
        result = subprocess.run(["git", "diff", "--quiet", "--cached"])
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update pipeline state [skip ci]"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("State committed and pushed.")
    except Exception as e:
        print(f"Failed to commit state (non-fatal, will retry next loop): {e}")


def trigger_self_restart():
    """Dispatches a fresh run of this same workflow via the GitHub API,
    right before this job would be killed for hitting the runtime cap --
    this is what keeps the chain unbroken."""
    token = os.environ.get("GH_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://api.github.com/repos/{repo}/actions/workflows/nhc-fast-watch.yml/dispatches"
    payload = json.dumps({"ref": "main"}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Self-restart triggered successfully (HTTP {resp.status}).")
    except urllib.error.HTTPError as e:
        print(f"Self-restart FAILED: HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}", file=sys.stderr)
        print("The 6-hour safety-net schedule trigger will catch this if the chain breaks here.", file=sys.stderr)


def main():
    print(f"Starting continuous poll loop, checking every {POLL_INTERVAL_SEC}s.")
    iteration = 0
    while True:
        elapsed = time.time() - START_TIME
        if elapsed > MAX_RUNTIME_SEC:
            print(f"Reached {elapsed/60:.1f} min runtime, approaching the 6h cap -- restarting the chain now.")
            trigger_self_restart()
            break

        iteration += 1
        try:
            nhc_pipeline.main()
        except SystemExit as e:
            print(f"[iteration {iteration}] nhc_pipeline.main() exited with code {e.code} (likely a fetch/API failure this cycle) -- continuing loop.")
        except Exception as e:
            print(f"[iteration {iteration}] Unexpected error, continuing loop: {e}")

        commit_state_if_changed()
        time.sleep(POLL_INTERVAL_SEC)

    print("Loop ending cleanly after triggering restart.")


if __name__ == "__main__":
    main()
