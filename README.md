# PA 2026 results feed

Data feed for the **PA Election Desk** site (For DPF Legislative Team). The site reads these files directly from GitHub, so updates here show up on the site within about 5 minutes, with no Netlify deploy.

## What writes what

| File | Written by | How often |
|---|---|---|
| `data/results.json` | Hourly news monitor (Claude scheduled task): AP and network race calls, concessions, certification news | Every hour at :25 |
| `data/candidates.json` | Hourly news monitor: candidate profiles | When profiles change |
| `data/state.json` | `scripts/state_fetch.py` via GitHub Actions: governor count and statewide reporting from electionreturns.pa.gov | Every 30 minutes |
| `state/discovery.json`, `state/raw/` | The same script: a log of what the state site returned, for troubleshooting | Each run (raw copies only on the first run or when something fails) |

The state count never changes a race call. The state does not call races; calls come from AP and the networks.

## What the state site allows

The state site's homepage (governor results, statewide precincts reporting, official/unofficial banner) loads for automated readers. Its State Senate, State House and Congress results pages answer automated readers with HTTP 403. This reader respects that and does not request them. Legislative and congressional counts come from the hourly news monitor (AP results as published by Spotlight PA and other outlets).

## Setup (one time)

1. Keep this repository **public**. The site can only read public files.
2. Upload everything in this folder, including the hidden `.github` folder. On a Mac, press Cmd+Shift+. in Finder to show it. Or use **Add file > Create new file**, name it `.github/workflows/state-feed.yml` and paste the contents.
3. Open the **Actions** tab, select **State returns feed**, and click **Run workflow**. The first run takes about 2 minutes.
4. Check `data/state.json`. `"status": "ok"` means it is working. `"blocked"` means the state site refused GitHub's servers; see `state/discovery.json` and the backup option below.

## Do not

- Do not connect this repository to Netlify. Every commit would trigger a paid deploy, and commits happen every 15 minutes.
- Do not edit `data/state.json` or `state/` by hand. The reader overwrites them.

## Backup if the state site blocks GitHub

A scheduled task on a computer with the Claude desktop app can open the state site in a normal browser every hour and write the same `data/state.json`. It only runs while that computer is on.

## Slowing it down later

After certification (Nov. 23), change `7,37 * * * *` in `.github/workflows/state-feed.yml` to `0 */6 * * *` (every 6 hours), or disable the workflow in the Actions tab.
