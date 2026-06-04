# GitHub Copilot Metrics Platform — Exact-Diff Rebuild Manual

## 1. What changed in this rebuild

This rebuild changes the exporter from a coarse day-level ingest model to a record-level exact-diff model.

### Old approach
The previous exporter mostly decided whether to skip or import based on day-level markers such as:
- enterprise daily active users exist
- user daily record exists

That was good enough for simple daily ingestion, but it was not ideal when:
- new GitHub fields appeared later
- only one metric family was missing for a day
- backfills had to be rerun safely
- billing and cohort metrics were added after earlier data had already been imported

### New approach
The rebuilt exporter:
1. fetches GitHub source reports
2. parses them into canonical metric points
3. creates a deterministic key for every point
4. reads existing raw points from VictoriaMetrics for the same day and metric family
5. compares source points to stored points
6. writes only missing records

This makes normal runs, bootstrap, and backfill follow the same core logic.

---

## 2. Architecture

```text
GitHub Copilot enterprise usage reports
GitHub Copilot user usage reports
GitHub Copilot user-teams reports
GitHub organization billing usage reports
            ↓
        Exact-diff exporter
            ↓
     VictoriaMetrics import/export
            ↓
           Grafana
```

### Scope model
- Enterprise usage is collected at the enterprise level.
- Billing is collected per organization.
- Seat snapshots are collected at the enterprise level and stored as daily snapshots.

---

## 3. GitHub endpoints used

### Enterprise usage metadata
- `GET /enterprises/{enterprise}/copilot/metrics/reports/enterprise-1-day`
- `GET /enterprises/{enterprise}/copilot/metrics/reports/enterprise-28-day/latest`
- `GET /enterprises/{enterprise}/copilot/metrics/reports/users-1-day`
- `GET /enterprises/{enterprise}/copilot/metrics/reports/users-28-day/latest`
- `GET /enterprises/{enterprise}/copilot/metrics/reports/user-teams-1-day`

### Organization billing metadata
- `GET /organizations/{org}/settings/billing/premium_request/usage`
- `GET /organizations/{org}/settings/billing/ai_credit/usage`

### Important URL note
Do not hardcode signed report download hosts.
Always follow the `download_links` returned by the GitHub API metadata response.

---

## 4. Environment variables

### Required
- `GH_TOKEN`
- `GH_ENTERPRISE`
- `VM_IMPORT_URL`
- `VM_EXPORT_URL`

### Strongly recommended
- `GH_BILLING_ORGS`
- `ENABLE_BILLING_REPORTS=true`
- `VM_SERIES_URL`
- `VM_DELETE_URL`

### Runtime behavior
- `DATA_LAG_DAYS=2`
- `POLL_INTERVAL_SECONDS=21600`
- `EXACT_DIFF_ENABLED=true`
- `DRIFT_POLICY=skip`
- `DRY_RUN=false`

### Optional operational controls
- `BOOTSTRAP_28D`
- `FORCE_BOOTSTRAP`
- `ENABLE_DATE_RANGE_BACKFILL`
- `BACKFILL_START_DAY`
- `BACKFILL_END_DAY`

---

## 5. How exact diff works

Every normalized metric point is converted into a deterministic key using:
- metric name
- timestamp
- full sorted label set

The exporter then:
1. groups points by day and family
2. asks VictoriaMetrics for existing raw samples for the same day / family
3. compares keys
4. skips points already present
5. imports only missing records

### What happens if the same key exists with a different value?
That is treated as drift.
Current policy:
- log drift
- count drift
- skip by default unless `DRIFT_POLICY=rewrite`

---

## 6. Metric families

The exporter handles these families independently:
- `enterprise_usage`
- `user_usage`
- `user_teams`
- `seat_snapshot`
- `billing_premium_request:{org}`
- `billing_ai_credit:{org}`

This allows more precise repair and cleaner backfills.

---

## 7. How to deploy

## Step 1 — Save the rebuilt exporter
Replace your app entrypoint with the rebuilt exporter file.

Example:
- copy `main_exact_diff_rebuilt.py` to your app folder as `main.py`
- or update your container to run it directly

## Step 2 — Confirm requirements
The rebuilt exporter uses:
- `requests`
- `prometheus-client`

Your `requirements.txt` should contain:

```txt
requests==2.32.3
prometheus-client==0.21.1
```

## Step 3 — Build the image

```bash
cd obseng-keystone-infra/github-copilot-insights/app
docker build -t YOUR_ARTIFACTORY/github-copilot-exporter:v-exact-diff-1 .
docker push YOUR_ARTIFACTORY/github-copilot-exporter:v-exact-diff-1
```

## Step 4 — Update the Deployment image tag
Update the image tag in your deployment YAML or values file.

## Step 5 — Set environment variables
Make sure these are present in the Deployment:

```yaml
- name: GH_ENTERPRISE
  value: "your-enterprise"

- name: GH_BILLING_ORGS
  value: "org-a,org-b,org-c"

- name: ENABLE_BILLING_REPORTS
  value: "true"

- name: VM_IMPORT_URL
  value: "http://YOUR_VM/api/v1/import"

- name: VM_EXPORT_URL
  value: "http://YOUR_VM/api/v1/export"

- name: DATA_LAG_DAYS
  value: "2"

- name: POLL_INTERVAL_SECONDS
  value: "21600"

- name: EXACT_DIFF_ENABLED
  value: "true"

- name: DRIFT_POLICY
  value: "skip"

- name: DRY_RUN
  value: "false"

- name: BOOTSTRAP_28D
  value: "false"

- name: FORCE_BOOTSTRAP
  value: "false"

- name: ENABLE_DATE_RANGE_BACKFILL
  value: "false"

- name: BACKFILL_START_DAY
  value: ""

- name: BACKFILL_END_DAY
  value: ""
```

## Step 6 — Deploy
If you use Argo CD:
- commit and push the manifest change
- sync the application

If testing manually:

```bash
kubectl apply -f YOUR_DEPLOYMENT_FILE.yaml
kubectl -n YOUR_NAMESPACE rollout restart deployment/github-copilot-exporter
kubectl -n YOUR_NAMESPACE rollout status deployment/github-copilot-exporter
kubectl -n YOUR_NAMESPACE logs deployment/github-copilot-exporter -f
```

---

## 8. How to run a safe 30-day backfill

### Best practice
Use a narrow window and keep bootstrap disabled.

Example:

```yaml
- name: ENABLE_DATE_RANGE_BACKFILL
  value: "true"

- name: BACKFILL_START_DAY
  value: "2026-05-05"

- name: BACKFILL_END_DAY
  value: "2026-06-03"

- name: BOOTSTRAP_28D
  value: "false"

- name: FORCE_BOOTSTRAP
  value: "false"
```

Apply and restart the Deployment.

Watch logs until the backfill completes.

Then immediately revert to:

```yaml
- name: ENABLE_DATE_RANGE_BACKFILL
  value: "false"

- name: BACKFILL_START_DAY
  value: ""

- name: BACKFILL_END_DAY
  value: ""
```

and redeploy.

---

## 9. How to validate after deployment

### Validate the process is running
```bash
kubectl -n YOUR_NAMESPACE logs deployment/github-copilot-exporter -f
```

### Validate that the latest daily usage exists
```promql
count_over_time(github_copilot_daily_active_users{enterprise="$enterprise"}[30d])
```

### Validate cohort metrics
```promql
sum by (ai_adoption_phase) (
  last_over_time(github_copilot_ai_adoption_phase_user_count{enterprise="$enterprise"}[30d])
)
```

### Validate AI credits
```promql
sum by (org) (
  sum_over_time(github_copilot_billing_ai_credit_total_net_quantity{enterprise="$enterprise"}[30d])
)
```

### Validate premium requests
```promql
sum by (org) (
  sum_over_time(github_copilot_billing_premium_request_total_net_quantity{enterprise="$enterprise"}[30d])
)
```

---

## 10. Dashboard strategy

Use four dashboards with minimal overlap:

1. **Executive Overview**
   - seats
   - active seats
   - MAU
   - acceptance rate
   - AI credit cost
   - LoC changed
   - high-level trends

2. **Usage & Adoption Deep Dive**
   - DAU / WAU / MAU
   - chat / agent / CLI adoption
   - prompt depth
   - feature / IDE / model / language usage
   - top users
   - cohorts

3. **Code Generation & Delivery Impact**
   - code generation
   - code acceptance
   - lines added / deleted
   - feature impact
   - IDE impact
   - model impact
   - PR metrics

4. **Billing, Seats, Teams & Cohorts**
   - seats
   - seat plans
   - assigning teams
   - premium requests
   - AI credits
   - team membership
   - cohort distribution

---

## 11. What to do if GitHub adds more fields later

Because this rebuild normalizes source data into metric families and uses exact diff:
- add the new field to the correct normalizer
- add dashboard panels only if the field answers a new question
- backfill the relevant date range
- no large delete-and-reimport should be needed unless values drift

This is the main advantage of the rebuilt architecture.