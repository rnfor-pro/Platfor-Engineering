# GitHub Copilot Enterprise Exporter — Final Bundle

This bundle contains the enterprise-first exact-diff exporter and the Kubernetes artifacts needed to deploy it.

## Files
- `github_copilot_enterprise_exporter.py` — Python exporter
- `requirements.txt` — Python dependencies
- `Dockerfile` — enterprise runtime image with internal CA trust
- `k8s/01-secret.yaml` — secret template
- `k8s/02-deployment.yaml` — deployment + service
- `k8s/kustomization.yaml` — kustomize entry
- `dashboard_panel_mapping.md` — recommended dashboard structure and metric mapping

## Placeholders to replace
### Docker / image
- `REPLACE_WITH_ARTIFACTORY_IMAGE`

### Kubernetes
- `REPLACE_WITH_NAMESPACE`
- `REPLACE_WITH_REAL_GITHUB_TOKEN`
- `REPLACE_WITH_ENTERPRISE_SLUG`
- `REPLACE_WITH_VM_IMPORT_URL`
- `REPLACE_WITH_VM_EXPORT_URL`

## Enterprise-only endpoint model
### Copilot usage metrics
- `/enterprises/{enterprise}/copilot/metrics/reports/enterprise-1-day`
- `/enterprises/{enterprise}/copilot/metrics/reports/enterprise-28-day/latest`
- `/enterprises/{enterprise}/copilot/metrics/reports/users-1-day`
- `/enterprises/{enterprise}/copilot/metrics/reports/users-28-day/latest`
- `/enterprises/{enterprise}/copilot/metrics/reports/user-teams-1-day`

### Enterprise billing usage
- `/enterprises/{enterprise}/settings/billing/ai_credit/usage`
- `/enterprises/{enterprise}/settings/billing/premium_request/usage`
- `/enterprises/{enterprise}/settings/billing/usage`
- `/enterprises/{enterprise}/settings/billing/usage/summary`

## Deploy
1. Replace placeholders.
2. Build and push the image.
3. Update the image tag in `k8s/02-deployment.yaml`.
4. Apply through Argo CD or `kubectl apply -k k8s/`.
5. Verify logs and dashboard queries.

## 30-day backfill
For a controlled 30-day backfill, temporarily set:
- `ENABLE_DATE_RANGE_BACKFILL=true`
- `BACKFILL_START_DAY=YYYY-MM-DD`
- `BACKFILL_END_DAY=YYYY-MM-DD`

Keep:
- `BOOTSTRAP_28D=false`
- `FORCE_BOOTSTRAP=false`

Turn backfill back off immediately after completion.
