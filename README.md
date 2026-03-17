Below is the **one setup to use** for your exact environment:

* Kubernetes namespace: **`namespace-name`**
* Existing repo: **`Platform-Engineering`**
* Alloy already deployed
* Grafana, VictoriaMetrics, VictoriaLogs, Loki, Prometheus, Tempo already running

This runbook uses a **best approach**:

**GitHub Copilot collector in Kubernetes → direct dated imports into VictoriaMetrics → Grafana dashboards query VictoriaMetrics → Alloy scrapes only the collector’s health metrics**.

That is the best fit because GitHub Copilot usage metrics are delivered as **report downloads via signed URLs with limited expiration**, while seat/licensing data comes from a separate Copilot management API. VictoriaMetrics supports direct historical imports in JSON line format, and Alloy is designed to scrape `/metrics` targets and forward those metrics. ([GitHub Docs][1])

Also, GitHub says the Copilot usage metrics policy must be enabled for these endpoints to work, and usage data is generally available within **two full UTC days** after a day closes, while dashboards may appear up to **three UTC days behind**. ([GitHub Docs][1])

---

# 0) What you are about to create

Inside `obseng-keystone-infra`, create this folder:

```text
Platform-Engineering/
└── github-copilot-insights/
    ├── app/
    │   ├── Dockerfile
    │   ├── requirements.txt
    │   └── main.py
    ├── k8s/
    │   ├── 01-secret.yaml
    │   ├── 02-deployment.yaml
    │   ├── 03-service.yaml
    │   └── 04-networkpolicy.yaml
    └── alloy/
        └── github-copilot-exporter.alloy
```

Run this first:

```bash
cd Platform-Engineering
mkdir -p github-copilot-insights/app github-copilot-insights/k8s github-copilot-insights/alloy
```

---

# 1) Before you paste anything, find 3 values in your cluster

You need these three values before editing the YAML:

1. Your GitHub org name
2. Your VictoriaMetrics **import** URL
3. Your existing Alloy `prometheus.remote_write` label

Run these commands:

```bash
kubectl -n dev-keystone get svc | egrep -i 'victoria|vm|insert|select'
kubectl -n dev-keystone get deploy | egrep -i 'alloy'
grep -R "prometheus.remote_write" -n .
```

Use the output to identify:

* the VictoriaMetrics import service, usually something like `vminsert`
* the Alloy config file that already has your working `prometheus.remote_write`
* the label name used there, for example something like `victoriametrics`, `default`, or `metrics`

For the collector, the correct VictoriaMetrics endpoint is:

* **Cluster VictoriaMetrics:** `http://<vminsert>:8480/insert/0/prometheus/api/v1/import`
* **Single-node VictoriaMetrics:** `http://<victoriametrics>:8428/api/v1/import` ([VictoriaMetrics Docs][2])

---

# 2) Create the GitHub token

This runbook uses a **fine-grained personal access token** because it is the fastest way to get running.

In GitHub, create a fine-grained PAT with these org permissions:

* **Organization Copilot metrics: Read**
* **GitHub Copilot Business: Read**
  or, if that option is not available, **Administration: Read**

Those permissions are what GitHub documents for:

* organization usage metrics
* seat information / billing endpoints ([GitHub Docs][1])

Also make sure your enterprise has the **Copilot usage metrics** policy enabled, otherwise the usage endpoints will not work. ([GitHub Docs][1])

---

# 3) Test the token before Kubernetes

Replace these two values first:

* `YOUR_GITHUB_TOKEN`
* `YOUR_GITHUB_ORG`

Run:

```bash
export GH_TOKEN='YOUR_GITHUB_TOKEN'
export GH_ORG='YOUR_GITHUB_ORG'
```

Test the billing endpoint:

```bash
curl -L \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/orgs/$GH_ORG/copilot/billing"
```

Test the organization usage metrics endpoint:

```bash
curl -L \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/orgs/$GH_ORG/copilot/metrics/reports/organization-28-day/latest"
```

GitHub’s current docs show those exact org endpoints and API version header, and the usage endpoint returns `download_links` that point to the actual report files. ([GitHub Docs][3])

If both calls work, continue.

---

# 4) Create the app files

## File: `Platform-Engineering/github-copilot-insights/app/requirements.txt`

```text
requests==2.32.3
prometheus-client==0.21.1
```

---

## File: `Platform-Engineering/github-copilot-insights/app/Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080

CMD ["python", "/app/main.py"]
```

---

## File: `Platform-Engineering/github-copilot-insights/app/main.py`

```python
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from prometheus_client import Counter, Gauge, start_http_server


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

# ----------------------------
# Environment variables
# ----------------------------
GH_TOKEN = os.environ["GH_TOKEN"]
GH_ORG = os.environ["GH_ORG"]
GH_API_BASE = os.getenv("GH_API_BASE", "https://api.github.com").rstrip("/")
GH_API_VERSION = os.getenv("GH_API_VERSION", "2026-03-10")

# GitHub says data is usually ready within two full UTC days.
# If you see missing recent data, change to 3.
DATA_LAG_DAYS = int(os.getenv("DATA_LAG_DAYS", "2"))

# How often the collector wakes up and checks/imports data.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "21600"))  # 6 hours

# Set to true only for the first rollout so the latest 28-day report is loaded once.
BOOTSTRAP_28D = os.getenv("BOOTSTRAP_28D", "true").lower() == "true"

EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "8080"))

# VictoriaMetrics import endpoint
VM_IMPORT_URL = os.environ["VM_IMPORT_URL"]
VM_USERNAME = os.getenv("VM_USERNAME", "")
VM_PASSWORD = os.getenv("VM_PASSWORD", "")
VM_BEARER_TOKEN = os.getenv("VM_BEARER_TOKEN", "")

HTTP_TIMEOUT = 60


# ----------------------------
# Prometheus self-metrics
# Alloy will scrape ONLY these metrics.
# ----------------------------
EXPORTER_UP = Gauge(
    "github_copilot_exporter_up",
    "1 if the last collector cycle succeeded",
    ["org"],
)

LAST_SUCCESS = Gauge(
    "github_copilot_exporter_last_success_unixtime_seconds",
    "Unix time of the last successful collector cycle",
    ["org"],
)

LAST_DURATION = Gauge(
    "github_copilot_exporter_last_run_duration_seconds",
    "Duration of the last collector cycle in seconds",
    ["org"],
)

IMPORTED_POINTS = Counter(
    "github_copilot_exporter_imported_points_total",
    "How many VictoriaMetrics points were imported",
    ["org"],
)

ERRORS = Counter(
    "github_copilot_exporter_errors_total",
    "How many collector cycles failed",
    ["org"],
)


# ----------------------------
# HTTP session for GitHub
# ----------------------------
session = requests.Session()
session.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": GH_API_VERSION,
    }
)

# Prevent importing the same stable day repeatedly while pod is alive
last_daily_import_day: Optional[str] = None

# Prevent re-running the 28-day bootstrap while pod is alive
bootstrapped = False


# ----------------------------
# Helpers
# ----------------------------
def vm_auth():
    if VM_BEARER_TOKEN:
        return None, {"Authorization": f"Bearer {VM_BEARER_TOKEN}"}
    if VM_USERNAME and VM_PASSWORD:
        return (VM_USERNAME, VM_PASSWORD), {}
    return None, {}


def github_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def parse_json_or_ndjson(text: str) -> Any:
    """
    Handles:
    - a normal JSON object
    - a normal JSON array
    - NDJSON (one JSON object per line)
    """
    text = text.strip()
    if not text:
        return []

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        return rows


def download_report_chunks(download_links: List[str]) -> List[Any]:
    chunks: List[Any] = []

    for link in download_links:
        resp = session.get(link, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        parsed = parse_json_or_ndjson(resp.text)

        if isinstance(parsed, list):
            chunks.extend(parsed)
        else:
            chunks.append(parsed)

    return chunks


def fetch_org_usage_28d() -> List[Any]:
    url = f"{GH_API_BASE}/orgs/{GH_ORG}/copilot/metrics/reports/organization-28-day/latest"
    meta = github_get_json(url)
    return download_report_chunks(meta.get("download_links", []))


def fetch_org_usage_for_day(day: str) -> List[Any]:
    url = f"{GH_API_BASE}/orgs/{GH_ORG}/copilot/metrics/reports/organization-1-day"
    meta = github_get_json(url, params={"day": day})
    return download_report_chunks(meta.get("download_links", []))


def fetch_billing() -> Dict[str, Any]:
    url = f"{GH_API_BASE}/orgs/{GH_ORG}/copilot/billing"
    return github_get_json(url)


def day_to_ms(day_str: str) -> int:
    """
    Use noon UTC for the report day to avoid any timezone confusion.
    """
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def coerce_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def vm_json_line(metric_name: str, labels: Dict[str, str], value: float, ts_ms: int) -> str:
    obj = {
        "metric": {
            "__name__": metric_name,
            **{k: str(v) for k, v in labels.items()},
        },
        "values": [value],
        "timestamps": [ts_ms],
    }
    return json.dumps(obj, separators=(",", ":"))


def append_point(lines: List[str], metric_name: str, labels: Dict[str, str], value: Any, ts_ms: int):
    num = coerce_number(value)
    if num is None:
        return
    lines.append(vm_json_line(metric_name, labels, num, ts_ms))


def extract_day_rows(chunks: List[Any]) -> List[Dict[str, Any]]:
    """
    Supports multiple shapes:
    - {"day_totals": [...]}
    - {"day": "...", ...}
    - [{"day_totals": [...]}]
    - [{"day": "...", ...}, ...]
    """
    rows: List[Dict[str, Any]] = []

    for chunk in chunks:
        if isinstance(chunk, dict) and "day_totals" in chunk:
            for row in chunk["day_totals"]:
                if isinstance(row, dict):
                    rows.append(row)
        elif isinstance(chunk, dict) and "day" in chunk:
            rows.append(chunk)
        elif isinstance(chunk, list):
            for item in chunk:
                if isinstance(item, dict) and "day_totals" in item:
                    rows.extend([r for r in item["day_totals"] if isinstance(r, dict)])
                elif isinstance(item, dict) and "day" in item:
                    rows.append(item)

    return rows


def build_usage_series_for_day(row: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    day = row["day"]
    ts_ms = day_to_ms(day)

    base = {"org": GH_ORG}

    # Top-level daily metrics
    root_fields = {
        "github_copilot_daily_active_users": row.get("daily_active_users"),
        "github_copilot_weekly_active_users": row.get("weekly_active_users"),
        "github_copilot_monthly_active_users": row.get("monthly_active_users"),
        "github_copilot_monthly_active_chat_users": row.get("monthly_active_chat_users"),
        "github_copilot_monthly_active_agent_users": row.get("monthly_active_agent_users"),
        "github_copilot_daily_active_cli_users": row.get("daily_active_cli_users"),
        "github_copilot_user_initiated_interaction_count": row.get("user_initiated_interaction_count"),
        "github_copilot_code_generation_activity_count": row.get("code_generation_activity_count"),
        "github_copilot_code_acceptance_activity_count": row.get("code_acceptance_activity_count"),
        "github_copilot_loc_suggested_to_add_sum": row.get("loc_suggested_to_add_sum"),
        "github_copilot_loc_suggested_to_delete_sum": row.get("loc_suggested_to_delete_sum"),
        "github_copilot_loc_added_sum": row.get("loc_added_sum"),
        "github_copilot_loc_deleted_sum": row.get("loc_deleted_sum"),
    }

    for metric_name, value in root_fields.items():
        append_point(lines, metric_name, base, value, ts_ms)

    # Pull request metrics
    pr = row.get("pull_requests") or {}
    pr_fields = {
        "github_copilot_pr_total_created": pr.get("total_created"),
        "github_copilot_pr_total_reviewed": pr.get("total_reviewed"),
        "github_copilot_pr_total_merged": pr.get("total_merged"),
        "github_copilot_pr_median_minutes_to_merge": pr.get("median_minutes_to_merge"),
        "github_copilot_pr_total_suggestions": pr.get("total_suggestions"),
        "github_copilot_pr_total_applied_suggestions": pr.get("total_applied_suggestions"),
        "github_copilot_pr_total_created_by_copilot": pr.get("total_created_by_copilot"),
        "github_copilot_pr_total_reviewed_by_copilot": pr.get("total_reviewed_by_copilot"),
        "github_copilot_pr_total_merged_created_by_copilot": pr.get("total_merged_created_by_copilot"),
        "github_copilot_pr_median_minutes_to_merge_copilot_authored": pr.get("median_minutes_to_merge_copilot_authored"),
        "github_copilot_pr_total_copilot_suggestions": pr.get("total_copilot_suggestions"),
        "github_copilot_pr_total_copilot_applied_suggestions": pr.get("total_copilot_applied_suggestions"),
    }

    for metric_name, value in pr_fields.items():
        append_point(lines, metric_name, base, value, ts_ms)

    # CLI metrics
    cli = row.get("totals_by_cli") or {}
    cli_token = cli.get("token_usage") or {}
    cli_fields = {
        "github_copilot_cli_session_count": cli.get("session_count"),
        "github_copilot_cli_request_count": cli.get("request_count"),
        "github_copilot_cli_prompt_count": cli.get("prompt_count"),
        "github_copilot_cli_output_tokens_sum": cli_token.get("output_tokens_sum"),
        "github_copilot_cli_prompt_tokens_sum": cli_token.get("prompt_tokens_sum"),
        "github_copilot_cli_avg_tokens_per_request": cli_token.get("avg_tokens_per_request"),
    }

    for metric_name, value in cli_fields.items():
        append_point(lines, metric_name, base, value, ts_ms)

    # By feature
    for item in row.get("totals_by_feature") or []:
        labels = {
            "org": GH_ORG,
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(lines, "github_copilot_feature_code_generation_activity_count", labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_feature_code_acceptance_activity_count", labels, item.get("code_acceptance_activity_count"), ts_ms)
        append_point(lines, "github_copilot_feature_user_initiated_interaction_count", labels, item.get("user_initiated_interaction_count"), ts_ms)
        append_point(lines, "github_copilot_feature_loc_suggested_to_add_sum", labels, item.get("loc_suggested_to_add_sum"), ts_ms)
        append_point(lines, "github_copilot_feature_loc_suggested_to_delete_sum", labels, item.get("loc_suggested_to_delete_sum"), ts_ms)
        append_point(lines, "github_copilot_feature_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)
        append_point(lines, "github_copilot_feature_loc_deleted_sum", labels, item.get("loc_deleted_sum"), ts_ms)

    # By IDE
    for item in row.get("totals_by_ide") or []:
        labels = {
            "org": GH_ORG,
            "ide": str(item.get("ide", "unknown")),
        }
        append_point(lines, "github_copilot_ide_code_generation_activity_count", labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_ide_code_acceptance_activity_count", labels, item.get("code_acceptance_activity_count"), ts_ms)
        append_point(lines, "github_copilot_ide_user_initiated_interaction_count", labels, item.get("user_initiated_interaction_count"), ts_ms)
        append_point(lines, "github_copilot_ide_loc_suggested_to_add_sum", labels, item.get("loc_suggested_to_add_sum"), ts_ms)
        append_point(lines, "github_copilot_ide_loc_suggested_to_delete_sum", labels, item.get("loc_suggested_to_delete_sum"), ts_ms)
        append_point(lines, "github_copilot_ide_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)
        append_point(lines, "github_copilot_ide_loc_deleted_sum", labels, item.get("loc_deleted_sum"), ts_ms)

    # By language + feature
    for item in row.get("totals_by_language_feature") or []:
        labels = {
            "org": GH_ORG,
            "language": str(item.get("language", "unknown")),
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(lines, "github_copilot_language_feature_code_generation_activity_count", labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_language_feature_code_acceptance_activity_count", labels, item.get("code_acceptance_activity_count"), ts_ms)
        append_point(lines, "github_copilot_language_feature_loc_suggested_to_add_sum", labels, item.get("loc_suggested_to_add_sum"), ts_ms)
        append_point(lines, "github_copilot_language_feature_loc_suggested_to_delete_sum", labels, item.get("loc_suggested_to_delete_sum"), ts_ms)
        append_point(lines, "github_copilot_language_feature_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)
        append_point(lines, "github_copilot_language_feature_loc_deleted_sum", labels, item.get("loc_deleted_sum"), ts_ms)

    return lines


def build_billing_series(billing: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    ts_ms = now_ms()
    labels = {"org": GH_ORG}
    seat_breakdown = billing.get("seat_breakdown") or {}

    fields = {
        "github_copilot_seat_total": seat_breakdown.get("total"),
        "github_copilot_seat_added_this_cycle": seat_breakdown.get("added_this_cycle"),
        "github_copilot_seat_pending_invitation": seat_breakdown.get("pending_invitation"),
        "github_copilot_seat_pending_cancellation": seat_breakdown.get("pending_cancellation"),
        "github_copilot_seat_active_this_cycle": seat_breakdown.get("active_this_cycle"),
        "github_copilot_seat_inactive_this_cycle": seat_breakdown.get("inactive_this_cycle"),
    }

    for metric_name, value in fields.items():
        append_point(lines, metric_name, labels, value, ts_ms)

    return lines


def import_to_victoriametrics(lines: List[str]) -> int:
    if not lines:
        return 0

    auth, extra_headers = vm_auth()
    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers)

    payload = ("\n".join(lines) + "\n").encode("utf-8")
    resp = requests.post(
        VM_IMPORT_URL,
        data=payload,
        headers=headers,
        auth=auth,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return len(lines)


def bootstrap_28d_once():
    """
    Imports the latest 28-day organization report once.
    IMPORTANT:
    After you confirm the first successful run, set BOOTSTRAP_28D=false
    in the Deployment so a future pod restart does not re-backfill the same 28 days.
    """
    global bootstrapped, last_daily_import_day

    if not BOOTSTRAP_28D or bootstrapped:
        return

    logging.info("Starting one-time 28-day bootstrap import for org=%s", GH_ORG)

    chunks = fetch_org_usage_28d()
    rows = extract_day_rows(chunks)

    lines: List[str] = []
    latest_day = None

    for row in rows:
        if row.get("day"):
            latest_day = max(latest_day, row["day"]) if latest_day else row["day"]
        lines.extend(build_usage_series_for_day(row))

    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(org=GH_ORG).inc(count)

    bootstrapped = True
    if latest_day:
        last_daily_import_day = latest_day

    logging.info(
        "Completed 28-day bootstrap import for org=%s imported_points=%s latest_day=%s",
        GH_ORG,
        count,
        latest_day,
    )


def import_latest_stable_day_if_needed():
    global last_daily_import_day

    target_day = (datetime.now(timezone.utc).date() - timedelta(days=DATA_LAG_DAYS)).isoformat()

    if last_daily_import_day == target_day:
        logging.info("Stable day %s already imported in this pod lifecycle; skipping", target_day)
        return

    logging.info("Importing stable day %s for org=%s", target_day, GH_ORG)

    chunks = fetch_org_usage_for_day(target_day)
    rows = extract_day_rows(chunks)

    lines: List[str] = []
    for row in rows:
        lines.extend(build_usage_series_for_day(row))

    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(org=GH_ORG).inc(count)
    last_daily_import_day = target_day

    logging.info(
        "Completed stable day import for org=%s day=%s imported_points=%s",
        GH_ORG,
        target_day,
        count,
    )


def import_billing_snapshot():
    billing = fetch_billing()
    lines = build_billing_series(billing)
    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(org=GH_ORG).inc(count)

    logging.info(
        "Imported billing snapshot for org=%s imported_points=%s",
        GH_ORG,
        count,
    )


def run_cycle():
    start = time.time()

    try:
        bootstrap_28d_once()
        import_latest_stable_day_if_needed()
        import_billing_snapshot()

        EXPORTER_UP.labels(org=GH_ORG).set(1)
        LAST_SUCCESS.labels(org=GH_ORG).set(time.time())
    except Exception:
        ERRORS.labels(org=GH_ORG).inc()
        EXPORTER_UP.labels(org=GH_ORG).set(0)
        logging.exception("Collector cycle failed for org=%s", GH_ORG)
        raise
    finally:
        LAST_DURATION.labels(org=GH_ORG).set(time.time() - start)


def main():
    logging.info("Starting GitHub Copilot collector on :%s for org=%s", EXPORTER_PORT, GH_ORG)
    start_http_server(EXPORTER_PORT)

    while True:
        try:
            run_cycle()
        except Exception:
            # Keep process alive so Alloy can still scrape self-metrics
            pass

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
```

This code handles both normal JSON and NDJSON report files. That means **nothing in your YAML changes** if GitHub gives you NDJSON later; only the parser matters, and the code above already supports both formats. GitHub documents NDJSON export as part of Copilot usage metrics/export workflows, while VictoriaMetrics expects its own JSON-line import schema at `/api/v1/import`, so the collector still must transform GitHub’s file into VictoriaMetrics import lines. ([GitHub Docs][4])

---

# 5) Create the Kubernetes files

## File: `Platform-Engineering/github-copilot-insights/k8s/01-secret.yaml`

Replace these placeholders before applying:

* `YOUR_GITHUB_TOKEN`
* `YOUR_GITHUB_ORG`
* `YOUR_VM_IMPORT_URL`

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: github-copilot-exporter-secret
  namespace: dev-keystone
type: Opaque
stringData:
  GH_TOKEN: "YOUR_GITHUB_TOKEN"
  GH_ORG: "YOUR_GITHUB_ORG"
  GH_API_BASE: "https://api.github.com"
  GH_API_VERSION: "2026-03-10"

  # Start with 2. If your org’s data is often incomplete for recent days, change to 3.
  DATA_LAG_DAYS: "2"

  # 6 hours
  POLL_INTERVAL_SECONDS: "21600"

  # Keep true only for the very first rollout so the latest 28-day report is loaded once.
  # After the first successful run, set this to false and re-apply.
  BOOTSTRAP_28D: "true"

  EXPORTER_PORT: "8080"
  LOG_LEVEL: "INFO"

  # Cluster VictoriaMetrics example:
  # http://vminsert.dev-keystone.svc.cluster.local:8480/insert/0/prometheus/api/v1/import
  #
  # Single-node VictoriaMetrics example:
  # http://victoriametrics.dev-keystone.svc.cluster.local:8428/api/v1/import
  VM_IMPORT_URL: "YOUR_VM_IMPORT_URL"

  # Leave blank if your internal VictoriaMetrics import endpoint does not require auth
  VM_USERNAME: ""
  VM_PASSWORD: ""
  VM_BEARER_TOKEN: ""
```

The import endpoint above must be the JSON-line import path, not `/import/prometheus`, because this collector writes VictoriaMetrics JSON-line objects. ([VictoriaMetrics Docs][2])

---

## File: `Platform-Engineering/github-copilot-insights/k8s/02-deployment.yaml`

Replace:

* `YOUR_REGISTRY/github-copilot-exporter:v1`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: github-copilot-exporter
  namespace: dev-keystone
  labels:
    app: github-copilot-exporter
spec:
  replicas: 1
  selector:
    matchLabels:
      app: github-copilot-exporter
  template:
    metadata:
      labels:
        app: github-copilot-exporter
    spec:
      containers:
        - name: github-copilot-exporter
          image: YOUR_REGISTRY/github-copilot-exporter:v1
          imagePullPolicy: IfNotPresent
          envFrom:
            - secretRef:
                name: github-copilot-exporter-secret
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet:
              path: /metrics
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /metrics
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 20
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
```

---

## File: `Platform-Engineering/github-copilot-insights/k8s/03-service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: github-copilot-exporter
  namespace: dev-keystone
  labels:
    app: github-copilot-exporter
spec:
  selector:
    app: github-copilot-exporter
  ports:
    - name: http
      port: 8080
      targetPort: 8080
```

---

## File: `Platform-Engineering/github-copilot-insights/k8s/04-networkpolicy.yaml`

If your cluster uses NetworkPolicy, apply this. If not, skip it.

Replace:

* `YOUR_ALLOY_NAMESPACE` if Alloy is not running in `dev-keystone`

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: github-copilot-exporter
  namespace: dev-keystone
spec:
  podSelector:
    matchLabels:
      app: github-copilot-exporter
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: dev-keystone
      ports:
        - protocol: TCP
          port: 8080
  egress:
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
      ports:
        - protocol: TCP
          port: 443
        - protocol: TCP
          port: 8480
        - protocol: TCP
          port: 8428
```

---

# 6) Create the Alloy snippet

Because your Alloy is already running and already scraping other applications, you should **not replace your Alloy config**.
You should add **one scrape block** for this exporter and point it to your **existing** `prometheus.remote_write` receiver.

## File: `Platform-Engineering/github-copilot-insights/alloy/github-copilot-exporter.alloy`

Replace:

* `REPLACE_WITH_YOUR_EXISTING_VM_REMOTE_WRITE_LABEL`

```alloy
prometheus.scrape "github_copilot_exporter" {
  targets = [
    {
      "__address__" = "github-copilot-exporter.dev-keystone.svc.cluster.local:8080",
      "job"         = "github-copilot-exporter",
    },
  ]

  metrics_path    = "/metrics"
  scrape_interval = "30s"
  scrape_timeout  = "10s"

  forward_to = [prometheus.remote_write.REPLACE_WITH_YOUR_EXISTING_VM_REMOTE_WRITE_LABEL.receiver]
}
```

Alloy’s documented pattern is that `prometheus.scrape` scrapes a target and forwards the scraped metrics to the receivers listed in `forward_to`, typically a `prometheus.remote_write` receiver. ([Grafana Labs][5])

### How to find the receiver label

Run this in the repo:

```bash
grep -R "prometheus.remote_write" -n .
```

If you see something like this in your current Alloy config:

```alloy
prometheus.remote_write "victoriametrics" {
  endpoint {
    url = "http://something"
  }
}
```

then your label is:

```text
victoriametrics
```

and your final Alloy line becomes:

```alloy
forward_to = [prometheus.remote_write.victoriametrics.receiver]
```

### Very important

This Alloy scrape is only for the collector’s **health metrics**.
The actual Copilot business metrics go **directly** from the collector into VictoriaMetrics.

---

# 7) Build and push the image

From your repo root:

```bash
cd Platform-Engineering/github-copilot-insights/app
docker build -t YOUR_REGISTRY/github-copilot-exporter:v1 .
docker push YOUR_REGISTRY/github-copilot-exporter:v1
```

If your cluster pulls images from a private registry, use the same registry and pull secret pattern you already use for your other apps.

---

# 8) Apply the Kubernetes files

From repo root:

```bash
cd Platform-Engineering

kubectl apply -f github-copilot-insights/k8s/01-secret.yaml
kubectl apply -f github-copilot-insights/k8s/02-deployment.yaml
kubectl apply -f github-copilot-insights/k8s/03-service.yaml
kubectl apply -f github-copilot-insights/k8s/04-networkpolicy.yaml
```

Check rollout:

```bash
kubectl -n dev-keystone rollout status deployment/github-copilot-exporter
kubectl -n dev-keystone get pods -l app=github-copilot-exporter
kubectl -n dev-keystone get svc github-copilot-exporter
```

Check logs:

```bash
kubectl -n dev-keystone logs deployment/github-copilot-exporter -f
```

You want to see messages like:

* starting one-time 28-day bootstrap
* completed 28-day bootstrap
* importing stable day
* imported billing snapshot

---

# 9) Add the Alloy scrape to your existing Alloy config

Because I do not know your exact Alloy file layout, do this in the same repo wherever your current Alloy config lives:

1. Open your existing Alloy config file
2. Find the `prometheus.remote_write` label already used for VictoriaMetrics
3. Paste the scrape block from `github-copilot-exporter.alloy`
4. Commit and deploy Alloy the same way your team already deploys Alloy

After deploy, verify Alloy can scrape the exporter.

First, test the exporter directly:

```bash
kubectl -n dev-keystone port-forward svc/github-copilot-exporter 8080:8080
```

Then in another terminal:

```bash
curl -s http://127.0.0.1:8080/metrics | grep github_copilot_exporter
```

You should see metrics like:

* `github_copilot_exporter_up`
* `github_copilot_exporter_last_success_unixtime_seconds`
* `github_copilot_exporter_last_run_duration_seconds`
* `github_copilot_exporter_errors_total`

---

# 10) After the first successful bootstrap, disable the 28-day backfill

This matters.

The first run should load the latest 28-day report once.
After that, change `BOOTSTRAP_28D` to `false` so a future pod restart does not re-import the same 28 days.

Edit this file:

`Platform-Engineering/github-copilot-insights/k8s/01-secret.yaml`

Change:

```yaml
BOOTSTRAP_28D: "true"
```

to:

```yaml
BOOTSTRAP_28D: "false"
```

Then apply again:

```bash
kubectl apply -f github-copilot-insights/k8s/01-secret.yaml
kubectl -n dev-keystone rollout restart deployment/github-copilot-exporter
kubectl -n dev-keystone rollout status deployment/github-copilot-exporter
```

---

# 11) First Grafana queries to test

Open Grafana Explore against your VictoriaMetrics datasource and test these.

### Seats

```promql
last_over_time(github_copilot_seat_total{org="YOUR_GITHUB_ORG"}[30d])
```

```promql
last_over_time(github_copilot_seat_active_this_cycle{org="YOUR_GITHUB_ORG"}[30d])
```

```promql
last_over_time(github_copilot_seat_inactive_this_cycle{org="YOUR_GITHUB_ORG"}[30d])
```

### Adoption

```promql
github_copilot_daily_active_users{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_weekly_active_users{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_monthly_active_users{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_monthly_active_chat_users{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_monthly_active_agent_users{org="YOUR_GITHUB_ORG"}
```

### Engagement

```promql
github_copilot_code_generation_activity_count{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_code_acceptance_activity_count{org="YOUR_GITHUB_ORG"}
```

```promql
100 *
github_copilot_code_acceptance_activity_count{org="YOUR_GITHUB_ORG"}
/
clamp_min(github_copilot_code_generation_activity_count{org="YOUR_GITHUB_ORG"}, 1)
```

### LoC

```promql
github_copilot_loc_suggested_to_add_sum{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_loc_added_sum{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_loc_deleted_sum{org="YOUR_GITHUB_ORG"}
```

### By feature

```promql
sum by (feature) (
  sum_over_time(github_copilot_feature_loc_added_sum{org="YOUR_GITHUB_ORG"}[$__range])
)
```

### By IDE

```promql
sum by (ide) (
  sum_over_time(github_copilot_ide_loc_added_sum{org="YOUR_GITHUB_ORG"}[$__range])
)
```

### By language and feature

```promql
sum by (language, feature) (
  sum_over_time(github_copilot_language_feature_loc_added_sum{org="YOUR_GITHUB_ORG"}[$__range])
)
```

### PR metrics

```promql
github_copilot_pr_total_created{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_pr_total_merged{org="YOUR_GITHUB_ORG"}
```

```promql
github_copilot_pr_median_minutes_to_merge{org="YOUR_GITHUB_ORG"}
```

### Collector health

```promql
time() - max(github_copilot_exporter_last_success_unixtime_seconds{org="YOUR_GITHUB_ORG"})
```

```promql
github_copilot_exporter_errors_total{org="YOUR_GITHUB_ORG"}
```

GitHub’s usage metrics model includes adoption, engagement, LoC, CLI, IDE, feature, language, and PR lifecycle fields, and the billing endpoint returns seat breakdown totals. ([GitHub Docs][6])

---

# 12) What changes if the GitHub report file is NDJSON

For this setup, **nothing changes in Kubernetes** and **nothing changes in Alloy**.

You keep:

* the same Deployment
* the same Secret
* the same Service
* the same Alloy scrape

Only the **parser** matters, and the `main.py` above already supports:

* standard JSON
* JSON arrays
* NDJSON

So if GitHub returns NDJSON, you do **not** need to rewrite your YAML.
The collector still:

1. downloads the file
2. parses each row
3. transforms it
4. writes VictoriaMetrics JSON-line import payloads

That is the right behavior because GitHub’s exported usage data can be NDJSON, but VictoriaMetrics expects its own JSON-line format at `/api/v1/import`. ([GitHub Docs][4])

---

# 13) The 4 placeholders you must replace

These are the only values I could not safely invent for you:

1. `YOUR_GITHUB_TOKEN`
2. `YOUR_GITHUB_ORG`
3. `YOUR_VM_IMPORT_URL`
4. `YOUR_REGISTRY/github-copilot-exporter:v1`
5. `REPLACE_WITH_YOUR_EXISTING_VM_REMOTE_WRITE_LABEL`

Everything else is ready to paste as-is.

---

# 14) The final rule to remember

Use this rule and you will not get lost:

**Copilot business metrics go directly from the collector into VictoriaMetrics.**
**Alloy scrapes only the collector’s self-metrics.**

That is the best approach for your `dev-keystone` setup.

If you want the next step, I’ll turn this into a **single Grafana dashboard JSON** using the exact metric names in this runbook.

[1]: https://docs.github.com/enterprise-cloud%40latest/rest/copilot/copilot-usage-metrics?apiVersion=2022-11-28 "REST API endpoints for Copilot usage metrics - GitHub Enterprise Cloud Docs"
[2]: https://docs.victoriametrics.com/victoriametrics/url-examples/ "VictoriaMetrics: API examples"
[3]: https://docs.github.com/en/rest/copilot/copilot-user-management "REST API endpoints for Copilot user management - GitHub Docs"
[4]: https://docs.github.com/en/copilot/how-tos/administer-copilot/view-usage-and-adoption?utm_source=chatgpt.com "Viewing the Copilot usage metrics dashboard"
[5]: https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.scrape/ "prometheus.scrape | Grafana Alloy documentation
"
[6]: https://docs.github.com/en/copilot/reference/copilot-usage-metrics/copilot-usage-metrics?utm_source=chatgpt.com "Data available in Copilot usage metrics"


