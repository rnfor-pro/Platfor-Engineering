#!/usr/bin/env bash
# =============================================================================
# VM Dirty Data Cleanup Script
# Run with your port-forward active on localhost:8428
# Set ENTERPRISE to your enterprise slug before running
# =============================================================================

set -euo pipefail

VM="http://localhost:8428"
ENTERPRISE="${ENTERPRISE:-sherwin-williams}"
BACKUP_DIR="./vm-backups-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "============================================================"
echo " VM Dirty Data Cleanup"
echo " Enterprise : $ENTERPRISE"
echo " VM         : $VM"
echo " Backup dir : $BACKUP_DIR"
echo "============================================================"
echo ""
read -p "Press ENTER to start, Ctrl+C to abort"

# =============================================================================
# STEP 1: BACK UP EVERYTHING FIRST
# =============================================================================

echo ""
echo "--- STEP 1: Backing up dirty families before deletion ---"

echo "[1/3] Backing up cohort metrics..."
curl -s -G "$VM/api/v1/export" \
  --data-urlencode "match[]={__name__=~\"github_copilot_ai_adoption_phase.*\",enterprise=\"$ENTERPRISE\"}" \
  > "$BACKUP_DIR/cohort_rollups.jsonl"
echo "  Lines: $(wc -l < "$BACKUP_DIR/cohort_rollups.jsonl")"

echo "[2/3] Backing up seat summary metrics..."
curl -s -G "$VM/api/v1/export" \
  --data-urlencode "match[]={__name__=~\"github_copilot_enterprise_seat_(active|never|pending|coverage|plan|assigning).*\",enterprise=\"$ENTERPRISE\"}" \
  > "$BACKUP_DIR/seat_summaries.jsonl"
echo "  Lines: $(wc -l < "$BACKUP_DIR/seat_summaries.jsonl")"

echo "[3/3] Backing up billing total metrics..."
curl -s -G "$VM/api/v1/export" \
  --data-urlencode "match[]={__name__=~\"github_copilot_billing_.*_total_.*\",enterprise=\"$ENTERPRISE\"}" \
  > "$BACKUP_DIR/billing_totals.jsonl"
curl -s -G "$VM/api/v1/export" \
  --data-urlencode "match[]={__name__=~\"github_billing_usage_summary_total_.*\",enterprise=\"$ENTERPRISE\"}" \
  >> "$BACKUP_DIR/billing_totals.jsonl"
echo "  Lines: $(wc -l < "$BACKUP_DIR/billing_totals.jsonl")"

echo ""
echo "Backups written to $BACKUP_DIR"
echo "Verify they are non-empty before continuing."
ls -lh "$BACKUP_DIR"
echo ""
read -p "Backups look good? Press ENTER to continue to deletion, Ctrl+C to abort"

# =============================================================================
# STEP 2: VERIFY WHAT EXISTS BEFORE DELETING
# Run these queries to confirm the data is actually dirty before deleting.
# =============================================================================

echo ""
echo "--- STEP 2: Pre-delete verification ---"

echo ""
echo "[CHECK] Cohort phase counts at a recent day (should match GitHub native after fix):"
curl -s -G "$VM/api/v1/query" \
  --data-urlencode "query=github_copilot_ai_adoption_phase_user_count{enterprise=\"$ENTERPRISE\"}" \
  --data-urlencode "time=$(date -u +%Y-%m-%dT12:00:00Z)" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
total=0
for r in d['data']['result']:
    phase=r['metric'].get('ai_adoption_phase','?')
    val=float(r['value'][1])
    total+=val
    print(f'  {phase}: {val}')
print(f'  TOTAL: {total}')
print('  (GitHub native shows Phase1=15, Phase2=7, Phase3=9 -- if VM shows much higher, it is dirty)')
"

echo ""
echo "[CHECK] How many distinct series exist for seat_active_last_28d_total:"
curl -s -G "$VM/api/v1/series" \
  --data-urlencode "match[]={__name__=\"github_copilot_enterprise_seat_active_last_28d_total\",enterprise=\"$ENTERPRISE\"}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'  Series count: {len(d[\"data\"])} (should be 1 per collection timestamp)')"

echo ""
read -p "Verified data is dirty. Press ENTER to delete, Ctrl+C to abort"

# =============================================================================
# STEP 3: DELETE DIRTY FAMILIES
# Order: cohort first (worst inflation), then seat summaries, then billing totals
# =============================================================================

echo ""
echo "--- STEP 3: Deleting dirty series ---"

echo ""
echo "[1/6] Deleting all cohort_rollup phase metrics..."
curl -s -G "$VM/api/v1/admin/tsdb/delete_series" \
  --data-urlencode "match[]={__name__=~\"github_copilot_ai_adoption_phase.*\",enterprise=\"$ENTERPRISE\"}"
echo "  Done"

echo "[2/6] Deleting per-user ai_adoption_phase flag metric..."
curl -s -G "$VM/api/v1/admin/tsdb/delete_series" \
  --data-urlencode "match[]={__name__=\"github_copilot_user_ai_adoption_phase\",enterprise=\"$ENTERPRISE\"}"
echo "  Done"

echo "[3/6] Deleting seat active window summary metrics..."
curl -s -G "$VM/api/v1/admin/tsdb/delete_series" \
  --data-urlencode "match[]={__name__=~\"github_copilot_enterprise_seat_active_last_(7|28|90)d_total\",enterprise=\"$ENTERPRISE\"}"
echo "  Done"

echo "[4/6] Deleting seat never_active, pending, coverage, plan, assigning_team totals..."
curl -s -G "$VM/api/v1/admin/tsdb/delete_series" \
  --data-urlencode "match[]={__name__=~\"github_copilot_enterprise_seat_(never_active|pending_cancellation|coverage_ratio|plan|assigning_team).*\",enterprise=\"$ENTERPRISE\"}"
echo "  Done"

echo "[5/6] Deleting billing total_ aggregates (per-item rows kept)..."
curl -s -G "$VM/api/v1/admin/tsdb/delete_series" \
  --data-urlencode "match[]={__name__=~\"github_copilot_billing_.*_total_.*\",enterprise=\"$ENTERPRISE\"}"
curl -s -G "$VM/api/v1/admin/tsdb/delete_series" \
  --data-urlencode "match[]={__name__=~\"github_billing_usage_summary_total_.*\",enterprise=\"$ENTERPRISE\"}"
echo "  Done"

echo "[6/6] Forcing disk merge to complete deletions..."
curl -s "$VM/internal/force_merge"
echo "  Done"

# =============================================================================
# STEP 4: VERIFY DELETION WORKED
# =============================================================================

echo ""
echo "--- STEP 4: Post-delete verification ---"

echo ""
echo "[CHECK] Cohort metrics should now return empty:"
curl -s -G "$VM/api/v1/query" \
  --data-urlencode "query=github_copilot_ai_adoption_phase_user_count{enterprise=\"$ENTERPRISE\"}" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
count=len(d['data']['result'])
print(f'  Series returned: {count} (expected 0)')
"

echo ""
echo "[CHECK] Seat summary metrics should now return empty:"
curl -s -G "$VM/api/v1/query" \
  --data-urlencode "query=github_copilot_enterprise_seat_active_last_28d_total{enterprise=\"$ENTERPRISE\"}" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
count=len(d['data']['result'])
print(f'  Series returned: {count} (expected 0)')
"

echo ""
echo "[CHECK] Confirm clean metrics still exist (should NOT be empty):"
curl -s -G "$VM/api/v1/query" \
  --data-urlencode "query=github_copilot_enterprise_seat_total{enterprise=\"$ENTERPRISE\"}" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
count=len(d['data']['result'])
print(f'  seat_total series: {count} (expected > 0 -- this metric is clean)')
"

curl -s -G "$VM/api/v1/query" \
  --data-urlencode "query=github_copilot_daily_active_users{enterprise=\"$ENTERPRISE\"}" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
count=len(d['data']['result'])
print(f'  daily_active_users series: {count} (expected > 0 -- this metric is clean)')
"

# =============================================================================
# STEP 5: INSTRUCTIONS FOR RE-IMPORT
# =============================================================================

echo ""
echo "============================================================"
echo " STEP 5: Re-import clean data"
echo "============================================================"
echo ""
echo "1. Deploy the v3 hardened exporter before re-importing."
echo "   DO NOT run the backfill with the old exporter -- it will"
echo "   re-introduce the same bugs."
echo ""
echo "2. Run a 90-day backfill in 15-day chunks:"
echo ""

START="2026-03-02"
END="2026-08-03"

python3 << PYEOF
from datetime import date, timedelta
start = date(2026, 3, 2)
end   = date(2026, 8, 3)
chunk = timedelta(days=14)
current = start
chunk_num = 1
while current <= end:
    chunk_end = min(current + chunk, end)
    print(f"  Chunk {chunk_num:2d}: BACKFILL_START_DAY={current.isoformat()}  BACKFILL_END_DAY={chunk_end.isoformat()}")
    current = chunk_end + timedelta(days=1)
    chunk_num += 1
PYEOF

echo ""
echo "3. After each chunk, verify cohort counts against GitHub native:"
echo "   curl -G 'http://localhost:8428/api/v1/query' \\"
echo "     --data-urlencode 'query=github_copilot_ai_adoption_phase_user_count{enterprise=\"$ENTERPRISE\"}' \\"
echo "     | python3 -m json.tool"
echo ""
echo "4. Seat summary metrics will be restored automatically by the"
echo "   next regular collection cycle (every POLL_INTERVAL_SECONDS)."
echo "   No backfill needed -- seats are live snapshot data only."
echo ""
echo "5. Historical seat trend panels will have a gap before today."
echo "   This is expected -- GitHub has no historical seat API."
echo "   The trend will grow forward correctly from now on."
echo ""
echo "============================================================"
echo " Cleanup complete."
echo "============================================================"


# Check if anything at all exists in this VM
curl 'http://localhost:8428/api/v1/label/__name__/values' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
names = d.get('data', [])
copilot = [n for n in names if 'copilot' in n]
print(f'Total metric names: {len(names)}')
print(f'Copilot metric names: {len(copilot)}')
for n in copilot[:10]:
    print(f'  {n}')
"
# Check what is actually in VM right now:
curl 'http://localhost:8428/api/v1/label/__name__/values' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
for n in sorted(d['data']):
    if 'phase' in n or 'cohort' in n:
        print(n)
"

# And check what the backfill log shows for cohort_rollups specifically:
kubectl logs -n dev-keystone deployment/github-copilot-exporter --tail=200 | \
  grep -E "cohort_rollup|enterprise_had_cohort|Skipping cohort"
