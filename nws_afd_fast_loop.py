#!/usr/bin/env python3
"""
nws_afd_fast_loop.py -- Continuous near-real-time wrapper around
nws_afd_pipeline.py, same self-restarting pattern as every other fast
loop in this system.
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import json

import nws_afd_pipeline

POLL_INTERVAL_SEC = 25
MAX_RUNTIME_SEC = 5.75 * 3600
START_TIME = time.time()


def commit_state_if_changed():
    try:
        subprocess.run(["git", "config", "user.name", "nws-afd-fast-watch-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "nws_afd_state.json"], check=True)
        result = subprocess.run(["git", "diff", "--quiet", "--cached"])
        if result.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "Update NWS AFD state [skip ci]"], check=True)
        for attempt in range(1, 4):
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
            push_result = subprocess.run(["git", "push"])
            if push_result.returncode == 0:
                print("State committed and pushed.")
                return
            print(f"Push attempt {attempt} rejected (likely a concurrent commit from another loop) -- retrying after pull --rebase.")
            time.sleep(2)
        print("State push failed after 3 attempts -- will retry with fresh state next loop iteration.")
    except Exception as e:
        print(f"Failed to commit state (non-fatal, will retry next loop): {e}")


def trigger_self_restart():
    token = os.environ.get("GH_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://api.github.com/repos/{repo}/actions/workflows/nws-afd-fast-watch.yml/dispatches"
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
    print(f"Starting continuous NWS AFD poll loop, checking every {POLL_INTERVAL_SEC}s.")
    iteration = 0
    while True:
        elapsed = time.time() - START_TIME
        if elapsed > MAX_RUNTIME_SEC:
            print(f"Reached {elapsed/60:.1f} min runtime, approaching the 6h cap -- restarting the chain now.")
            trigger_self_restart()
            break

        iteration += 1
        try:
            nws_afd_pipeline.main()
        except SystemExit as e:
            print(f"[iteration {iteration}] nws_afd_pipeline.main() exited with code {e.code} -- continuing loop.")
        except Exception as e:
            print(f"[iteration {iteration}] Unexpected error, continuing loop: {e}")

        commit_state_if_changed()
        time.sleep(POLL_INTERVAL_SEC)

    print("Loop ending cleanly after triggering restart.")


if __name__ == "__main__":
    main()
