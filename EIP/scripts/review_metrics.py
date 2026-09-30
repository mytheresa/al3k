#!/usr/bin/env python3
"""
Code review metrics dashboard generator for the Orca repo.

Pulls merged-PR data from GitHub via the `gh` CLI and renders a single
self-contained HTML dashboard you can open in a browser and review with
the team on a regular cadence (e.g. before each retro).

Metrics (all based on REAL human reviewers -- automated/bot accounts are excluded):
  - Cycle time:            PR opened  -> merged
  - Time to first review:  PR opened  -> first human review
  - Approval -> merge:     approval requirement met -> merged
  - PR size breakdown:     median cycle time by lines-changed bucket
  - Cycle time distribution

Requirements:
  - Python 3.8+
  - `gh` CLI, authenticated (`gh auth status`)

Usage:
  python3 scripts/review_metrics.py                 # last 90 days, writes review-metrics-dashboard.html
  python3 scripts/review_metrics.py --days 30
  python3 scripts/review_metrics.py --since 2026-06-23
  python3 scripts/review_metrics.py --repo mytheresa/orca --limit 300
  python3 scripts/review_metrics.py --out /tmp/dash.html
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Automated / bot reviewer accounts to exclude so metrics reflect real people.
# Extend this set if new automation reviewers are added to the repo.
BOT_LOGINS = {
    "qltysh",
    "dependabot",
    "github-actions",
    "coderabbitai",
    "copilot-pull-request-reviewer",
    "copilot-swe-agent",
}


def parse_ts(ts):
    """Parse a GitHub ISO-8601 timestamp into an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fetch_prs(repo, limit):
    """Fetch merged PRs via the gh CLI. Returns a list of dicts."""
    cmd = [
        "gh", "pr", "list",
        "--state", "merged",
        "--limit", str(limit),
        "--json", "number,title,author,createdAt,mergedAt,additions,deletions,reviews",
    ]
    if repo:
        cmd += ["--repo", repo]
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,  # never wait on interactive input
            env=env,
            timeout=120,
        )
    except FileNotFoundError:
        sys.exit("Error: `gh` CLI not found. Install it and run `gh auth login`.")
    except subprocess.TimeoutExpired:
        sys.exit("Error: `gh pr list` timed out after 120s.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Error running gh: {e.stderr.strip() or e}")
    return json.loads(out.stdout)


def human_reviews(pr):
    """Return list of (datetime, state, login) for non-bot reviews."""
    result = []
    for r in pr.get("reviews") or []:
        login = (r.get("author") or {}).get("login")
        if not login or login in BOT_LOGINS:
            continue
        result.append((parse_ts(r["submittedAt"]), r.get("state"), login))
    return result


def first_human_approvals(pr, required=2):
    """
    Return the datetime at which the approval requirement was satisfied,
    using the earliest approval per distinct human reviewer.
    Returns None if there are no human approvals.
    """
    approvals = {}
    for t, state, login in human_reviews(pr):
        if state == "APPROVED" and (login not in approvals or t < approvals[login]):
            approvals[login] = t
    times = sorted(approvals.values())
    if not times:
        return None
    idx = min(required, len(times)) - 1
    return times[idx]


def pct(sorted_arr, q):
    n = len(sorted_arr)
    return sorted_arr[min(n - 1, int(q * n))]


def summarize(arr):
    if not arr:
        return None
    s = sorted(arr)
    return {
        "n": len(s),
        "median": statistics.median(s),
        "mean": statistics.mean(s),
        "p25": pct(s, 0.25),
        "p75": pct(s, 0.75),
        "p90": pct(s, 0.90),
        "max": max(s),
    }


def size_bucket(lines):
    if lines < 50:
        return "XS (<50)"
    if lines < 200:
        return "S (50-200)"
    if lines < 500:
        return "M (200-500)"
    return "L (500+)"


BUCKET_ORDER = ["XS (<50)", "S (50-200)", "M (200-500)", "L (500+)"]

DIST_BUCKETS = [
    ("<1h", 0, 1),
    ("1-4h", 1, 4),
    ("4-12h", 4, 12),
    ("12-24h", 12, 24),
    ("1-3d", 24, 72),
    ("3-7d", 72, 168),
    (">7d", 168, float("inf")),
]


def compute(prs, since):
    cycle, first_review, appr_to_merge = [], [], []
    by_size = {b: [] for b in BUCKET_ORDER}
    dist = {label: 0 for label, _, _ in DIST_BUCKETS}
    analyzed = 0

    for pr in prs:
        if not pr.get("mergedAt"):
            continue
        merged = parse_ts(pr["mergedAt"])
        if merged < since:
            continue
        created = parse_ts(pr["createdAt"])
        analyzed += 1

        c = (merged - created).total_seconds() / 3600
        cycle.append(c)

        for label, lo, hi in DIST_BUCKETS:
            if lo <= c < hi:
                dist[label] += 1
                break

        hr = [t for t, _, _ in human_reviews(pr)]
        if hr:
            first_review.append((min(hr) - created).total_seconds() / 3600)

        satisfied = first_human_approvals(pr)
        if satisfied:
            appr_to_merge.append(max(0, (merged - satisfied).total_seconds() / 3600))

        lines = pr.get("additions", 0) + pr.get("deletions", 0)
        by_size[size_bucket(lines)].append(c)

    return {
        "analyzed": analyzed,
        "cycle": summarize(cycle),
        "first_review": summarize(first_review),
        "appr_to_merge": summarize(appr_to_merge),
        "by_size": {b: summarize(v) for b, v in by_size.items() if v},
        "dist": dist,
        "dist_total": len(cycle),
    }


def compute_timeline(prs, since, bucket="month"):
    """
    Build a historical timeline by grouping PRs into the period they MERGED in.
    Returns an ordered list of period dicts with median cycle time and median
    time-to-first-review, so the trend can be plotted to see if we're improving.

    bucket: "month" (YYYY-MM) or "week" (ISO year-week).
    """
    def key_for(dt):
        if bucket == "week":
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        return dt.strftime("%Y-%m")

    def label_for(dt):
        # Short, human-readable axis label without a repeated year prefix.
        if bucket == "week":
            # Monday of the ISO week, e.g. "14 Apr".
            monday = dt - timedelta(days=dt.isoweekday() - 1)
            return monday.strftime("%-d %b")
        return dt.strftime("%b %Y")

    groups = {}  # period key -> aggregation for that period
    for pr in prs:
        if not pr.get("mergedAt"):
            continue
        merged = parse_ts(pr["mergedAt"])
        if merged < since:
            continue
        created = parse_ts(pr["createdAt"])
        k = key_for(merged)
        g = groups.setdefault(k, {"cycle": [], "first_review": [], "label": label_for(merged)})
        g["cycle"].append((merged - created).total_seconds() / 3600)
        hr = [t for t, _, _ in human_reviews(pr)]
        if hr:
            g["first_review"].append((min(hr) - created).total_seconds() / 3600)

    timeline = []
    for k in sorted(groups):
        g = groups[k]
        timeline.append({
            "period": k,
            "label": g["label"],
            "count": len(g["cycle"]),
            "cycle_median": statistics.median(g["cycle"]) if g["cycle"] else None,
            "first_review_median": statistics.median(g["first_review"]) if g["first_review"] else None,
        })
    return timeline


def fmt_hours(h):
    if h is None:
        return "n/a"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def render_timeline_svg(timeline):
    """
    Render a two-series line chart (median cycle time + median time to first
    review) as inline SVG. Hours are plotted on a shared axis.
    Returns an SVG string, or an empty-state message if not enough data.
    """
    pts = [t for t in timeline if t["cycle_median"] is not None]
    if len(pts) < 2:
        return "<p style='color:var(--muted)'>Not enough history yet — need at least two periods with merged PRs.</p>"

    W, H = 1040, 320
    PAD_L, PAD_R, PAD_T, PAD_B = 50, 20, 20, 70
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    cyc = [t["cycle_median"] for t in pts]
    fr = [t["first_review_median"] for t in pts if t["first_review_median"] is not None]
    y_max = max(cyc + (fr or [1])) * 1.15 or 1
    n = len(pts)

    def x(i):
        return PAD_L + (plot_w * i / (n - 1) if n > 1 else 0)

    def y(v):
        return PAD_T + plot_h * (1 - v / y_max)

    # gridlines + y labels (in hours, switch to days if large)
    grid = ""
    use_days = y_max > 96
    steps = 4
    for i in range(steps + 1):
        val = y_max * i / steps
        gy = y(val)
        label = f"{val/24:.1f}d" if use_days else f"{val:.0f}h"
        grid += (f"<line x1='{PAD_L}' y1='{gy:.1f}' x2='{W-PAD_R}' y2='{gy:.1f}' "
                 f"stroke='#334155' stroke-width='1'/>"
                 f"<text x='{PAD_L-8}' y='{gy+4:.1f}' fill='#64748b' font-size='11' "
                 f"text-anchor='end'>{label}</text>")

    def polyline(series, color):
        coords = []
        circles = ""
        for i, t in enumerate(pts):
            v = t[series]
            if v is None:
                continue
            px, py = x(i), y(v)
            coords.append(f"{px:.1f},{py:.1f}")
            # Tooltip on hover so exact values/period are available without axis clutter.
            tip = f"{t.get('label', t['period'])} · {fmt_hours(v)} · n={t['count']}"
            circles += (f"<circle cx='{px:.1f}' cy='{py:.1f}' r='3.5' fill='{color}'>"
                        f"<title>{tip}</title></circle>")
        line = (f"<polyline points='{' '.join(coords)}' fill='none' "
                f"stroke='{color}' stroke-width='2.5'/>") if coords else ""
        return line + circles

    cycle_line = polyline("cycle_median", "#38bdf8")
    fr_line = polyline("first_review_median", "#34d399")

    # x labels — thin them out and rotate so they never overlap, even with many
    # weekly points. Rotated -40° and right-anchored at each tick for a clean read.
    max_labels = 12
    label_step = max(1, -(-n // max_labels))  # ceil division
    axis_y = H - PAD_B
    xlabels = f"<line x1='{PAD_L}' y1='{axis_y}' x2='{W-PAD_R}' y2='{axis_y}' stroke='#334155' stroke-width='1'/>"
    for i, t in enumerate(pts):
        show_label = (i % label_step == 0) or (i == n - 1)
        if show_label:
            lx, ly = x(i), axis_y + 6
            # small tick mark + rotated label
            xlabels += (f"<line x1='{lx:.1f}' y1='{axis_y}' x2='{lx:.1f}' y2='{axis_y+4}' "
                        f"stroke='#475569' stroke-width='1'/>"
                        f"<text x='{lx:.1f}' y='{ly+8:.1f}' fill='#94a3b8' font-size='11' "
                        f"text-anchor='end' transform='rotate(-40 {lx:.1f} {ly+8:.1f})'>"
                        f"{t.get('label', t['period'])}</text>")

    legend = (
        "<circle cx='0' cy='-4' r='4' fill='#38bdf8'/>"
        "<text x='10' y='0' fill='#94a3b8' font-size='12'>Cycle time (median)</text>"
        "<circle cx='190' cy='-4' r='4' fill='#34d399'/>"
        "<text x='200' y='0' fill='#94a3b8' font-size='12'>Time to first review (median)</text>"
    )

    return (
        f"<svg viewBox='0 0 {W} {H}' width='100%' preserveAspectRatio='xMidYMid meet' "
        f"role='img' aria-label='Timeline of median cycle time and time to first review'>"
        f"{grid}{cycle_line}{fr_line}{xlabels}"
        f"<g transform='translate({PAD_L},{H-8})'>{legend}</g>"
        f"</svg>"
    )


def render_html(m, repo, since, generated, timeline=None):
    timeline_svg = render_timeline_svg(timeline or [])

    def stat_rows(s):
        if not s:
            return "<tr><td colspan='2'>no data</td></tr>"
        rows = [
            ("Median", fmt_hours(s["median"])),
            ("Mean", fmt_hours(s["mean"])),
            ("p25", fmt_hours(s["p25"])),
            ("p75", fmt_hours(s["p75"])),
            ("p90", fmt_hours(s["p90"])),
            ("Max", fmt_hours(s["max"])),
        ]
        return "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)

    # distribution bars
    dist_total = m["dist_total"] or 1
    max_pct = max((v / dist_total for v in m["dist"].values()), default=1) or 1
    dist_bars = ""
    for label, _, _ in DIST_BUCKETS:
        v = m["dist"][label]
        p = 100 * v / dist_total
        h = 100 * (p / (max_pct * 100))
        cls = "warn" if label in ("3-7d",) else ("danger" if label == ">7d" else "")
        dist_bars += (
            f"<div class='bcol'><div class='bar {cls}' style='height:{h:.0f}%'>"
            f"<span class='p'>{p:.0f}%</span></div><div class='bl'>{label}</div></div>"
        )

    # size bars
    size_max = max((s["median"] for s in m["by_size"].values()), default=1) or 1
    size_rows = ""
    for b in BUCKET_ORDER:
        s = m["by_size"].get(b)
        if not s:
            continue
        w = 100 * s["median"] / size_max
        cls = "big" if b == "L (500+)" else ""
        size_rows += (
            f"<div class='srow'><span class='sname'>{b}</span>"
            f"<div class='strack'><div class='sfill {cls}' style='width:{w:.0f}%'></div></div>"
            f"<span class='snum'>{fmt_hours(s['median'])} · {s['n']} PRs</span></div>"
        )

    c = m["cycle"] or {}
    fr = m["first_review"] or {}
    am = m["appr_to_merge"] or {}

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Code Review Metrics · {repo}</title>
<style>
 :root{{--bg:#0f172a;--card:#1e293b;--border:#334155;--fg:#f8fafc;--muted:#94a3b8;--blue:#38bdf8;--green:#34d399;--amber:#fbbf24;--red:#f87171}}
 *{{box-sizing:border-box;margin:0;padding:0}}
 body{{font-family:'Segoe UI',Helvetica,Arial,sans-serif;background:linear-gradient(180deg,#0f172a,#1e293b);color:var(--fg);padding:3rem;min-height:100vh}}
 .head{{max-width:1100px;margin:0 auto 2rem}}
 h1{{font-size:2rem;font-weight:800}} .meta{{color:var(--muted);margin-top:.4rem}}
 .wrap{{max-width:1100px;margin:0 auto;display:grid;gap:1.5rem}}
 .kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:1.2rem}}
 .kpi{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:1.3rem}}
 .kpi .l{{color:var(--muted);font-size:.9rem}} .kpi .v{{font-size:2.3rem;font-weight:800;margin:.2rem 0}}
 .kpi .n{{color:#64748b;font-size:.85rem}}
 .blue{{color:var(--blue)}}.green{{color:var(--green)}}.amber{{color:var(--amber)}}.red{{color:var(--red)}}
 .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}}
 .panel{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:1.5rem}}
 .panel h2{{font-size:1.15rem;margin-bottom:1rem}}
 table{{width:100%;border-collapse:collapse}} td{{padding:.4rem 0;border-bottom:1px solid var(--border);color:var(--muted)}}
 td:last-child{{text-align:right;color:var(--fg);font-weight:600}}
 .bars{{display:flex;align-items:flex-end;gap:.6rem;height:200px;padding-top:1.5rem}}
 .bcol{{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}}
 .bar{{width:70%;background:linear-gradient(180deg,#38bdf8,#0ea5e9);border-radius:6px 6px 0 0;position:relative;min-height:2px}}
 .bar.warn{{background:linear-gradient(180deg,#fbbf24,#f59e0b)}} .bar.danger{{background:linear-gradient(180deg,#f87171,#ef4444)}}
 .bar .p{{position:absolute;top:-1.4rem;left:0;right:0;text-align:center;font-size:.8rem;font-weight:700}}
 .bl{{margin-top:.5rem;color:var(--muted);font-size:.8rem}}
 .srow{{display:grid;grid-template-columns:110px 1fr 130px;align-items:center;gap:1rem;margin-bottom:1rem}}
 .sname{{font-weight:600;font-size:.95rem}} .strack{{background:#0b1220;border:1px solid var(--border);border-radius:8px;height:24px;overflow:hidden}}
 .sfill{{height:100%;background:linear-gradient(90deg,#38bdf8,#0ea5e9)}} .sfill.big{{background:linear-gradient(90deg,#fbbf24,#f59e0b)}}
 .snum{{text-align:right;color:var(--muted);font-size:.9rem}}
 .hint{{color:#64748b;font-size:.85rem;margin:-.4rem 0 .8rem}}
 .foot{{max-width:1100px;margin:2rem auto 0;color:#64748b;font-size:.85rem}}
</style></head><body>
<div class="head">
  <h1>Code Review Metrics</h1>
  <div class="meta">{repo} · window: {since.date()} → {generated.date()} · {m['analyzed']} merged PRs · bots excluded</div>
</div>
<div class="wrap">
  <div class="kpis">
    <div class="kpi"><div class="l">Median cycle time</div><div class="v blue">{fmt_hours(c.get('median'))}</div><div class="n">open → merge</div></div>
    <div class="kpi"><div class="l">Median time to first review</div><div class="v green">{fmt_hours(fr.get('median'))}</div><div class="n">human reviewers only</div></div>
    <div class="kpi"><div class="l">Median approval → merge</div><div class="v amber">{fmt_hours(am.get('median'))}</div><div class="n">time sitting approved</div></div>
    <div class="kpi"><div class="l">p90 cycle time</div><div class="v red">{fmt_hours(c.get('p90'))}</div><div class="n">slowest 10%</div></div>
  </div>

  <div class="panel">
    <h2>Trend over time — are we improving?</h2>
    <p class="hint">Lower is better. Each point is the median for PRs merged in that period.</p>
    {timeline_svg}
  </div>

  <div class="panel">
    <h2>Cycle time distribution (open → merge)</h2>
    <div class="bars">{dist_bars}</div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>PR size vs median cycle time</h2>
      {size_rows or "<p style='color:var(--muted)'>no data</p>"}
    </div>
    <div class="panel">
      <h2>Detailed stats</h2>
      <table>
        <tr><td><b>Cycle time</b></td><td></td></tr>{stat_rows(m['cycle'])}
        <tr><td><b>Time to first review</b></td><td></td></tr>{stat_rows(m['first_review'])}
        <tr><td><b>Approval → merge</b></td><td></td></tr>{stat_rows(m['appr_to_merge'])}
      </table>
    </div>
  </div>
</div>
<div class="foot">
  Generated {generated.strftime('%Y-%m-%d %H:%M %Z')} · bots excluded: {', '.join(sorted(BOT_LOGINS))}
</div>
</body></html>"""


def period_trend(timeline, series="cycle_median"):
    """
    Compare the latest complete period to the previous one for a given series.
    Returns a dict with prev/curr values, direction arrow, and a human phrase,
    or None if there aren't two comparable periods.

    "Improving" means the metric went DOWN (lower cycle time is better).
    """
    pts = [t for t in (timeline or []) if t.get(series) is not None]
    if len(pts) < 2:
        return None
    prev, curr = pts[-2], pts[-1]
    pv, cv = prev[series], curr[series]
    if pv == 0:
        return None
    change_pct = (cv - pv) / pv * 100
    if change_pct <= -5:
        arrow, verdict = "🟢 ▼", "improving"
    elif change_pct >= 5:
        arrow, verdict = "🔴 ▲", "worse"
    else:
        arrow, verdict = "⚪ ▬", "flat"
    return {
        "prev_period": prev["period"],
        "curr_period": curr["period"],
        "prev": pv,
        "curr": cv,
        "change_pct": change_pct,
        "arrow": arrow,
        "verdict": verdict,
    }


def build_chat_message(m, repo, since, generated, timeline=None):
    """Build a Google Chat message (text payload with Chat formatting)."""
    c = m["cycle"] or {}
    fr = m["first_review"] or {}
    am = m["appr_to_merge"] or {}

    # Google Chat text formatting: *bold*, _italic_, `code`.
    lines = [
        f"*Code Review Metrics — {repo}*",
        f"_{since.date()} → {generated.date()} · {m['analyzed']} merged PRs · bots excluded_",
        "",
        f"• *Median cycle time:* {fmt_hours(c.get('median'))}  (p90 {fmt_hours(c.get('p90'))})",
        f"• *Time to first review:* {fmt_hours(fr.get('median'))}  _(human reviewers)_",
        f"• *Approval → merge:* {fmt_hours(am.get('median'))}",
    ]

    # Compact period-over-period trend indicator.
    ct = period_trend(timeline, "cycle_median")
    frt = period_trend(timeline, "first_review_median")
    if ct or frt:
        lines += ["", "*Trend vs previous period:*"]
        if ct:
            lines.append(
                f"    {ct['arrow']} *Cycle time* {ct['verdict']}: "
                f"{fmt_hours(ct['prev'])} → {fmt_hours(ct['curr'])} "
                f"({ct['change_pct']:+.0f}%, {ct['prev_period']}→{ct['curr_period']})"
            )
        if frt:
            lines.append(
                f"    {frt['arrow']} *First review* {frt['verdict']}: "
                f"{fmt_hours(frt['prev'])} → {fmt_hours(frt['curr'])} "
                f"({frt['change_pct']:+.0f}%)"
            )

    lines += ["", "*Median cycle time by PR size:*"]
    for b in BUCKET_ORDER:
        s = m["by_size"].get(b)
        if s:
            lines.append(f"    • {b}: {fmt_hours(s['median'])}  ({s['n']} PRs)")
    lines += [
        "",
        "_Levers: speed up first-review pickup and shrink PRs._",
    ]
    return "\n".join(lines)


def post_to_google_chat(webhook_url, text):
    """POST a text message to a Google Chat incoming webhook."""
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201):
                sys.exit(f"Error: Google Chat returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        sys.exit(f"Error posting to Google Chat: HTTP {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"Error posting to Google Chat: {e.reason}")


def main():
    ap = argparse.ArgumentParser(description="Generate a code review metrics dashboard.")
    ap.add_argument("--repo", default=None, help="owner/name (defaults to current repo)")
    ap.add_argument("--days", type=int, default=90, help="look back N days (default 90)")
    ap.add_argument("--since", default=None, help="explicit start date YYYY-MM-DD (overrides --days)")
    ap.add_argument("--limit", type=int, default=300, help="max PRs to fetch (default 300)")
    ap.add_argument("--out", default="review-metrics-dashboard.html", help="output HTML path")
    ap.add_argument(
        "--gchat-webhook",
        default=os.environ.get("GCHAT_WEBHOOK_URL"),
        help="Google Chat incoming webhook URL (or set GCHAT_WEBHOOK_URL env var). "
             "If provided, posts a summary to the Chat space.",
    )
    ap.add_argument(
        "--no-html",
        action="store_true",
        help="skip writing the HTML dashboard (useful when only posting to Chat)",
    )
    ap.add_argument(
        "--timeline-months",
        type=int,
        default=6,
        help="how many months of history to plot in the trend chart (default 6)",
    )
    ap.add_argument(
        "--bucket",
        choices=["month", "week"],
        default="week",
        help="timeline granularity (default week)",
    )
    args = ap.parse_args()

    generated = datetime.now(timezone.utc)
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    else:
        since = generated - timedelta(days=args.days)

    print(f"Fetching merged PRs (limit {args.limit})...", file=sys.stderr)
    prs = fetch_prs(args.repo, args.limit)
    print(f"Fetched {len(prs)} PRs. Computing metrics since {since.date()}...", file=sys.stderr)

    m = compute(prs, since)
    if m["analyzed"] == 0:
        sys.exit("No merged PRs found in the selected window.")

    repo_label = args.repo or "current repo"

    # Timeline uses a wider lookback than the KPI window so the trend is visible.
    timeline_since = generated - timedelta(days=30 * args.timeline_months)
    timeline = compute_timeline(prs, timeline_since, bucket=args.bucket)

    if not args.no_html:
        html = render_html(m, repo_label, since, generated, timeline=timeline)
        with open(args.out, "w") as f:
            f.write(html)

    c = m["cycle"]
    print(f"\nAnalyzed {m['analyzed']} merged PRs.")
    print(f"  Median cycle time:        {fmt_hours(c['median'])}")
    if m["first_review"]:
        print(f"  Median time to 1st review: {fmt_hours(m['first_review']['median'])}")
    if m["appr_to_merge"]:
        print(f"  Median approval -> merge:  {fmt_hours(m['appr_to_merge']['median'])}")
    if not args.no_html:
        print(f"\nDashboard written to: {args.out}")

    if args.gchat_webhook:
        print("Posting summary to Google Chat...", file=sys.stderr)
        post_to_google_chat(
            args.gchat_webhook,
            build_chat_message(m, repo_label, since, generated, timeline=timeline),
        )
        print("Posted to Google Chat.")


if __name__ == "__main__":
    main()
