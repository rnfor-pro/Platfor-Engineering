# SKILL.md — Enterprise-Only GitHub Copilot Build Standard

Use this guidance when generating code or documentation for the GitHub Copilot project.

Requirements:
- prefer enterprise billing endpoints over org endpoints when enterprise billing is the source of truth
- use enterprise user and user-team reports for detailed dashboards
- preserve a stable normalized metric contract
- prefer exact-diff import behavior
- keep dashboards structured in rows with one overview text panel and non-technical descriptions
- avoid duplicate panels unless they answer a different question
- if Grafana sample dashboards include telemetry not exposed by GitHub usage APIs, state that clearly and recommend an OTel path instead
