```bash
# 1. First BACKUP — export all copilot series to a file before deleting anything
curl -s "http://dev-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local:8428/api/v1/export" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams"}' \
  --data-urlencode 'match[]={__name__=~"github_billing.*", enterprise="sherwin-williams"}' \
  > /tmp/copilot_backup_$(date +%Y%m%d_%H%M%S).ndjson

echo "Backup complete. File size:"
ls -lh /tmp/copilot_backup_*.ndjson | tail -1
```

Confirm the backup file is non-empty before proceeding to the delete step.

```bash
# 2. CHECK — count how many series exist WITHOUT schema_version label
# This shows you exactly what will be deleted before you delete it
curl -s "http://dev-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version=""}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Series without schema_version: {len(d[\"data\"])}')"
```

```bash
# 3. CHECK — count how many series exist WITH schema_version label
# This is what you are keeping
curl -s "http://dev-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version="2026-06-exact-diff-v1"}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Series with schema_version: {len(d[\"data\"])}')"
```

Only proceed to step 4 if step 2 returns a non-zero count AND step 3 returns a non-zero count — confirming you have both old and new data and the new data is safely there before you delete the old.

```bash
# 4. DELETE — remove all copilot series WITHOUT schema_version label
curl -X POST "http://dev-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local:8428/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version=""}'

echo "Exit code: $?"
```

```bash
# 5. DELETE — remove billing series WITHOUT schema_version label (same issue affects github_billing_* metrics)
curl -X POST "http://dev-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local:8428/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_billing.*", enterprise="sherwin-williams", schema_version=""}'

echo "Exit code: $?"
```

```bash
# 6. VERIFY — confirm old series are gone
curl -s "http://dev-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version=""}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Remaining old series: {len(d[\"data\"])} (should be 0)')"
```

```bash
# 7. FORCE PURGE — VictoriaMetrics marks deleted series but does not immediately free disk
# This forces an immediate cleanup of the deleted series from storage
curl -X POST "http://dev-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local:8428/internal/force_merge?partition_prefix=small"

echo "Force merge triggered"
```

---

**Run these in order, one at a time, not as a script.** Stop after step 3 and confirm the counts make sense before running step 4. If step 3 returns 0 (no series with schema_version), do not delete — it means the backfill did not write the new label set and you would lose all your data.

After step 6 confirms 0 remaining old series, go back to Grafana — every panel that was showing doubles should immediately show a single clean value. No dashboard changes needed if the data is clean.

If you need to run these from outside the cluster (from your local machine):

```bash
# Port-forward first in a separate terminal
kubectl port-forward -n np-keystone \
  svc/dev-victoriametrics-victoria-metrics-single-server 8428:8428

# Then run all the curl commands above replacing the hostname with:
# http://localhost:8428
```





==========================================================================
The error is clear — `curl: (6) Could not resolve host: np-victoriametrics-victoria-metrics-single-server.np-keystone.svc.cluster.local`

**You are running this from your local Windows machine (MINGW64/Git Bash), not from inside the cluster.** The `.svc.cluster.local` DNS name only resolves from inside the Kubernetes cluster. Your local machine has no way to reach it directly.

**You need to port-forward first.** Open two terminals:

**Terminal 1 — keep this running the whole time:**
```bash
kubectl port-forward -n np-keystone \
  svc/np-victoriametrics-victoria-metrics-single-server 8428:8428
```

If that service name is wrong, find the exact name first:
```bash
kubectl get svc -n np-keystone | grep victoria
```

Then use whatever name appears in the output.

**Terminal 2 — run the steps using localhost:**

```bash
# Step 2 — check old series count
curl -s "http://localhost:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version=""}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Series without schema_version: {len(d[\"data\"])}')"
```

```bash
# Step 3 — check new series count (confirm data exists before deleting)
curl -s "http://localhost:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version="2026-06-exact-diff-v1"}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Series with schema_version: {len(d[\"data\"])}')"
```

**Only if step 3 returns a non-zero number, run the deletes:**

```bash
# Step 4 — delete old copilot series
curl -X POST "http://localhost:8428/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version=""}'
echo "Exit code: $?"
```

```bash
# Step 5 — delete old billing series
curl -X POST "http://localhost:8428/api/v1/admin/tsdb/delete_series" \
  --data-urlencode 'match[]={__name__=~"github_billing.*", enterprise="sherwin-williams", schema_version=""}'
echo "Exit code: $?"
```

```bash
# Step 6 — verify old series gone
curl -s "http://localhost:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version=""}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Remaining old series: {len(d[\"data\"])} (should be 0)')"
```

```bash
# Step 7 — force purge
curl -X POST "http://localhost:8428/internal/force_merge?partition_prefix=small"
```

The port-forward in Terminal 1 must stay open the entire time you are running the commands in Terminal 2.








================================

**Confirm the backfill completed successfully first, then we proceed.**

Run these three checks in order:

**Check 1 — confirm backfill wrote new data with schema_version:**
```bash
curl -s "http://localhost:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*", enterprise="sherwin-williams", schema_version="2026-06-exact-diff-v1"}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'New series with schema_version: {len(d[\"data\"])}')"
```

**Check 2 — confirm no old series crept back:**
```bash
curl -s "http://localhost:8428/prometheus/api/v1/series" \
  --data-urlencode 'match[]={__name__=~"github_copilot.*|github_billing.*", enterprise="sherwin-williams", schema_version=""}' \
  --data-urlencode 'start=-90d' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Old series without schema_version: {len(d[\"data\"])} (should be 0)')"
```

**Check 3 — confirm a specific key metric has data:**
```bash
curl -s "http://localhost:8428/api/v1/query?query=github_copilot_daily_active_users%7Benterprise%3D%22sherwin-williams%22%7D" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
results = d.get('data',{}).get('result',[])
print(f'Daily active users series count: {len(results)}')
for r in results[:3]:
    print(f'  labels={r[\"metric\"]} value={r[\"value\"][1]}')
"
```

**What the results should look like:**
- Check 1: a large number (tens of thousands) — all your 90-day data
- Check 2: 0 or very close to 0
- Check 3: exactly **1 series** with `schema_version="2026-06-exact-diff-v1"` — no duplicates

**If all three checks pass**, here is what to do next:

1. **Set backfill back to false** in the manifest and redeploy:
```yaml
- name: ENABLE_DATE_RANGE_BACKFILL
  value: "false"
```
```bash
kubectl apply -f github-copilot-exporter-manifest.yaml -n np-keystone
kubectl rollout restart -n np-keystone deploy/github-copilot-exporter
```

2. **Import the updated dashboards** into Grafana — all four files are ready and waiting. The duplicate panel issue should now be resolved since there is only one label set in VictoriaMetrics.

3. **Come back and confirm** the panels are showing single values — then I will proceed with the remaining dashboard changes we planned (dynamic time ranges, nav links, sort_desc on bargauge queries, removing Active Days panel).

Share the output of the three checks and we go from there.