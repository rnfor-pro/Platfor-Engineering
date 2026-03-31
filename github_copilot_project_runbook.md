# GitHub Copilot Enterprise Metrics Project Runbook

## Purpose

This project collects GitHub Copilot metrics, stores them in VictoriaMetrics, and visualizes them in Grafana.

It supports:
- Enterprise Copilot usage metrics
- Enterprise code generation metrics
- Enterprise seat snapshot metrics
- Grafana dashboards for leadership and engineering teams

---

## High-Level Architecture

```text
GitHub Copilot Enterprise APIs
        |
        v
github-copilot-exporter (Python app running in Kubernetes)
        |
        +--> pulls enterprise usage reports
        +--> pulls enterprise user usage reports
        +--> pulls enterprise seat snapshot
        +--> transforms data into VictoriaMetrics JSON line format
        |
        v
VictoriaMetrics
        |
        v
Grafana dashboards
```

Health metrics from the exporter can also be scraped by Alloy if needed.

---

## Components Used

### Kubernetes
Runs the exporter as a Deployment in the `dev-keystone` namespace.

### GitHub Copilot APIs
Used as the source of:
- Enterprise usage metrics
- Enterprise user usage metrics
- Enterprise seat metrics

### VictoriaMetrics
Stores:
- historical enterprise usage metrics
- historical code generation metrics
- enterprise seat snapshots

### Grafana
Reads from VictoriaMetrics and renders:
- Enterprise Usage dashboard
- Enterprise Code Generation dashboard

### Argo CD
Deploys the Kubernetes manifests from the `obseng-keystone-infra` repository.

---

## Repository Layout

```text
obseng-keystone-infra/
└── github-copilot-insights/
    ├── app/
    │   ├── main.py
    │   ├── requirements.txt
    │   └── Dockerfile
    └── k8s/
        ├── 01-secret.yaml
        ├── 02-deployment.yaml
        └── kustomization.yaml
```

> In your setup, `03-service.yaml` was merged into `02-deployment.yaml`, and `04-networkpolicy.yaml` was removed because it was not needed.

---

## What Each File Does

### `app/main.py`
This is the collector application. It:
- calls GitHub enterprise Copilot APIs
- downloads daily and 28-day reports
- parses JSON / NDJSON report content
- transforms it into VictoriaMetrics import lines
- writes metrics into VictoriaMetrics
- exports internal health metrics on `/metrics`

### `app/requirements.txt`
Contains Python dependencies:
- `requests`
- `prometheus-client`

### `app/Dockerfile`
Builds the exporter image and includes:
- Python runtime
- required packages
- internal certificate handling if needed
- non-root runtime user

### `k8s/01-secret.yaml`
Stores the GitHub token only:
- `GH_TOKEN`

### `k8s/02-deployment.yaml`
Defines:
- the Deployment
- the Service
- environment variables
- probes
- security context

### `k8s/kustomization.yaml`
Lets Argo CD render the app from the `k8s` folder.

---

## Environment Variables Used

### Required
- `GH_TOKEN`
- `GH_ENTERPRISE`
- `VM_IMPORT_URL`
- `VM_SERIES_URL`

### Common runtime settings
- `GH_API_BASE`
- `GH_API_VERSION`
- `DATA_LAG_DAYS`
- `POLL_INTERVAL_SECONDS`
- `EXPORTER_PORT`
- `LOG_LEVEL`

### Optional backfill / bootstrap settings
- `BOOTSTRAP_28D`
- `FORCE_BOOTSTRAP`
- `ENABLE_DATE_RANGE_BACKFILL`
- `BACKFILL_START_DAY`
- `BACKFILL_END_DAY`

---

## Data Flow

### 1. GitHub creates usage data
When developers use Copilot, GitHub later makes the activity available through enterprise-level usage reports.

### 2. The exporter fetches the data
The app fetches:
- enterprise 28-day report
- enterprise 1-day report
- enterprise users 28-day report
- enterprise users 1-day report
- enterprise seats snapshot

### 3. The exporter transforms the data
The app converts GitHub report fields into time-series metrics such as:
- `github_copilot_daily_active_users`
- `github_copilot_weekly_active_users`
- `github_copilot_monthly_active_users`
- `github_copilot_code_generation_activity_count`
- `github_copilot_code_acceptance_activity_count`
- `github_copilot_loc_added_sum`
- `github_copilot_loc_deleted_sum`
- `github_copilot_enterprise_seat_total`

### 4. VictoriaMetrics stores the data
The exporter writes the transformed metrics to VictoriaMetrics using the JSON-line import endpoint.

### 5. Grafana displays the data
Grafana dashboards query VictoriaMetrics and render the results.

---

## Deployment Flow

### Build and push the image

```bash
cd obseng-keystone-infra/github-copilot-insights/app
docker build -t YOUR_REGISTRY/github-copilot-exporter:v-enterprise-backfill .
docker push YOUR_REGISTRY/github-copilot-exporter:v-enterprise-backfill
```

### Update the manifest
In `02-deployment.yaml`, update the image tag to the new version.

### Deploy through Argo CD
Because Argo CD is already watching `obseng-keystone-infra`, commit and push the manifest changes to Git and let Argo sync them.

If testing manually:

```bash
kubectl apply -f obseng-keystone-infra/github-copilot-insights/k8s/02-deployment.yaml
kubectl -n dev-keystone rollout restart deployment/github-copilot-exporter
kubectl -n dev-keystone rollout status deployment/github-copilot-exporter
```

### Watch logs

```bash
kubectl -n dev-keystone logs deployment/github-copilot-exporter -f
```

---

## Grafana Dashboards Delivered

### 1. Enterprise Usage dashboard
Purpose:
- adoption trends
- DAU / WAU / MAU
- chat / agent / CLI usage
- prompts per active user
- IDE / feature / language usage
- seat totals

### 2. Enterprise Code Generation dashboard
Purpose:
- lines of code changed with AI
- code generation vs acceptance
- lines added / deleted
- feature breakdown
- IDE breakdown
- model and language breakdowns
- PR activity

---

## Backfill Strategy

### Why backfill was needed
The dashboards were set to `Last 60 days`, but the backend only had a small amount of data. That made the graphs appear as tiny spikes.

Also, model and language panels were blank because the app did not originally export those metric families.

### How backfill works
A date-range backfill loops day by day and imports:
- enterprise daily usage report
- enterprise daily user usage report

This is the correct way to make February and March appear together.

### Recommended backfill example
- `ENABLE_DATE_RANGE_BACKFILL=true`
- `BACKFILL_START_DAY=2026-02-01`
- `BACKFILL_END_DAY=2026-03-29`

### Important
After the backfill completes, turn the date-range backfill flags back off.

---

## Commands Used for VictoriaMetrics Maintenance

### Port-forward VictoriaMetrics

```bash
kubectl -n dev-keystone port-forward svc/dev-victoriametrics-victoria-metrics-single-server 8428:8428
```

### Preview matching series

```bash
curl -s -X POST -g "http://127.0.0.1:8428/prometheus/api/v1/series"   --data-urlencode "match[]={__name__=~\"github_copilot_.*\",enterprise=\"YOUR_ENTERPRISE_SLUG\"}"   --data-urlencode "start=-90d"
```

### Export backup

```bash
curl -s -X POST -g "http://127.0.0.1:8428/api/v1/export"   --data-urlencode "match[]={__name__=~\"github_copilot_.*\",enterprise=\"YOUR_ENTERPRISE_SLUG\"}"   > github-copilot-enterprise-backup.jsonl
```

### Delete existing enterprise Copilot metrics

```bash
curl -v -X POST -g "http://127.0.0.1:8428/api/v1/admin/tsdb/delete_series"   --data-urlencode "match[]={__name__=~\"github_copilot_.*\",enterprise=\"YOUR_ENTERPRISE_SLUG\"}"
```

### Reset rollup cache

```bash
curl -Is http://127.0.0.1:8428/internal/resetRollupResultCache
```

---

## Issues We Hit and How They Were Resolved

### 1. GitHub API 401 Unauthorized
**Problem**  
The exporter failed with 401 errors while calling GitHub Copilot endpoints.

**Cause**  
The token in Kubernetes did not match the working token that succeeded outside Kubernetes.

**Resolution**  
- verified the token outside Kubernetes first
- checked the Secret value inside the pod
- corrected the token stored in Kubernetes

### 2. `curl: not found`
**Problem**  
Some debug commands failed because `curl` was missing in the container.

**Resolution**  
Used Python and Kubernetes-based checks instead of relying on `curl` inside the pod.

### 3. Certificate verification failed
**Problem**  
The exporter failed to download signed report URLs because the certificate chain could not be verified.

**Cause**  
The runtime image did not trust the required internal CA chain.

**Resolution**  
- updated the Dockerfile to include the internal CA certificates
- set:
  - `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`
  - `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`

### 4. Azure signed download links returned 401
**Problem**  
The exporter hit 401 errors when following GitHub report `download_links`.

**Cause**  
GitHub headers / auth were being reused against Azure-hosted signed URLs.

**Resolution**  
Used a separate unauthenticated session for the signed report downloads.

### 5. Duplicate data from repeated bootstrap
**Problem**  
Daily and monthly active user values became inflated.

**Cause**  
The app was run multiple times with:
- `BOOTSTRAP_28D=true`

That caused overlapping historical imports.

**Resolution**  
- deleted existing Copilot metrics from VictoriaMetrics
- re-imported cleanly
- changed the app so it checks VictoriaMetrics before importing again
- added idempotent logic for daily imports and bootstrap

### 6. Delete command failed with `missing 'match[]' arg`
**Problem**  
The VictoriaMetrics delete command failed.

**Cause**  
The command formatting was wrong in Git Bash / MINGW and `match[]` was not passed correctly.

**Resolution**  
Used one-line commands with:
- `-g`
- `--data-urlencode`

### 7. CrashLoopBackOff after enterprise changes
**Problem**  
The pod crashlooped after switching to enterprise mode.

**Likely cause**  
Missing required environment variables such as `GH_ENTERPRISE`.

**Resolution**  
Checked logs, added the missing env var, and redeployed.

### 8. Time-series charts looked tiny
**Problem**  
The points on the 60-day charts were very small.

**Cause**  
The backend only had a small number of historical daily points.

**Resolution**  
Added date-range backfill to import February and March day by day.

### 9. Model and language panels were blank
**Problem**  
The dashboards imported successfully, but model/language panels showed no data.

**Cause**  
The exporter was not writing these enterprise metrics:
- `github_copilot_user_model_feature_prompt_count`
- `github_copilot_user_model_feature_loc_added_sum`
- `github_copilot_user_language_feature_loc_added_sum`

**Resolution**  
Updated `main.py` to export those metric families and reimported the affected date range.

### 10. Enterprise seat total disappeared on larger time ranges
**Problem**  
Enterprise seat total only showed for recent ranges.

**Cause**  
Seat total is a current snapshot metric, not a historical day-based report. Older history had either not been collected yet or had been deleted.

**Resolution**  
- confirmed seat snapshot imports in logs
- used `last_over_time(...)` in the dashboard
- let history accumulate over time

---

## Operational Guidance

### When to run commands outside the pod
Run these outside the pod:
- `kubectl ...`
- `kubectl port-forward ...`
- `docker build`
- `docker push`
- `curl http://127.0.0.1:8428/...`

### When to run commands inside the pod
Run inside the pod only when you need:
- environment checks
- runtime debugging
- internal DNS / app-level checks

Example:

```bash
kubectl -n dev-keystone exec -it deploy/github-copilot-exporter -- printenv GH_ENTERPRISE
```

---

## Recommended Safe Defaults

Use these in normal steady state:

```text
BOOTSTRAP_28D=false
FORCE_BOOTSTRAP=false
ENABLE_DATE_RANGE_BACKFILL=false
BACKFILL_START_DAY=
BACKFILL_END_DAY=
```

Turn them on only for:
- one-time bootstrap
- one-time recovery
- one-time date-range backfill

---

## How to Know Things Are Working

### Exporter logs should show
- `Imported enterprise seat snapshot`
- `Imported enterprise day=...`
- `Completed enterprise date-range backfill ...`

### Grafana Explore checks

#### Verify enough daily history exists

```promql
count_over_time(github_copilot_daily_active_users{enterprise="$enterprise"}[60d])
```

#### Verify model usage exists

```promql
sum by (model) (sum_over_time(github_copilot_user_model_feature_prompt_count{enterprise="$enterprise"}[60d]))
```

#### Verify language usage exists

```promql
sum by (language) (sum_over_time(github_copilot_user_language_feature_loc_added_sum{enterprise="$enterprise"}[60d]))
```

---

## Final Summary

This project now supports:
- enterprise-level Copilot usage metrics
- enterprise-level code generation metrics
- enterprise seat snapshot metrics
- Grafana dashboards for usage and code generation
- date-range backfill for restoring missing months
- idempotent import logic to reduce duplicates
- Argo CD-based deployment flow
- hardened container / manifest security

The most important operational rule is:

**do not leave bootstrap or backfill flags enabled after the one-time import finishes.**
