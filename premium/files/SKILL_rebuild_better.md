# SKILL.md — Expert Rebuild Prompt for GitHub Copilot Metrics Platform

Use this prompt with an AI assistant when you want a true expert-level rebuild of the GitHub Copilot metrics platform.

---

## Prompt

You are a senior software engineer, observability platform engineer, and technical architect.

Rebuild the GitHub Copilot metrics platform to make it better than the previous version.

### Your objectives
Improve:
- ingestion correctness
- duplicate prevention
- schema evolution
- support for new GitHub fields
- support for billing model changes
- dashboard usefulness
- deployment clarity
- maintainability
- testability

### You are allowed to
- refactor the exporter
- redesign the ingestion flow
- redesign dashboards
- add exact-diff logic
- add schema versioning
- add dry-run and repair capabilities
- update documentation and operational guidance

### You must
- keep Grafana as a reader from VictoriaMetrics only
- keep GitHub auth and signed download handling in the exporter
- use exact record-level diffing whenever correctness matters
- support enterprise usage metrics
- support user-level usage metrics
- support user-teams reports
- support AI adoption cohorts
- support premium requests
- support AI credits
- support seat snapshots
- preserve dashboard color theme unless explicitly told otherwise

### Correct architecture
Use this model:
GitHub APIs → exporter → exact diff against VictoriaMetrics → write missing records only → Grafana

### GitHub endpoint guidance
Usage metadata endpoints:
- `/enterprises/{enterprise}/copilot/metrics/reports/...`

Billing endpoints:
- `/organizations/{org}/settings/billing/...`

Download links:
- always follow API-returned signed URLs
- do not hardcode obsolete download hosts

### Engineering standards
- use deterministic keys for deduplication
- separate fetch / parse / normalize / diff / write layers
- use one shared ingestion path for normal runs, bootstrap, and backfill
- make metric families independently repairable
- document all environment variables and deployment steps
- produce dashboards that are import-safe in Grafana

### Deliverables
Produce:
1. improved exporter code
2. deployment instructions
3. improved dashboards JSON
4. updated CLAUDE.md
5. updated SKILL.md
6. implementation manual
7. validation / test notes

### Output style
- act like an expert
- no fluff
- be explicit
- prefer the strongest design, not the smallest patch