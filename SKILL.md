# SKILL.md — Maintain and Extend the GitHub Copilot Enterprise Metrics Exporter

## Goal
Extend the exporter without breaking the production ingestion pattern.

## What the exporter does
- calls GitHub Copilot enterprise report APIs
- follows signed download links
- parses JSON / NDJSON report payloads
- converts report data into time-series metrics
- writes the metrics to VictoriaMetrics
- exposes exporter self-metrics on /metrics

## Implementation rules
1. Preserve existing behavior first.
2. Add new GitHub metrics by extending metric builders, not by rewriting the architecture.
3. Keep code clean and low-noise.
4. Prefer generic handling when GitHub introduces additive nested numeric fields.
5. Avoid unnecessary new API calls unless they provide real reporting value.

## Current priorities
- support new GitHub AI adoption cohort metrics
- enrich enterprise billing / seat metrics
- preserve user-level labels and activities
- keep duplicate prevention in place
- keep backfill logic and stable-day imports intact

## Recommended pattern for new fields
### Enterprise-level report additions
- if GitHub adds a new aggregate array, build a dedicated helper
- emit a record metric plus numeric metrics with stable labels

### User-level report additions
- add record metrics with user_login / user_id labels
- attach new categorical fields as labels when cardinality is acceptable
- export numeric activity fields as metrics

### Billing additions
- derive useful aggregate counts from seat assignment snapshots
- also emit user-level seat record metrics where operationally useful

## Testing checklist
- compile the file
- run builder functions against synthetic sample rows
- verify new metrics serialize into VictoriaMetrics JSON-line format
- confirm existing metrics still emit
- confirm no required env var was removed accidentally

## Don’ts
- do not move tokens into code
- do not remove CA-bundle support
- do not collapse all logic into one monolithic function
- do not introduce direct Grafana-to-GitHub assumptions
- do not disable the VM existence checks that prevent duplicates
