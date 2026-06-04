# CLAUDE.md — GitHub Copilot Enterprise Exporter Coding Guide

## Goal
Extend the existing GitHub Copilot enterprise exporter without breaking current production behavior.

## Current architecture
- Enterprise Copilot usage metrics come from the GitHub Copilot usage metrics report APIs.
- The exporter runs in Kubernetes and writes transformed metrics into VictoriaMetrics.
- Grafana reads from VictoriaMetrics.
- The exporter already supports:
  - enterprise daily and 28-day usage reports
  - enterprise user-level usage reports
  - enterprise seat snapshot metrics
  - idempotent imports using VictoriaMetrics existence checks
  - controlled bootstrap and date-range backfill

## Important design rule
Do not replace the current design with direct Grafana-to-GitHub queries.
Keep the collector model:
GitHub APIs → exporter → VictoriaMetrics → Grafana.

## New requirement
Add logic to track legacy premium request usage for dashboarding now, while preparing the exporter for GitHub AI Credits as the final long-term billing metric.

## Key product facts
- Premium requests are legacy and are being replaced by GitHub AI Credits.
- Copilot usage metrics APIs are report-based and enterprise-scoped.
- Billing AI credit usage and billing premium request usage are available through billing APIs that are organization-scoped.
- Because of that mismatch, enterprise dashboards must aggregate billing usage across one or more organizations.

## Required implementation approach
1. Preserve all current enterprise usage metrics logic.
2. Add organization-scoped billing collectors for:
   - premium request usage
   - AI credit usage
3. Aggregate those results into enterprise-labeled metrics.
4. Keep all new code additive and backward compatible.
5. Add concise metadata comments above each major code block.
6. Keep the code clean, readable, and production-oriented.

## Required environment variables
- `GH_TOKEN`
- `GH_ENTERPRISE`
- `VM_IMPORT_URL`
- `VM_SERIES_URL`
- `GH_BILLING_ORGS` — comma-separated organization names used for organization billing usage APIs

Optional toggles:
- `ENABLE_BILLING_REPORTS=true|false`
- `BOOTSTRAP_28D=true|false`
- `FORCE_BOOTSTRAP=true|false`
- `ENABLE_DATE_RANGE_BACKFILL=true|false`
- `BACKFILL_START_DAY=YYYY-MM-DD`
- `BACKFILL_END_DAY=YYYY-MM-DD`

## Metrics to keep
Retain all existing enterprise usage, code generation, user, and seat metrics unless there is a strong reason not to.

## Metrics to add
### Billing metrics
From the org billing premium request usage report:
- per-item metrics labeled by org, model, product, sku, unit_type
- gross quantity
- gross amount USD
- discount quantity
- discount amount USD
- net quantity
- net amount USD
- price per unit USD
- org-level totals per day

From the org billing AI credit usage report:
- the same metric families as above

### Team-level visibility
Add enterprise user-team join metrics so the team can build team-level dashboards.

### AI adoption cohorts
Add GitHub’s newer AI adoption phase metrics if present in the usage report payloads.

### Seat enrichment
Keep seat activity and assignment metadata such as:
- assigning team
- plan type
- last activity timestamp
- last authenticated timestamp
- pending cancellation
- active last 7d / 28d / 90d totals
- never active total

## Error handling expectations
- Premium request billing is legacy; if that endpoint fails or disappears, log a warning and continue.
- AI credit billing should be treated as the long-term metric.
- Never let one billing org failure stop the full cycle.
- Keep the exporter alive if a cycle fails.

## Duplicate prevention expectations
- Do not re-import a day if that day already exists in VictoriaMetrics.
- Keep the existing idempotent behavior for enterprise usage imports.
- Add a simple billing-day marker metric so billing day imports can also be skipped safely.

## Testing expectations
Before handing off code:
1. Make sure the file compiles.
2. Make sure the new code is additive, not destructive.
3. Make sure the logging is readable.
4. Make sure metric names are consistent and dashboard-friendly.
5. Make sure comments explain what each major block is doing.

## Dashboarding guidance
Recommend dashboards for:
- AI credit daily quantity and USD cost by org
- Premium request daily quantity and USD cost by org
- AI credit usage by model
- Premium request usage by model
- Team-level Copilot adoption
- AI adoption cohort distribution
- Seat activity recency and never-active seat counts

## Implementation preference
Prefer small, clearly named helper functions over deeply nested logic.

## Do not do
- Do not remove existing metrics unless they are clearly obsolete.
- Do not hide failures silently.
- Do not mix GitHub report API logic with Grafana logic.
- Do not hardcode org names in code; use env vars.
