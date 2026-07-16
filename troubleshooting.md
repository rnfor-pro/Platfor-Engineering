**Title:** Build Loki-Sourced Copilot Analytics Dashboards (5 Dashboards)

**Description:**
Design and build five Grafana dashboards sourced from Loki structured log data written by the GitHub Copilot exact-diff exporter. These dashboards surface per-user, per-day insights that are not available in the existing VictoriaMetrics dashboards which only provide enterprise-level aggregates.

**Dashboards to build:**
1. **Daily User Activity Feed** — Human-readable daily activity log per user showing model, feature, prompt count, LOC, and IDE. Enables managers to look up any developer's recent Copilot activity without PromQL knowledge.
2. **Model and Feature Usage Intelligence** — Cross-user analysis of model and feature adoption. Identifies which users are using the most expensive models and which features are being adopted fastest across the enterprise.
3. **AI Credit Spend Intelligence** — Per-user credit spend leaderboard, daily spend trends, and anomaly detection for users exceeding normal consumption thresholds. Fills the gap left by VictoriaMetrics which only captures enterprise-level billing totals.
4. **IDE and Plugin Version Compliance** — Fleet-wide view of IDE versions and Copilot plugin versions in use. Enables IT and platform teams to identify developers on outdated plugin versions and track CLI version distribution.
5. **Adoption Behaviour Patterns** — Correlates actual usage behaviour (chat vs agent vs CLI, chat panel modes) with AI adoption phase. Surfaces users on the cusp of phase graduation and explains the behavioural differences between phases.

**Data source:** Loki stream `{enterprise="sherwin-williams", log_source="copilot_exporter"}` parsed with `| json`. All dashboards require the Loki output to be enabled and healthy on the exporter and the `max_streams_per_user` Loki limit to be raised to accommodate the copilot exporter stream volume.

**Dependencies:**
- Loki data availability confirmed via Explore queries (Steps 1-10 checklist)
- GitHub Copilot exact-diff exporter running with `LOKI_ENDPOINT` configured
- Loki `max_streams_per_user` limit raised or `service_name` label injection excluded for copilot exporter streams
- Exporter `ai_adoption_phase` dict fix deployed and backfill completed

**Acceptance criteria:**
- All five dashboards imported and returning data in Grafana
- Enterprise and username dropdown variables populated dynamically
- All panels show data for the last 90 days
- Nav links connect all five dashboards to each other and to the existing VictoriaMetrics dashboards