# NHC Storm Watch — Automated Advisory & Recon Texts

Checks NHC pages on a schedule and texts you a summary whenever something new
posts — no copy/pasting required.

Two independent watchers:

| | Public/Intermediate Advisory | Recon VDM |
|---|---|---|
| Script | `check_storm.py` | `check_recon.py` |
| Workflow | `.github/workflows/nhc-watch.yml` | `.github/workflows/nhc-recon-watch.yml` |
| State file | `state.json` | `recon_state.json` |
| Runs | Every hour | Every 15 minutes |
| Watches | `https://www.nhc.noaa.gov/text/MIATCPAT2.shtml` | `https://www.nhc.noaa.gov/text/MIAREPNT2.shtml` |
| Dedupes on | Advisory number (e.g. "6A") | Fix time (VDMs don't have a sequence number) |

## What the Advisory text includes

Every Advisory text is ONE message with three parts, in this order:

1. **A short "in your voice" blurb** — templated (not AI-generated at runtime,
   so it's consistent and doesn't need an API key), covering what changed and
   where the storm is headed, in a condensed version of your house style
2. **The comparison section** — distance/direction/speed moved since the last
   advisory, whether NHC changed anything (pulled from their own "CHANGES
   WITH THIS ADVISORY" section), and status/wind/pressure deltas
3. **The full technical breakdown** — every field from the Forecast/Advisory
   product (location, movement, pressure, sustained wind, peak forecast
   wind, position accuracy, next advisory) plus the complete multi-day
   forecast track table

**Important — message length:** this combined message typically runs
**1,000–1,200+ characters**. That is long for SMS. Most carriers will split
it into several text-message parts, and some may truncate it. This was a
deliberate tradeoff — the alternative (short text + separate email for the
full breakdown) was considered and turned down in favor of one message that
has everything, accepting that it may arrive as multiple texts or get cut
off on some carriers. If that turns out to be a real problem once you're
seeing it live, it's a small change to split the delivery — just say so.

Two NHC products get fetched for this single message:
- **Public/Intermediate Advisory** (`MIATCPAT2`) — drives the "something new
  posted" detection and the comparison math
- **Forecast/Advisory** (`MIATCMAT2`) — drives the full technical breakdown
  and forecast track. Note: Intermediate Advisories (like "6A") only exist
  as Public Advisories, not Forecast/Advisories — so when an intermediate
  triggers the message, the "Full Breakdown" section will reflect whatever
  the most recent Forecast/Advisory was, which may be numbered slightly
  differently. That's expected, not a bug.

## What the Recon text includes

- Fix time (Central), aircraft callsign/mission, position, pressure, eye
  status, flight-level wind, surface (SFMR) wind when reported

## One-time setup (15–20 minutes)

1. **Create a GitHub repo.** Go to github.com, click "New repository," name it
   whatever you want (e.g. `nhc-watch`), keep it **Private**, create it.

2. **Upload all these files**, keeping the folder structure:
   - `check_storm.py`
   - `check_recon.py`
   - `state.json`
   - `recon_state.json`
   - `.github/workflows/nhc-watch.yml`
   - `.github/workflows/nhc-recon-watch.yml`

   Easiest way: on the repo page, click "Add file" → "Upload files," and drag
   the whole unzipped folder in from Finder — GitHub will preserve the
   `.github/workflows/` structure.

3. **Get an app password for sending email.** If you have Gmail:
   - Go to myaccount.google.com → Security → 2-Step Verification (turn on if
     not already) → App passwords
   - Create one named "nhc-watch," copy the 16-character password
   - (Any SMTP provider works — Gmail is just the most common)

4. **Add secrets to the repo.** In your repo: Settings → Secrets and
   variables → Actions → "New repository secret." Add each of these (both
   workflows share the same five secrets):

   | Name | Value |
   |---|---|
   | `SMTP_SERVER` | `smtp.gmail.com` (if using Gmail) |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | your full Gmail address |
   | `SMTP_PASS` | the 16-character app password from step 3 |
   | `ALERT_TO` | `4092895745@vtext.com` (your Verizon gateway) |

5. **Test each one manually.** Go to the "Actions" tab in your repo. You'll
   see both "NHC Storm Watch" and "NHC Recon Watch" listed. For each: click
   it → "Run workflow" → "Run workflow" button. Watch it run — click into it
   to see the log. Since both state files start empty, the first run of each
   should always fire a text — but note the very first Advisory run won't
   have a "previous advisory" to compare against yet, so it'll skip the
   "Moved: X mi" line that first time only. From the second new advisory
   onward, that comparison will be there.

   If no recon aircraft is currently flying, `check_recon.py` will find no
   VDM data and exit quietly without texting — that's expected, not a
   failure.

6. **Check your phone.** You should get a text within a minute or two of the
   runs finishing.

Once step 5 works for both, you're done — they run themselves from then on,
independently of each other.

## Adjusting the check frequency

Edit the `cron` line in the relevant workflow file. Cron times are in UTC,
not Central.

**Advisory** (`nhc-watch.yml`):
- Every hour (default): `5 * * * *`
- Every 30 minutes: `5,35 * * * *`

**Recon** (`nhc-recon-watch.yml`):
- Every 15 minutes (default): `*/15 * * * *`
- Every 30 minutes: `5,35 * * * *`

More frequent checks don't cause spam — you only get texted when something
actually changes — but they do use more of your free Actions minutes. At the
default settings for both watchers combined, you're using well under the
2,000 free minutes/month.

## If the storm dissipates and a new one forms

Both URLs are tied to "Atlantic Storm #2" this season:
- `check_storm.py`: `MIATCPAT2.shtml` (the `2` in `AT2`)
- `check_recon.py`: `MIAREPNT2.shtml` (the `2` in `NT2`)

If Bertha dissipates and the next system becomes Storm #3, update the `2` to
`3` in both files (`ADVISORY_URL` / `RECON_URL` near the top of each) and
commit the change.

## Troubleshooting

- **No text arrived:** Check the Actions tab → click the failed/latest run →
  read the log. Most common cause is a typo in one of the secrets.
- **"Failed to send text" in the log:** Almost always an SMTP auth issue —
  double check the app password, and that 2-Step Verification is on for
  Gmail (app passwords require it).
- **First Advisory text is missing the "Moved: X mi" line:** Expected — there
  was no previous advisory in state.json to compare against yet. It'll show
  up starting with the second new advisory.
- **Recon watcher ran but sent nothing, and that seems wrong:** Check the log
  for "No VDM fix time found" (no plane currently flying — normal) vs "No new
  fix" (same fix as last time — also normal) vs an actual error.
- **Message got cut off on your phone:** Some carriers truncate long SMS from
  email gateways. The Advisory text can run 300–400 characters with all the
  comparison data — if it's getting cut, let me know and I can trim it down.


