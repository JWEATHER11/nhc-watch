#!/usr/bin/env python3
"""
nhc_recon_fast_loop.py -- Continuous wrapper around nhc_recon_pipeline.py.

GitHub's cron-based schedule trigger proved unreliable for this repo -- the
scheduled */5 workflow never fired on its own even after hours of an active
recon mission. Rather than depend on that, this uses the same self-restarting
continuous-loop pattern that's already proven reliable for the main advisory
pipeline (nhc_fast_loop.py): stay running for just under GitHub's 6-hour job
cap, checking every 60 seconds, then trigger a fresh run of itself via the
GitHub API right before hitting that limit.
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import json

import nhc_recon_pipeline

POLL_INTERVAL_SEC = 60
MAX_RUNTIME_SEC = 5.75 * 3600
START_TIME = time.time()


def commit_state_if_changed():
    try:
        subprocess.run(["git", "config", "user.name", "nhc-recon-fast-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "recon_pipeline_state.json"], check=True)
        result = subprocess.run(["git", "diff", "--quiet", "--cached"])
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update recon pipeline state [skip ci]"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("State committed and pushed.")
    except Exception as e:
        print(f"Failed to commit state (non-fatal, will retry next loop): {e}")


def trigger_self_restart():
    token = os.environ.get("GH_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://api.github.com/repos/{repo}/actions/workflows/nhc-recon-fast-watch.yml/dispatches"
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


def main():
    print(f"Starting continuous recon poll loop, checking every {POLL_INTERVAL_SEC}s.")
    iteration = 0
    while True:
        elapsed = time.time() - START_TIME
        if elapsed > MAX_RUNTIME_SEC:
            print(f"Reached {elapsed/60:.1f} min runtime -- restarting the chain now.")
            trigger_self_restart()
            break

        iteration += 1
        try:
            nhc_recon_pipeline.main()
        except SystemExit as e:
            print(f"[iteration {iteration}] nhc_recon_pipeline.main() exited with code {e.code} -- continuing loop.")
        except Exception as e:
            print(f"[iteration {iteration}] Unexpected error, continuing loop: {e}")

        commit_state_if_changed()
        time.sleep(POLL_INTERVAL_SEC)

    print("Loop ending cleanly after triggering restart.")


if __name__ == "__main__":
    main()
