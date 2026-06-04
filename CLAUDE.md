# CLAUDE.md — GitHub Copilot Enterprise Metrics Collector

## Purpose
This repository contains the Python exporter that pulls GitHub Copilot enterprise usage reports and seat assignment data, transforms them into time-series metrics, and writes them into VictoriaMetrics for Grafana dashboards.

## Architecture
GitHub Copilot Enterprise APIs
→ exporter in Kubernetes
→ VictoriaMetrics
→ Grafana

## Non-negotiable requirements
1. Do not replace the report-based ingestion model with direct Grafana-to-GitHub querying.
2. Keep signed report downloads separate from the authenticated GitHub metadata session.
3. Preserve idempotency:
   - check VictoriaMetrics before reimporting daily data
   - skip 28-day bootstrap when data already exists
   - keep backfill flags opt-in
4. Keep the GitHub token in Kubernetes Secret only.
5. Preserve non-root runtime and CA-bundle handling.
6. Favor backward-compatible additions over breaking changes.

## Coding guidelines
- Keep functions small and purpose-specific.
- Add short metadata comments above each logical code block.
- Prefer explicit helper functions over copy/paste logic.
- Normalize labels and metric names consistently.
- Treat new GitHub report fields defensively: support missing keys and schema drift.

## Data sources currently in use
- enterprise usage reports:
  - /enterprises/{enterprise}/copilot/metrics/reports/enterprise-28-day/latest
  - /enterprises/{enterprise}/copilot/metrics/reports/enterprise-1-day
- enterprise user usage reports:
  - /enterprises/{enterprise}/copilot/metrics/reports/users-28-day/latest
  - /enterprises/{enterprise}/copilot/metrics/reports/users-1-day
- enterprise billing / seat assignments:
  - /enterprises/{enterprise}/copilot/billing/seats

## Must-have metrics
### Core enterprise usage
- DAU / WAU / MAU
- chat / agent / CLI activity
- code generation activity
- code acceptance activity
- lines suggested / added / deleted
- PR metrics
- feature / IDE / language breakdowns

### User-level usage
- user_login / user_id labels
- prompt count
- generation / acceptance
- usage by feature / IDE / language / model
- AI adoption phase labels

### Billing / seat metrics
- enterprise seat total
- seat rows returned
- pending cancellation total
- active last 7d / 28d / 90d
- never active total
- plan type totals
- assigning team totals
- user-level seat records and timestamp metrics

### New GitHub metrics to support
- ai_adoption_phase on user-level reports
- totals_by_ai_adoption_phase on enterprise-level reports

## Safety checklist before finalizing changes
- code compiles cleanly
- new metrics do not break existing metrics
- signed URL downloads still use a separate session
- seat snapshot import still works
- backfill / bootstrap flags remain opt-in
- comments explain each major code block briefly

## Preferred output when asked to modify code
- updated `main.py`
- a concise summary of what changed
- any new metrics added
- any new environment variables if required
- any migration / backfill notes if historical reimport is needed
