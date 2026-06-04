# CLAUDE.md — Rebuild Better Every Time for GitHub Copilot Metrics

## Mission
When working on the GitHub Copilot metrics platform, always improve the system rather than preserving weak patterns for compatibility alone.

## Default engineering posture
1. Prefer better architecture over minimal edits when the user asks to rebuild.
2. Improve:
   - code structure
   - ingestion accuracy
   - duplicate prevention
   - schema evolution
   - dashboard usefulness
   - deployment clarity
   - testability
3. When GitHub adds new fields, billing models, or report types, extend the schema safely and make the dashboards better.

## Required technical standards
- Use **exact record-level diffing** when comparing source data to VictoriaMetrics.
- Use a deterministic key based on:
  - metric name
  - timestamp
  - full sorted label set
- Read from VictoriaMetrics before writing whenever correctness matters.
- Use one unified ingestion path for:
  - normal daily runs
  - 28-day bootstrap
  - backfill
- Treat these as separate metric families:
  - enterprise usage
  - user usage
  - user-teams
  - cohorts
  - seats
  - premium requests
  - AI credits

## Architecture principles
- Grafana only reads from VictoriaMetrics.
- The exporter owns:
  - GitHub auth
  - metadata requests
  - signed download link handling
  - parsing JSON / NDJSON
  - schema normalization
  - diffing
  - writes to VictoriaMetrics
- Enterprise usage remains enterprise-scoped.
- Billing remains organization-scoped even if licensing is managed at the enterprise level.

## GitHub URL guidance
Assume current authoritative metadata endpoints are under:
- `https://api.github.com/enterprises/{enterprise}/copilot/metrics/reports/...`
- `https://api.github.com/organizations/{org}/settings/billing/...`

Assume report downloads must always follow returned signed URLs.
Do not hardcode legacy Azure download domains.

## Dashboard philosophy
Dashboards should be designed for:
- leadership
- teams
- developers

Avoid duplicate panels unless they answer materially different questions.
Prefer a small number of strong dashboards:
1. Executive Overview
2. Usage & Adoption Deep Dive
3. Code Generation & Delivery Impact
4. Billing, Seats, Teams & Cohorts

Every dashboard should:
- have a clear top overview text panel
- be organized in rows
- have non-technical panel descriptions
- preserve the established color theme unless explicitly asked otherwise

## Quality bar
Before handing off code:
- compile it
- test normalizers with sample data
- sanity check metric names
- verify dashboards are import-safe
- document deployment and rollback steps

## Improvement rule
If the user asks to rebuild, do not just patch.
Refactor, extend, and improve the code, logic, dashboards, and documentation so the next rebuild starts from a better foundation.