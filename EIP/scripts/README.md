# Code Review Metrics Dashboard

A simple, self-contained tool to track code review health for this repo and
review it with the team on a regular cadence (e.g. before each retro).

It pulls merged-PR data from GitHub via the `gh` CLI and generates a single
standalone HTML dashboard. No servers, no external services, no extra
dependencies beyond `gh` and Python 3.

## What it measures

All metrics are based on **real human reviewers** — automated/bot accounts
(Qlty, Copilot, Dependabot, etc.) are excluded.

- **Cycle time** — PR opened → merged
- **Time to first review** — PR opened → first human review
- **Approval → merge** — approval requirement met → merged (time sitting approved)
- **PR size breakdown** — median cycle time by lines-changed bucket
- **Cycle time distribution** — how PRs spread across time buckets
- **Trend over time** — a line chart of median cycle time and median
  time-to-first-review per period (month or week), so you can see at a glance
  whether the team is improving. History is back-filled from existing PR data,
  so the trend is useful from the very first run.

## Prerequisites

- Python 3.8+
- [`gh` CLI](https://cli.github.com/), authenticated: `gh auth status`

## Usage

```bash
# Last 90 days (default) → writes review-metrics-dashboard.html
python3 scripts/review_metrics.py

# Last 30 days
python3 scripts/review_metrics.py --days 30

# From a specific date
python3 scripts/review_metrics.py --since 2026-06-23

# Custom repo / output path
python3 scripts/review_metrics.py --repo mytheresa/orca --out /tmp/dash.html

# Trend chart: show 12 months of history, monthly granularity
python3 scripts/review_metrics.py --timeline-months 12 --bucket month
```

Then open the generated `review-metrics-dashboard.html` in a browser.

### The trend chart

The dashboard's "Trend over time" panel plots the median cycle time and median
time-to-first-review for each period, so you can watch whether things are
improving over successive retros. Two knobs control it:

| Flag | Description |
|------|-------------|
| `--timeline-months N` | Months of history to plot (default 6). Independent of the KPI window, which stays at `--days`. |
| `--bucket week\|month` | Granularity of each point (default `week`). Use `month` for a smoother, longer-range view. |

History is derived from the PRs fetched, grouped by the period they merged in —
no stored state is needed, so the trend is populated from the first run. Just
make sure `--limit` is high enough to cover the history you want to plot (it
defaults to 300 PRs).

## Posting a summary to Google Chat

The script can post a formatted summary to a Google Chat space via an
**incoming webhook** — handy for sharing the numbers with the team without
opening the HTML.

### One-time setup: create the webhook

1. Open the target Google Chat **space**.
2. Space menu → **Apps & integrations** → **Webhooks** → **Add webhooks**.
3. Give it a name (e.g. "Review Metrics") and copy the generated URL. It looks
   like `https://chat.googleapis.com/v1/spaces/AAAA.../messages?key=...&token=...`.

Treat the webhook URL as a secret — anyone with it can post to the space.

### Post the summary

```bash
# Pass the webhook explicitly
python3 scripts/review_metrics.py --days 90 \
  --gchat-webhook "https://chat.googleapis.com/v1/spaces/..."

# Or set it once via env var (recommended — keeps the URL out of shell history)
export GCHAT_WEBHOOK_URL="https://chat.googleapis.com/v1/spaces/..."
python3 scripts/review_metrics.py --days 90

# Post to Chat only, skip writing the HTML file
python3 scripts/review_metrics.py --days 90 --no-html
```

The message includes cycle time, time to first review, approval → merge, and a
median-cycle-time-by-PR-size breakdown.

### Options

| Flag | Description |
|------|-------------|
| `--gchat-webhook URL` | Google Chat webhook URL (or set `GCHAT_WEBHOOK_URL`) |
| `--no-html` | Skip writing the HTML dashboard (useful when only posting) |
| `--days N` | Look back N days (default 90) |
| `--since YYYY-MM-DD` | Explicit start date (overrides `--days`) |
| `--repo owner/name` | Target repo (defaults to current) |
| `--out PATH` | Output HTML path |

> Note: posting is triggered whenever you run the command. To post
> automatically on a schedule, run it from a cron job on a machine you control,
> or wire it into CI later.

## Suggested cadence

Run it before each retro/sprint review and open the dashboard together. Watch
the two levers that actually drive cycle time:

1. **Time to first review** — the biggest bottleneck
2. **PR size** — large PRs dominate volume and take far longer

Approval → merge is already fast (~30 min), so it's shown for completeness but
isn't a lever worth optimizing.

## Adding new bot accounts

If a new automated reviewer is added to the repo, add its login to the
`BOT_LOGINS` set at the top of `review_metrics.py` so metrics keep reflecting
real people.

## Note on the generated HTML

`review-metrics-dashboard.html` is a generated artifact and is already listed
in the repo `.gitignore`, so snapshots won't be committed by default. If you'd
rather keep a history of dashboards, remove that entry and commit them
intentionally.
