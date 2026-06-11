Use this same pattern around `127.0.0.1:8428` after port-forwarding.

Set this first:

```bash
VM_BASE="http://127.0.0.1:8428"
ENTERPRISE="sherwin-williams"
BACKUP_FILE="github_copilot_full_backup_$(date +%Y%m%d_%H%M%S).bin"
```

## 1) Preview Copilot series before deleting anything

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'start=-90d'
```

Billing summary too:

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'start=-90d'
```

## 2) Export backup before deleting

```bash
curl -s "$VM_BASE/api/v1/export/native" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}' \
  -o "$BACKUP_FILE"
```

Confirm backup exists:

```bash
ls -lh "$BACKUP_FILE"
```

## 3) Delete only Copilot and expected related series

```bash
curl -v -X POST -g "$VM_BASE/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}'
```

## 4) Clear cache immediately after delete

```bash
curl -sS "$VM_BASE/internal/resetRollupResultCache"
```

## 5) Validate delete worked

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'start=-90d'
```

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'start=-90d'
```

## 6) Rerun one clean bootstrap / backfill

After delete, deploy the new exporter with:

* `BOOTSTRAP_28D=true` only if you really want bootstrap
* or preferred: `ENABLE_DATE_RANGE_BACKFILL=true` with your date range
* keep `FORCE_BOOTSTRAP=false`

Then watch logs:

```bash
kubectl -n dev-keystone logs deployment/github-copilot-exporter -f
```

## 7) Turn bootstrap off again immediately after it completes

Set back to:

```yaml
BOOTSTRAP_28D=false
ENABLE_DATE_RANGE_BACKFILL=false
BACKFILL_START_DAY=""
BACKFILL_END_DAY=""
```

Sync again.

## 8) Validate that data is back

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'start=-90d'
```

A few spot checks:

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/query" \
  --data-urlencode 'query=max(github_copilot_monthly_active_users{enterprise="'"$ENTERPRISE"'"})'
```

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/query" \
  --data-urlencode 'query=max(github_copilot_enterprise_seat_total{enterprise="'"$ENTERPRISE"'"})'
```

```bash
curl -s -X POST -g "$VM_BASE/prometheus/api/v1/query" \
  --data-urlencode 'query=max(github_copilot_enterprise_seat_active_last_28d_total{enterprise="'"$ENTERPRISE"'"})'
```

## 9) Restore backup if something goes wrong

```bash
curl -sS -H 'Content-Type: application/octet-stream' \
  --data-binary @"$BACKUP_FILE" \
  "$VM_BASE/api/v1/import/native"
```

Then clear cache again:

```bash
curl -sS "$VM_BASE/internal/resetRollupResultCache"
```

## 10) Delete backup if everything works

```bash
rm "$BACKUP_FILE"
```

One correction to the old command: use `enterprise="$ENTERPRISE"` for your current exporter/dashboards, not `org=...`, unless you are intentionally targeting older org-labeled series.







=======================

Use Python instead.

Inside the pod:

### Backup

```bash
python - <<'PY'
import requests

VM_BASE = "http://dev-victoriametrics-victoria-metrics-single-server.dev-keystone.svc.cluster.local:8428"
ENTERPRISE = "YOUR_ENTERPRISE"

data = [
    ("match[]", f'{{__name__=~"github_copilot_.*",enterprise="{ENTERPRISE}"}}'),
    ("match[]", f'{{__name__=~"github_billing_usage_summary_.*",enterprise="{ENTERPRISE}"}}'),
]

r = requests.post(f"{VM_BASE}/api/v1/export/native", data=data, timeout=300)
r.raise_for_status()
with open("/tmp/github_copilot_full_backup.bin", "wb") as f:
    f.write(r.content)

print("backup written to /tmp/github_copilot_full_backup.bin")
PY
```

### Delete

```bash
python - <<'PY'
import requests

VM_BASE = "http://dev-victoriametrics-victoria-metrics-single-server.dev-keystone.svc.cluster.local:8428"
ENTERPRISE = "YOUR_ENTERPRISE"

data = [
    ("match[]", f'{{__name__=~"github_copilot_.*",enterprise="{ENTERPRISE}"}}'),
    ("match[]", f'{{__name__=~"github_billing_usage_summary_.*",enterprise="{ENTERPRISE}"}}'),
]

r = requests.post(f"{VM_BASE}/api/v1/admin/tsdb/delete_series", data=data, timeout=300)
r.raise_for_status()
print(r.text if r.text else "delete request sent successfully")
PY
```

### Restore

```bash
python - <<'PY'
import requests

VM_BASE = "http://dev-victoriametrics-victoria-metrics-single-server.dev-keystone.svc.cluster.local:8428"

with open("/tmp/github_copilot_full_backup.bin", "rb") as f:
    r = requests.post(
        f"{VM_BASE}/api/v1/import/native",
        data=f,
        headers={"Content-Type": "application/octet-stream"},
        timeout=300,
    )
r.raise_for_status()
print("restore completed")
PY
```

### Reset cache

```bash
python - <<'PY'
import requests
VM_BASE = "http://dev-victoriametrics-victoria-metrics-single-server.dev-keystone.svc.cluster.local:8428"
r = requests.get(f"{VM_BASE}/internal/resetRollupResultCache", timeout=60)
print(r.status_code)
print(r.text)
PY
```

### Verify backup file exists

```bash
ls -lh /tmp/github_copilot_full_backup.bin
```

Replace only:

* `YOUR_ENTERPRISE`

Use the pod that already has Python and `requests`, like your exporter pod.







That error means your machine cannot resolve the **Kubernetes internal service DNS**.

You used:

```bash
dev-victoriametrics-victoria-metrics-single-server.dev-keystone.svc.cluster.local
```

That only resolves **from inside the cluster**, not from your laptop.

Use one of these two ways.

## Option 1 — run the curl from inside a pod

This avoids port-forwarding.

```bash
kubectl -n dev-keystone exec -it deploy/github-copilot-exporter -- sh
```

Then inside the pod:

```bash
VM_BASE="http://dev-victoriametrics-victoria-metrics-single-server.dev-keystone.svc.cluster.local:8428"
ENTERPRISE="YOUR_ENTERPRISE"
```

Backup:

```bash
curl -sS "$VM_BASE/api/v1/export/native" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}' \
  -o /tmp/github_copilot_full_backup.bin
```

Delete:

```bash
curl -sS -X POST "$VM_BASE/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}'
```

Restore if needed:

```bash
curl -sS -H 'Content-Type: application/octet-stream' \
  --data-binary @/tmp/github_copilot_full_backup.bin \
  "$VM_BASE/api/v1/import/native"
```

Reset cache:

```bash
curl -Is "$VM_BASE/internal/resetRollupResultCache"
```

## Option 2 — use a reachable URL from your laptop

If you already have an external/internal reachable URL for VictoriaMetrics, use that instead of the `.svc.cluster.local` name.

## One more thing

Your screenshot also shows this typo pattern earlier:

```text
...single-server.namespace.svc.cluster.local.port/api/v1/import
```

That is invalid.

It must be:

```text
http://SERVICE.NAMESPACE.svc.cluster.local:8428
```

not:

```text
http://SERVICE.NAMESPACE.svc.cluster.local.port
```

So the real issue is:

* **not a curl problem**
* **not a VictoriaMetrics problem**
* **you are running a cluster-internal DNS name from outside the cluster**

The best path for you is **Option 1**.




A **200 OK** on both:

* enterprise **AI credit** usage
* enterprise **premium request** usage

means your token now appears to have **enterprise billing access**.

## What I think now

### Good

* your **billing-side auth looks fixed**
* the token can now reach the enterprise billing endpoints

### Still likely broken

Your earlier exporter logs were failing on:

```text
/enterprises/.../copilot/metrics/reports/enterprise-1-day
```

That is the **enterprise Copilot usage metrics** endpoint, not the billing endpoint.

So right now the most likely state is:

* **billing access = working**
* **enterprise usage metrics access = still not confirmed**
* dashboards can still be empty if usage/report endpoints are still failing

---

## Important note about the `FileNotFoundError`

That error is now **not the real issue**.

Since you got `HTTP/1.1 200 OK`, the API call itself succeeded.
The Python `FileNotFoundError` is just because the output file was not where the second command expected it.

So treat it like this:

* **200 OK = success**
* **FileNotFoundError = local shell/file-path problem**

---

## What I would do next

### 1. Re-test the actual failing endpoint from the exporter logs

This is the one that matters most right now:

```bash
export GH_TOKEN='REPLACE_WITH_TOKEN'
export GH_ENTERPRISE='REPLACE_WITH_ENTERPRISE'
export DAY='2026-06-07'

curl -sS -D ent_headers.txt -o ent_meta.json \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/enterprises/${GH_ENTERPRISE}/copilot/metrics/reports/enterprise-1-day?day=${DAY}"

grep HTTP ent_headers.txt
cat ent_meta.json
```

Do **not** use `/tmp` for now. Use files in your current folder like:

* `ent_headers.txt`
* `ent_meta.json`

If this returns `200`, then the usage-side auth is fixed too.

---

### 2. Test the user usage endpoint too

```bash
curl -sS -D users_headers.txt -o users_meta.json \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/enterprises/${GH_ENTERPRISE}/copilot/metrics/reports/users-1-day?day=${DAY}"

grep HTTP users_headers.txt
cat users_meta.json
```

---

### 3. If both return 200, restart the exporter

Because your pod logs were still showing the old 401 failure.

```bash
kubectl -n dev-keystone rollout restart deployment/github-copilot-exporter
kubectl -n dev-keystone rollout status deployment/github-copilot-exporter
kubectl -n dev-keystone logs deployment/github-copilot-exporter -f
```

What you want to see now:

* no more `401 Unauthorized`
* log lines showing imports or backfill progress

---

## My conclusion

You have likely fixed **enterprise billing permissions**, but you have **not yet proven** that the token also works for the **enterprise Copilot usage metrics** endpoints, which are the ones your exporter was crashing on.

So the next checkpoint is simple:

* if `enterprise-1-day` returns **200**
* and `users-1-day` returns **200**

then your auth issue is likely solved and the exporter just needs to be restarted and watched.

Paste me the output of those two exact tests and I’ll tell you whether you’re clear to rerun the backfill.
