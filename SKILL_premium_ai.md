# SKILL.md — Extend GitHub Copilot Exporter for Premium Requests and AI Credits

## Role
Act as a senior software engineer working on an existing Python exporter that collects GitHub Copilot enterprise telemetry and writes it into VictoriaMetrics for Grafana dashboarding.

## Objective
Extend the production exporter to add the most useful additional GitHub Copilot visibility metrics without breaking current behavior.

## Required outcome
Update the exporter so it can track:
1. legacy premium request usage for dashboarding
2. GitHub AI credit usage for dashboarding
3. AI adoption cohorts from the usage metrics API
4. team-level joins from the enterprise user-teams report
5. useful seat activity and assignment metrics

## Constraints
- Keep the current architecture.
- Do not redesign the system.
- Do not change Grafana to query GitHub directly.
- Keep the code additive and production-safe.
- Add concise metadata comments above major code blocks.
- Write clean code with minimal repetition.

## Existing architecture
GitHub Copilot APIs → exporter in Kubernetes → VictoriaMetrics → Grafana.

The exporter already supports:
- enterprise daily usage report imports
- enterprise 28-day usage report imports
- enterprise users usage report imports
- enterprise seat snapshot imports
- date-range backfill
- duplicate prevention using VictoriaMetrics existence checks

## What must be researched and used
Use official GitHub docs first.
Use GitHub changelog and community discussions for product context where official API docs are incomplete.

Important source areas:
- Copilot usage metrics report APIs
- billing usage APIs
- premium request usage docs
- AI credits docs
- AI adoption cohort changelog
- user-teams report docs

## Important product facts to preserve
- Premium requests are legacy and transition to AI credits.
- Enterprise Copilot usage metrics are report-based.
- Billing premium request usage and AI credit usage are organization-scoped billing APIs.
- Enterprise dashboards therefore need organization-level billing data aggregated into enterprise-labeled metrics.

## Environment variable design
Support these variables:
- `GH_TOKEN`
- `GH_ENTERPRISE`
- `GH_BILLING_ORGS`
- `VM_IMPORT_URL`
- `VM_SERIES_URL`
- `ENABLE_BILLING_REPORTS`
- `BOOTSTRAP_28D`
- `FORCE_BOOTSTRAP`
- `ENABLE_DATE_RANGE_BACKFILL`
- `BACKFILL_START_DAY`
- `BACKFILL_END_DAY`

## Billing metrics to add
For each org in `GH_BILLING_ORGS`, collect per-day data for:
### Premium request usage
From the org billing premium request usage API:
- gross quantity
- gross amount
- discount quantity
- discount amount
- net quantity
- net amount
- price per unit
Labels should include at least:
- enterprise
- org
- product
- sku
- model
- unit_type

### AI credit usage
From the org billing AI credit usage API:
- gross quantity
- gross amount
- discount quantity
- discount amount
- net quantity
- net amount
- price per unit
Use the same label strategy.

Also emit org-level daily totals.

## Additional deep-insight metrics to add
### Team visibility
Use the enterprise `user-teams-1-day` report.
Emit:
- per-user team membership daily record
- daily team member count

### AI adoption cohort metrics
Use the newer report fields if present:
- `ai_adoption_phase` at user level
- `totals_by_ai_adoption_phase` at enterprise level

Emit cohort counts and activity metrics by phase.

### Seat activity enrichment
Emit:
- seat assigned record
- plan type totals
- assigning team totals
- pending cancellation total
- active last 7d / 28d / 90d totals
- never active total
- last activity timestamp
- last authenticated timestamp
- created timestamp
- updated timestamp
- pending cancellation timestamp

## Duplicate prevention requirements
Preserve current idempotency.
In addition:
- add a billing-day marker metric
- skip billing import if that billing day marker already exists
- do not reimport enterprise usage or team join data if that day already exists

## Error handling requirements
- If premium request billing fails, log a warning and continue.
- If AI credit billing fails, log a warning and continue.
- Never let one org failure break the full cycle.

## Code style requirements
- Keep helper functions small.
- Keep naming explicit.
- Keep comments short and useful.
- Avoid unnecessary abstractions.
- Avoid repetition where practical.
- Keep the final file readable by operators, not just developers.

## Validation requirements
Before finishing:
1. compile the Python file
2. ensure all imports resolve
3. ensure no existing behavior is removed unintentionally
4. ensure billing logic is optional when `GH_BILLING_ORGS` is empty
5. ensure logs clearly show what was imported and skipped

## Deliverables
Provide:
1. updated `main.py`
2. updated `CLAUDE.md`
3. updated `SKILL.md`
4. short explanation of what changed
5. list of the most useful new dashboard metrics enabled by the changes
