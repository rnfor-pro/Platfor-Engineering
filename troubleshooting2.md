You can use the **same VictoriaMetrics host and port**, but **not** the `/api/v1/import` path to delete. For single-node VictoriaMetrics, backup/export uses `/api/v1/export/native`, delete uses `/api/v1/admin/tsdb/delete_series`, restore uses `/api/v1/import/native`, and after backfill/restore it is recommended to call `/internal/resetRollupResultCache`. ([docs.victoriametrics.com][1])

Your URL in the screenshot should look like this pattern, not with `.port` in the hostname:

```text
http://dev-victoriametrics-victoria-metrics-single-server.NAMESPACE.svc.cluster.local:8428
```

I’ll call that base URL:

```bash
VM_BASE="http://dev-victoriametrics-victoria-metrics-single-server.NAMESPACE.svc.cluster.local:8428"
ENTERPRISE="YOUR_ENTERPRISE"
```

## 1) Backup first

This backs up only the Copilot metrics in VictoriaMetrics native format:

```bash
curl -sS "$VM_BASE/api/v1/export/native" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  > github_copilot_backup_$(date +%Y%m%d_%H%M%S).bin
```

This backs up the billing summary family too:

```bash
curl -sS "$VM_BASE/api/v1/export/native" \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}' \
  > github_billing_summary_backup_$(date +%Y%m%d_%H%M%S).bin
```

If you want one combined backup file instead:

```bash
curl -sS "$VM_BASE/api/v1/export/native" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}' \
  > github_copilot_full_backup_$(date +%Y%m%d_%H%M%S).bin
```

## 2) Delete

Delete only the Copilot metrics:

```bash
curl -sS -X POST "$VM_BASE/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}'
```

Delete the billing summary family:

```bash
curl -sS -X POST "$VM_BASE/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}'
```

Or both in one call:

```bash
curl -sS -X POST "$VM_BASE/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  --data-urlencode 'match[]={__name__=~"github_billing_usage_summary_.*",enterprise="'"$ENTERPRISE"'"}'
```

Then clear cache:

```bash
curl -Is "$VM_BASE/internal/resetRollupResultCache"
```

## 3) Restore if needed

If the delete/backfill does not work, restore the native backup:

```bash
curl -sS -H 'Content-Type: application/octet-stream' \
  --data-binary @github_copilot_full_backup_YYYYMMDD_HHMMSS.bin \
  "$VM_BASE/api/v1/import/native"
```

Or restore each file separately:

```bash
curl -sS -H 'Content-Type: application/octet-stream' \
  --data-binary @github_copilot_backup_YYYYMMDD_HHMMSS.bin \
  "$VM_BASE/api/v1/import/native"
```

```bash
curl -sS -H 'Content-Type: application/octet-stream' \
  --data-binary @github_billing_summary_backup_YYYYMMDD_HHMMSS.bin \
  "$VM_BASE/api/v1/import/native"
```

Then clear cache again:

```bash
curl -Is "$VM_BASE/internal/resetRollupResultCache"
```

## 4) Delete the backup file if everything works

Linux/macOS/Git Bash:

```bash
rm github_copilot_full_backup_YYYYMMDD_HHMMSS.bin
```

Windows PowerShell:

```powershell
Remove-Item .\github_copilot_full_backup_YYYYMMDD_HHMMSS.bin
```

## 5) Quick verify before and after delete

Before delete:

```bash
curl -sS "$VM_BASE/api/v1/export" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}' \
  | head
```

After delete:

```bash
curl -sS "$VM_BASE/api/v1/export" \
  --data-urlencode 'match[]={__name__=~"github_copilot_.*",enterprise="'"$ENTERPRISE"'"}'
```

If you are running these from your laptop, that cluster DNS name usually will **not** resolve unless you are on the cluster network or inside a pod. If that happens, run the same curl commands from a pod in the cluster using the same `VM_BASE`.

[1]: https://docs.victoriametrics.com/victoriametrics/url-examples/ "VictoriaMetrics: API examples"




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
