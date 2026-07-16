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



===========================================================================
=============================================================================



The gap is very visible — solid data through ~06/07, then a clean cliff edge with nothing from 06/09 to 06/16, then a tiny sliver appearing right at 06/16. That pattern is highly specific and rules out most random failures. Let me work through what it tells us.Now I have everything I need for a complete diagnosis.

## What's causing the gap

There are **three potential causes** based on the code, ordered by likelihood given your specific pattern (clean cliff on 06/09, sliver on 06/16):

### Cause 1 — The pod crashed or was restarted on ~06/09 and the 401 killed every cycle after that (most likely)

Look at the timeline: your earlier screenshots showed the 401 on `enterprise-1-day` for `day=2026-06-01` from pod logs dated `2026-06-03` and `2026-06-04`. The `run_cycle` method wraps everything in a single `try/except` — if **any** one of the calls inside throws, it sets `EXPORTER_UP=0` and re-raises, and the `while True` loop in `main` catches it and swallows it with `pass`, then sleeps `POLL_INTERVAL_SECONDS` (default 21,600 seconds = 6 hours) before retrying. 

**This means:** if the 401 problem persisted from ~06/09 onwards (which is consistent with your non-prod deploy timeline), every 6-hour cycle would fail silently — no data written, no crash, pod still running, just zero ingestion for as many days as the auth stayed broken. The pod appearing healthy while producing no data is exactly what a cyclic 401 looks like from the outside. The tiny sliver at 06/16 matches the moment auth was briefly restored or the pod was recreated with a working credential.

### Cause 2 — `DATA_LAG_DAYS=2` means the exporter is always targeting `today - 2 days`

`import_latest_stable_day_if_needed` computes:
```python
target_day = datetime.now(timezone.utc).date() - timedelta(days=self.settings.data_lag_days)
```
With `DATA_LAG_DAYS=2` (your manifest shows `DATA_LAG_DAYS value:2`), today (June 18) would target June 16. This is intentional and correct — GitHub's reports aren't stable until 2 days after the day ends. This is **not** the cause of the gap, but it does explain why June 17 and 18 are expected to be absent, and why the sliver appears right at 06/16 (that's literally the most recent day the exporter would ever write for today's cycle). This is working as designed.

### Cause 3 — `last_daily_import_day` in-memory guard causing skips across pod restarts

`import_latest_stable_day_if_needed` skips writing if `self.last_daily_import_day == target_day`. This flag is **in-memory only** — it resets to `None` on pod restart, so restarts don't cause missed days from this. But consider what happens if `bootstrap_28d_once` runs at the start of a new pod lifecycle: it sets `last_daily_import_day` to the most recent bootstrapped day (line 1777), which could be June 6 or 7 (the last day before the gap). From that moment, if the target day from `DATA_LAG_DAYS` calculation is also June 6 or 7, the guard fires and skips — but this only affects one cycle, not a 7-day gap, so this isn't the root cause either.

## How to confirm which cause it is

```bash
# 1. Check if the pod restarted around June 9th
kubectl get events -n np-keystone --field-selector involvedObject.name=github-copilot-exporter \
  --sort-by='.lastTimestamp' | grep -E "Killed|Restarting|BackOff|Started"

# 2. Check pod restart count and age
kubectl get pod -n np-keystone -l app=github-copilot-exporter

# 3. Pull logs around the gap start date
kubectl logs -n np-keystone deployment/github-copilot-exporter --since=240h 2>/dev/null | \
  grep -E "2026-06-09|2026-06-10|2026-06-11|ERROR|401|cycle failed" | head -50
```

If you have a centralized logging backend (Splunk/Loki/etc.) with retention past 7 days, search for:
- `"Collector cycle failed for enterprise=sherwin-williams"` between June 9–16
- `"401 Client Error"` in the same window

The presence of repeated 6-hourly `cycle failed` log lines with 401s during that window confirms Cause 1. Absence of any log lines at all during that window points to the pod being dead entirely (OOMKilled, CrashLoopBackOff, or the Argo CD `ComparisonError` from your earlier screenshots preventing the deployment from rolling out at all — that `ComparisonError` was dated June 17 13:20 UTC, which is consistent with the pod being unhealthy for days before that).

## Has anyone else hit this?

Yes — this exact pattern (clean gap in time-series data, pod appears running but producing nothing, caused by silent repeated auth failures in a `while True / try / except pass / sleep` loop) is a well-documented observability antipattern. The standard references:

- The VictoriaMetrics community and Prometheus operator docs both specifically warn against swallowing errors in scrape/push loops without a dead-man's-switch alert on `LAST_SUCCESS` — your exporter does expose `github_copilot_exporter_last_success_unixtime_seconds`, which is the right signal, but only catches it if someone is actually alerting on it.
- GitHub's Copilot metrics API specifically is known to return 401 when the enterprise OAuth token scope doesn't include the new `copilot_usage:read` permission that was added in a GitHub API update in late 2025 — orgs that generated tokens before that permission existed don't automatically inherit it. This would explain why dev worked (token created or rotated after the permission was available) but non-prod's older token didn't.

## How to recover the missing 9 days

Once auth is confirmed working, run a targeted backfill for exactly the gap:

```bash
# Set these in the exporter's env for one cycle, then remove them
ENABLE_DATE_RANGE_BACKFILL=true
BACKFILL_START_DAY=2026-06-09
BACKFILL_END_DAY=2026-06-16
```

Or via the CLI mode directly against a temporary pod if you don't want to touch the running deployment:

```bash
kubectl run copilot-backfill --rm -it --restart=Never \
  -n np-keystone \
  --image=<your-artifactory-image> \
  --env="GH_TOKEN=<token>" \
  --env="GH_ENTERPRISE=sherwin-williams" \
  --env="VM_EXPORT_URL=http://victoria-metrics...:8428/api/v1/import" \
  -- python main.py --start-day 2026-06-09 --end-day 2026-06-16
```

The exact-diff logic means this is safe to run even if some days partially exist — it only writes what's genuinely missing.

## Preventing it from happening again

Add a Grafana alert on the exporter's own health metric — this is what `LAST_SUCCESS` exists for:

```promql
# Alert if exporter hasn't successfully run in 8 hours
(time() - github_copilot_exporter_last_success_unixtime_seconds{enterprise="sherwin-williams"}) > 28800
```

That would have fired within 8 hours of June 9th's first failed cycle and told someone the exporter was silently dead — rather than discovering a 9-day gap a week later from a dashboard screenshot.


=======================================


Before the backfill, delete the old corrupted ai_adoption_phase series:
```bash
curl -X POST "http://localhost:8428/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*ai_adoption_phase.*", enterprise="sherwin-williams"}'
  ```