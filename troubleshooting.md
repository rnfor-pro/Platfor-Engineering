Most likely, the problem is **before Grafana**. If **all dashboards** show no data, the usual causes are:

1. the exporter is not running correctly
2. GitHub API access is failing
3. data is not getting written to VictoriaMetrics
4. Grafana is pointed at the wrong datasource / variable value

Use this order.

## 1) Check the exporter pod first

```bash
kubectl -n YOUR_NAMESPACE get pods -l app=github-copilot-exporter
kubectl -n YOUR_NAMESPACE logs deploy/github-copilot-exporter --since=2h
```

What you want to see:

* no crash loop
* log lines like:

  * `Starting GitHub Copilot enterprise collector`
  * `Imported enterprise day=...`
  * `Imported enterprise seat snapshot...`
  * `Imported billing usage...`

If you see:

* `401` or `403` → token / permissions issue
* `404` → wrong enterprise slug or wrong endpoint
* SSL errors → cert / CA problem
* import errors → VictoriaMetrics URL/auth issue

---

## 2) Check the runtime env values actually loaded into the pod

```bash
kubectl -n YOUR_NAMESPACE exec deploy/github-copilot-exporter -- sh -c 'env | egrep "GH_ENTERPRISE|GH_API_BASE|GH_API_VERSION|BILLING_SCOPE|ENABLE_BILLING_REPORTS|ENABLE_BILLING_USAGE_SUMMARY|VM_IMPORT_URL|VM_EXPORT_URL|DATA_LAG_DAYS|POLL_INTERVAL_SECONDS|ENABLE_DATE_RANGE_BACKFILL|BACKFILL_START_DAY|BACKFILL_END_DAY|EXPORTER_PORT|LOG_LEVEL"'
```

Most common mistakes:

* wrong `GH_ENTERPRISE`
* wrong `VM_IMPORT_URL`
* wrong `VM_EXPORT_URL`
* `GH_API_BASE` wrong for your GitHub type
* billing flags disabled
* backfill off when you expected it on

---

## 3) Test GitHub enterprise usage access directly

```bash
export GH_TOKEN='REPLACE_WITH_TOKEN'
export GH_ENTERPRISE='REPLACE_WITH_ENTERPRISE'
export DAY='2026-06-07'

curl -sS -D /tmp/ent_headers.txt -o /tmp/ent_meta.json \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/enterprises/${GH_ENTERPRISE}/copilot/metrics/reports/enterprise-1-day?day=${DAY}"

grep HTTP /tmp/ent_headers.txt
python - <<'PY'
import json
meta = json.load(open("/tmp/ent_meta.json"))
print("download_links:", len(meta.get("download_links", [])))
print("report_day:", meta.get("report_day"))
print(meta)
PY
```

Expected:

* HTTP `200`
* `download_links` > 0

If not, the dashboards will be empty because usage data never got fetched.

---

## 4) Test enterprise billing access directly

If you switched to enterprise billing endpoints, this is a very common failure point.

```bash
curl -sS -D /tmp/ai_headers.txt -o /tmp/ai.json \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/enterprises/${GH_ENTERPRISE}/settings/billing/ai_credit/usage?year=2026&month=6"

grep HTTP /tmp/ai_headers.txt
python - <<'PY'
import json
data = json.load(open("/tmp/ai.json"))
print(data)
PY
```

```bash
curl -sS -D /tmp/pr_headers.txt -o /tmp/pr.json \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/enterprises/${GH_ENTERPRISE}/settings/billing/premium_request/usage?year=2026&month=6"

grep HTTP /tmp/pr_headers.txt
python - <<'PY'
import json
data = json.load(open("/tmp/pr.json"))
print(data)
PY
```

If these fail:

* your token may not be an enterprise admin / billing manager token
* or you may be using the wrong GitHub API host

---

## 5) Check whether VictoriaMetrics actually has the data

This is the fastest truth test.

```bash
kubectl -n YOUR_NAMESPACE port-forward svc/YOUR_VM_SERVICE 8428:8428
```

Then in another terminal:

```bash
curl -s -X POST -g "http://127.0.0.1:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__="github_copilot_daily_active_users",enterprise="YOUR_ENTERPRISE"}' \
  --data-urlencode 'start=-30d'
```

Also try:

```bash
curl -s -X POST -g "http://127.0.0.1:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__="github_copilot_enterprise_seat_total",enterprise="YOUR_ENTERPRISE"}' \
  --data-urlencode 'start=-30d'
```

If these return nothing, Grafana is not the problem.
It means the exporter never wrote the data.

---

## 6) Check Grafana variable population

In Grafana Explore, try:

```promql
label_values(github_copilot_enterprise_seat_total, enterprise)
```

If that returns nothing:

* the `enterprise` variable in your dashboards will be empty
* then almost every panel will show no data

This is one of the most common causes when dashboards import successfully but stay blank.

---

## 7) Check the most likely issue based on “all dashboards are empty”

If **everything** is blank, the most likely causes are:

### Most likely

* wrong `GH_ENTERPRISE`
* exporter failed to start
* VictoriaMetrics import URL is wrong
* token cannot read enterprise usage APIs
* you are querying a different VM datasource than the one the exporter writes to

### Less likely

* dashboard JSON issue
* panel query issue

If even `github_copilot_enterprise_seat_total` is missing, the issue is almost certainly **not the dashboard**.

---

## 8) Cohort and billing can be empty for additional reasons

Even if usage works:

### Cohorts empty

* the report for that day may not contain `ai_adoption_phase`
* your users may not yet qualify into phases
* you may not have backfilled enough post-5/29 data

### Billing empty

* enterprise billing endpoint permissions missing
* wrong endpoint for your GitHub environment
* no usage returned for the selected period

But again, if **all** dashboards are blank, start with usage + VM import first.

---

## 9) Fastest end-to-end checks

Run these in order:

```bash
kubectl -n YOUR_NAMESPACE logs deploy/github-copilot-exporter --since=2h
```

```bash
kubectl -n YOUR_NAMESPACE exec deploy/github-copilot-exporter -- sh -c 'env | egrep "GH_ENTERPRISE|VM_IMPORT_URL|VM_EXPORT_URL|BILLING_SCOPE|ENABLE_BILLING_REPORTS"'
```

```bash
curl -sS -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/enterprises/${GH_ENTERPRISE}/copilot/metrics/reports/enterprise-1-day?day=2026-06-07"
```

```bash
curl -s -X POST -g "http://127.0.0.1:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__="github_copilot_daily_active_users",enterprise="YOUR_ENTERPRISE"}' \
  --data-urlencode 'start=-30d'
```

---

## My strongest guess

If you are using the new enterprise-first exporter, the most likely issue is one of these:

* **enterprise billing token permissions are insufficient**
* **wrong enterprise slug**
* **VM import/export URL mismatch**
* **dashboard variable `enterprise` has no values because `github_copilot_enterprise_seat_total` never got written**

Paste me:

1. the last 50 exporter log lines
2. the output of the env check
3. whether the `series` query above returns anything

and I’ll tell you exactly where the break is.
