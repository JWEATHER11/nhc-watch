# NHC Storm Watch

Fully automated hurricane advisory & recon tracking system for the Gulf Coast / Southeast Texas (Beaumont) area. Fetches NHC data, compares to the last update, rewrites it in a broadcast-meteorologist voice, and sends it to Telegram -- with zero manual steps once it's running.

## The three active pipelines

### 1. NHC Fast Watch (`nhc_pipeline.py` + `nhc_fast_loop.py`)
The main advisory watcher. Runs **continuously**, checking every ~25 seconds, 24/7.

- Fetches the Public Advisory (TCP) and Discussion (TCD) from IEM first, falling back to NHC's own site if IEM is down
- Compares to the last advisory: distance/direction moved, wind/pressure change, status change
- Converts kt->mph, Zulu->Central time, mph->Saffir-Simpson category -- all hard-coded math, zero AI involved in any number
- Sends the structured facts (not raw NHC text) to the Claude API, which writes a 2-paragraph narrative: paragraph 1 is current state & why watches/warnings changed, paragraph 2 is the medium-to-long-term outlook (peak intensity, when, when it weakens)
- Delivers to Telegram, with NHC's own Key Messages appended at the bottom, verbatim
- **How it stays running 24/7 despite GitHub's 6-hour job limit:** `nhc_fast_loop.py` wraps the pipeline in a loop, and about 15 minutes before hitting that limit, it calls the GitHub API to trigger a fresh run of itself, so there's no gap. A 6-hour scheduled trigger exists as a safety net in case that self-restart chain ever breaks.
- Workflow file: `.github/workflows/nhc-fast-watch.yml`
- State file: `pipeline_state.json`

### 2. NHC Recon Pipeline (`nhc_recon_pipeline.py`)
Separate & independent. Checks every 5 minutes for new recon aircraft fixes (VDMs).

- Only sends when a plane is actually in the storm (VDMs stop when no mission is flying)
- Tracks **running peak flight-level wind, peak surface wind (SFMR), and lowest pressure for the whole mission** -- not just the latest single fix
- Short 1-2 sentence AI read on what the fix means (still weak, steadily deepening, etc.)
- Delivers to the same Telegram chat as Fast Watch
- Workflow file: `.github/workflows/nhc-recon-pipeline.yml`
- State file: `recon_pipeline_state.json`

### 3. Legacy SMS system (`check_storm.py` / `check_recon.py`)
The original text-message-based system built before Telegram was set up. Still present but superseded by the two pipelines above. Safe to disable (see below) if you don't want duplicate alerts.

## Required secrets
Set in **Settings -> Secrets and variables -> Actions**:

| Secret | What it's for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (from platform.claude.com/dashboard) -- funds the AI rewrite step |
| `TELEGRAM_BOT_TOKEN` | From @BotFather in Telegram |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Gmail SMTP, used as a fallback if Telegram isn't configured, and for the legacy SMS system |
| `ALERT_TO` | Phone-to-SMS email gateway address, used by the legacy system & as a last-resort fallback |

## Pausing everything (for quiet weeks with no storms)
No code changes needed. Go to the **Actions** tab, click a workflow name in the left sidebar, click the **"..."** menu, click **"Disable workflow."** Click **"Enable workflow"** the same way to resume. Do this for each of the 3-4 workflows you want paused.

## When a new storm forms (this storm dissipates, a new one gets a new number)
Update `STORM_PIL_SUFFIX` in **both** `nhc_pipeline.py` and `nhc_recon_pipeline.py` -- e.g. change `"AT2"` to `"AT3"` for the 3rd Atlantic storm of the season. That's the only code change needed.

## Manually testing anything
Go to **Actions**, click the workflow, click **"Run workflow."** To force a real test send (bypass the "no change since last time" dedupe check), edit the relevant state file (`pipeline_state.json` or `recon_pipeline_state.json`) and clear the `last_advisory_number` / `last_fix_zulu` field, then run the workflow.

## If something breaks
Each pipeline has retry logic (3 attempts per step) and sends you a Telegram message describing the failure if it can't recover. Check the **Actions** tab -> click the failed run -> expand the failing step for the full error log.

## Repo visibility
This repo is **public** (code is visible to anyone, but nobody can edit it or see your secrets -- those stay encrypted regardless of visibility). It's public specifically so the continuous Fast Watch loop can run on GitHub's unlimited free Actions minutes for public repos, instead of the 2,000 min/month cap on private repos.
