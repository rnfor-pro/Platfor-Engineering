# CLAUDE.md — Enterprise-Only GitHub Copilot Exporter Standard

When rebuilding this project, prefer enterprise-first design:
- enterprise usage metrics
- enterprise cohorts
- enterprise seats
- enterprise billing usage
- enterprise AI credits
- enterprise premium requests
- optional enterprise billing usage summary

Core rule:
- always improve code, logic, and dashboards when rebuilding
- keep metric names stable where possible
- prefer exact-diff deduplication over coarse day-level checks
- keep signed report download handling generic by following returned `download_links`

If runtime telemetry such as TTFT or tool calls is requested, do not invent it from GitHub usage APIs. Add a separate OpenTelemetry pipeline.
