# Exact-Diff Exporter Validation Notes

## What was validated locally
- Python syntax compilation passed.
- Sample enterprise usage rows normalized successfully.
- Sample user usage rows with `ai_adoption_phase` normalized successfully.
- Sample AI credit billing payload normalized successfully.
- Deterministic metric keys are stable for identical metric name + timestamp + labels.

## Important limitation
This validation was local only.
It did not call the live GitHub APIs or a live VictoriaMetrics instance because those credentials and endpoints are environment-specific.