# Code Review Metrics — Analysis & Decisions

A readable record of the code review cycle-time analysis performed on the
`mytheresa/orca` repository, the findings, and the decisions that came out of
it. The accompanying tooling that produces these numbers on demand lives in
[`EIP/scripts/`](../scripts/) (see its [README](../scripts/README.md)).

## Context

The goal was to understand how long code review takes on `orca` and find the
levers that would actually reduce cycle time. All metrics below are based on
**real human reviewers** — automated/bot accounts (Qlty, GitHub Copilot,
Dependabot, github-actions) are excluded, because counting bot reviews badly
distorts the "time to first review" number.

Data source: merged pull requests pulled via the `gh` CLI. The headline window
is the **past 3 months** (45 merged PRs at time of analysis).

## Headline metrics (past 3 months)

| Metric | Value |
|--------|-------|
| Median cycle time (open → merge) | ~3.1 days (~75h) |
| p90 cycle time | ~6.2 days (~149h) |
| Median time to first **human** review | ~30 hours |
| Median approval → merge | ~0.4 hours (~24 min) |
| PRs taking > 1 day to merge | ~75% |

### Cycle time distribution

Most PRs land in the 3–7 day bucket; same-day merges are rare.

| Bucket | Share |
|--------|-------|
| < 1h | 4% |
| 1–4h | 11% |
| 4–12h | 4% |
| 12–24h | 4% |
| 1–3d | 22% |
| 3–7d | 44% |
| > 7d | 9% |

### Cycle time by PR size (the dominant driver)

| Size | Median cycle time | Volume |
|------|-------------------|--------|
| XS (<50 lines) | ~2.4h | small |
| S (50–200) | ~3.0d | — |
| M (200–500) | ~27.8h | — |
| L (500+ lines) | ~3.8d | >50% of PRs |

Large PRs are over half the volume and take roughly 40× longer to merge than
extra-small ones.

## Key findings

1. **PR size is the strongest driver of cycle time.** Large PRs dominate volume
   and merge far more slowly than small ones.
2. **Waiting for the first human review is a real cost (~30h median).** An
   earlier draft reported ~1h here, but that figure was polluted by near-instant
   bot reviews. With bots excluded, review pickup is a meaningful part of the
   ~3-day cycle.
3. **Merging after approval is effectively free (~24 min median).** Once a PR is
   approved it merges promptly in the large majority of cases; only ~4% sit more
   than a day after approval.

## Decisions

### Keep the 2-approver requirement

The team considered dropping from 2 required approvers to 1 to speed things up.
The data did **not** support it:

- Branch protection on `main` requires 2 approving reviews + code-owner review,
  with stale approvals dismissed on new pushes.
- The wait for the **second** approval has a median of ~42 minutes — under 1% of
  the ~3-day cycle time.
- 43 of 45 PRs already had 2+ approvals, so the requirement is rarely the
  blocker.

**Decision:** keep 2 approvers. Dropping to 1 would save ~42 min per PR while
giving up a reviewer and code-owner coverage — not worth it.

### Focus the levers on review pickup and PR size

Because approval → merge is already fast, the auto-merge idea was demoted to a
minor "nice to have" (it only helps the ~4% long tail). The high-impact levers:

1. **Cut time to first review** — add a review SLA (e.g. respond within one
   business day), round-robin reviewer assignment, and a "needs review" alert so
   PRs don't sit unseen.
2. **Shrink PRs** — aim for < 200 lines changed; flag oversized PRs; split large
   changes into incremental/stacked PRs.
3. **Make it visible** — track time-to-first-review, cycle time, p90, and PR
   size on a dashboard reviewed each retro so regressions are caught early.

## Monitoring

The [`review_metrics.py`](../scripts/review_metrics.py) tool regenerates these
numbers on demand and renders a standalone HTML dashboard, including a
**historical trend chart** (weekly by default) so the team can watch whether
cycle time and time-to-first-review are improving over successive retros. It can
also post a summary — with a week-over-week trend indicator — to a Google Chat
space via an incoming webhook.

Suggested cadence: run it before each retro and review the trend together.

See the [scripts README](../scripts/README.md) for usage.

---

_Note: figures are point-in-time snapshots from the analysis window and will
drift as new PRs merge. Re-run the tool for current numbers._
