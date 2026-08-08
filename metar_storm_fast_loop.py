#!/usr/bin/env python3
"""
metar_storm_fast_loop.py -- Continuous wrapper around
metar_storm_pipeline.py.

The original metar-storm-watch.yml used a plain */10 cron trigger, but
this repo already runs many long-lived "Fast Watch (Continuous)" jobs
that each occupy a runner slot for hours at a stretch -- under that
load, a small, quick cron job competing for a *new* runner slot every
10 minutes can get starved out entirely (observed directly: it fired
once right after being manually primed, then never fired again for
hours, while the continuous jobs kept running uninterrupted). Moving to
the same continuous self-restarting pattern the other reliable
pipelines already use (nhc_fast_loop.py etc.) fixes this the same way
it works for them: once a slot is won, this job just keeps it instead
of re-competing for a new one every cycle.

Checks every 5 minutes internally -- per instruction, faster than the
original 10-minute cadence, tightened once the KFDM WeatherNet layer
was added (that source updates faster than ASOS/METAR obs do). For
just under GitHub's 6-hour job cap, then triggers a fresh run of
itself via the GitHub API right before it would be killed.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import metar_storm_pipeline

POLL_INTERVAL_SEC = 300  # 5 minutes -- per instruction, "the faster the better"
MAX_RUNTIME_SEC = 5.75 * 3600  # 5h45m -- leaves a buffer before GitHub's 6h hard cap
START_TIME = time.time()


def commit_state_if_changed():
    try:
        subprocess.run(["git", "config", "user.name", "metar-storm-watch-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "metar_storm_state.json"], check=True)
        result = subprocess.run(["git", "diff", "--quiet", "--cached"])
        if result.returncode == 0:
            return  # nothing changed

        subprocess.run(["git", "commit", "-m", "Update METAR storm state [skip ci]"], check=True)

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
    url = f"https://api.github.com/repos/{repo}/actions/workflows/metar-storm-watch.yml/dispatches"
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
            metar_storm_pipeline.main()
        except Exception as e:
            print(f"[iteration {iteration}] Unexpected error, continuing loop: {e}")

        commit_state_if_changed()
        time.sleep(POLL_INTERVAL_SEC)

    print("Loop ending cleanly after triggering restart.")


if __name__ == "__main__":
    main()
