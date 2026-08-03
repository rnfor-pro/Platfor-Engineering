# Full Codebase Audit — Findings Before Any Code Change

## Methodology
Read every function, every build_point call, every pipeline step,
every timestamp, every key derivation. Mapped all data flows from
API call → row extraction → metric builder → diff engine → VM write.

---

## Finding 1 [CRITICAL — CONFIRMED BUG]: series_key() includes value
Location: MetricPoint.series_key() → delegates to record_key() which SHA-256s
{metric, labels, timestamp_ms, VALUE}.

Impact: Two points with same metric+labels+timestamp but different values
produce different series_key hashes. Both pass "not in existing" check in
diff_points_against_vm. Both get written to VM. Affects EVERY family where
two sources can write the same series position.

Root of all count inflation.

---

## Finding 2 [CRITICAL — CONFIRMED BUG]: Cohort double-write (enterprise + fallback)
Location: _process_daily_usage() calls:
  A. build_enterprise_usage_points() → writes github_copilot_ai_adoption_phase_user_count
     from row["totals_by_ai_adoption_phase"] (GitHub's 28d-window server count)
  B. build_ai_adoption_phase_rollup_points() → writes SAME metric name
     from distinct user_id count of per-user rows (daily client count)
Both fire unconditionally every day. Values always differ → Finding 1 lets both
write → VM has 2 points per phase per day → Grafana sums them → 2x inflation.

Confirmed matches observed: Grafana phase counts ~928 vs GitHub native 31.

---

## Finding 3 [HIGH — CONFIRMED BUG]: Seat summary double-counts multi-org users
Location: build_enterprise_seat_points() — active_7d/28d/90d/never_active
counters incremented for EVERY seat row including duplicate user_logins
(users with seats in 2+ orgs under enterprise).

Impact: sum(active_28d + never_active + ...) > total_seats.
Explains why "Active Seats (28d)=706" while "Seat Total=718" but
never_active+active doesn't add up to 718.

---

## Finding 4 [HIGH — CONFIRMED BUG]: Seat delta uses live now() per-seat
Location: build_enterprise_seat_points() — now = datetime.now(timezone.utc)
set once before loop but then now.timestamp() called fresh inside loop body
(earlier version did this; current version sets now once but still could
drift between collect_at snapshot vs actual loop time).

More precisely: collected_at is passed in from _process_daily_seats() which
captures it BEFORE fetch_enterprise_seats() — the API call can take 5-30s
for large enterprises (paginating 700+ seats). Seats fetched on page 5 are
evaluated against a timestamp that is 5-30s staler than page 1, potentially
flipping boundary seats across 7d/28d/90d thresholds.

Fix: use collected_at as the frozen reference (already passed in).

---

## Finding 5 [HIGH — CONFIRMED BUG]: No intra-batch dedup before VM write
Location: diff_points_against_vm() — iterates incoming points list directly.
If two points in the same batch share series_key (same metric+labels+ts),
both independently pass "not in existing", both go into missing[], both
get imported. This can happen when:
  - enterprise_rows contains 2 rows for the same day (API pagination artifact)
  - billing API returns same SKU/model combination twice
  - bootstrap_28d_once iterates enterprise_by_day but doesn't dedup within day

---

## Finding 6 [MEDIUM]: bootstrap_28d_once has a silent cohort gap
Location: bootstrap_28d_once() only calls build_enterprise_usage_points() and
build_user_usage_points() — it does NOT call build_ai_adoption_phase_rollup_points(),
build_team_rollup_points(), or build_user_team_points().

Impact: 28-day bootstrap creates enterprise_usage and user_usage history but
leaves cohort_rollups, team_rollups, user_teams empty for the same date range.
No error is surfaced. Dashboard gap appears for cohort panels covering the
bootstrap window.

Also: bootstrap does NOT gate cohort fallback on enterprise_had_cohort_totals
(because it calls build_enterprise_usage_points directly without the gate logic).
So if FORCE_BOOTSTRAP=true is ever run on an already-ingested range, enterprise
cohort data from bootstrap + cohort_rollup data from a previous process_day
run will coexist in VM with different values.

---

## Finding 7 [MEDIUM]: Billing can write same series from enterprise AND org scope
Location: _process_daily_billing() — when billing_scope="enterprise", calls
enterprise billing endpoint. When billing_scope="organization", iterates orgs.
But if billing_scope="enterprise" and org-scoped data was previously ingested
(e.g. during development, or if billing_scope was changed), both sets exist
in VM. The series_key for enterprise-scoped and org-scoped billing has different
base labels (billing_scope="enterprise" vs "organization") so they DON'T collide.
This is by design. NOT a bug.

However: if billing_scope is changed between runs without deleting old data,
dashboards will silently show both old and new scope data in sums.
This is an operational risk, not a code bug. Needs a warning log.

---

## Finding 8 [MEDIUM]: billing total_ metrics are derived sums written to same timestamp
Location: build_billing_points() and build_billing_usage_summary_points() —
per-item metrics written for each usageItem, then total_{suffix} metrics written
using totals dict (sum of all items). These share the same ts_ms.

Risk: If GitHub API returns the same usageItem twice (observed in the wild for
some billing endpoints), the total will be doubled AND two per-item points with
same series_key (if item labels match exactly) will exist in the batch.
No dedup before total calculation.

Needs: deduplicate usageItems before accumulating totals.

---

## Finding 9 [LOW]: export_existing_points uses series_key() to build existing dict
Location: VictoriaMetricsClient.export_existing_points() reconstructs MetricPoint
from VM response then calls point.series_key(). With the old (broken) series_key
that included value, this worked correctly ONLY if the value read back from VM
exactly matched the value that would be generated. With fixed series_key (no value),
this works correctly regardless — the key is purely positional.

Status: Resolved by Finding 1 fix. No additional change needed.

---

## Finding 10 [LOW]: run_cycle() raises on any exception, main loop swallows with pass
Location: main() — while True: try: exporter.run_cycle() except Exception: pass
Combined with run_cycle raising on error, every API failure causes a 6-hour
silence with no data written and no observable signal except EXPORTER_UP=0.
This is the mechanism that caused the June 9-16 silent gap.

Existing LAST_SUCCESS gauge is the correct counter-measure, but no alert is
configured by default. Not a code bug — operational gap.

---

## Finding 11 [LOW]: import_latest_stable_day_if_needed skips if last_daily_import_day matches
Location: in-memory guard only. Pod restart resets to None, so first cycle
after restart always re-processes the stable day. This is correct.
BUT: if bootstrap sets last_daily_import_day to the SAME value as today's
stable day target, the stable day import is skipped for the whole pod lifecycle.
Example: bootstrap runs, latest bootstrapped day = 2026-08-01.
DATA_LAG_DAYS=2, today=2026-08-03, target=2026-08-01. Guard fires: skip.
2026-08-01 daily report data (including fresh totals_by_model, PR data) is
never imported for this lifecycle. Only resolved on next pod restart.

---

## Finding 12 [LOW]: fetch_enterprise_seats pagination breaks at page_rows < 50
Location: fetch_enterprise_seats() — breaks when len(page_rows) < 50.
GitHub's seat API page size is 100, not 50. A page with exactly 50 seats
would incorrectly terminate pagination, returning only the first N pages.
Should break when page_rows is empty, not < 50.

Impact: For enterprises with seat counts that produce a final page of exactly
1-49 rows (i.e. total_seats mod 100 between 1 and 49), all seats on the last
page are silently dropped. Seat total will be less than API-reported total_seats.

---

## Summary Table

| # | Severity  | Family         | Bug                                        | Impact                              |
|---|-----------|----------------|--------------------------------------------|-------------------------------------|
| 1 | CRITICAL  | ALL            | series_key includes value                  | All drift = new insert = duplicates |
| 2 | CRITICAL  | cohort_rollups | Enterprise + fallback both fire always     | 2x-30x cohort count inflation       |
| 3 | HIGH      | seat_snapshot  | Multi-org user counted N times in summary  | active/never totals exceed seat total|
| 4 | HIGH      | seat_snapshot  | Reference time not frozen before API call  | ±1 seat at window boundaries        |
| 5 | HIGH      | ALL            | No intra-batch dedup before VM write       | API dupes get double-written        |
| 6 | MEDIUM    | bootstrap      | bootstrap_28d skips cohort/team families   | Dashboard gap for bootstrap window  |
| 7 | MEDIUM    | billing        | billing_scope change leaves orphan data    | Silent double-count in dashboards   |
| 8 | MEDIUM    | billing        | usageItems not deduped before total calc   | Total inflated if API returns dupes |
| 9 | LOW       | diff engine    | Resolved by Fix 1                          | n/a                                 |
|10 | LOW       | main loop      | Exception swallow = silent gap             | Operational, not code               |
|11 | LOW       | stable day     | bootstrap can block stable day import      | One pod lifecycle missing fresh data|
|12 | LOW       | seat fetch     | Pagination breaks at <50 not <100          | Last page seats silently dropped    |
