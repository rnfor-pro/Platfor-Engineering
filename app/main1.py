import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from prometheus_client import Counter, Gauge, start_http_server


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

GH_TOKEN = os.environ["GH_TOKEN"]
GH_ENTERPRISE = os.environ["GH_ENTERPRISE"]
GH_API_BASE = os.getenv("GH_API_BASE", "https://api.github.com").rstrip("/")
GH_API_VERSION = os.getenv("GH_API_VERSION", "2026-03-10")

DATA_LAG_DAYS = int(os.getenv("DATA_LAG_DAYS", "2"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "21600"))
BOOTSTRAP_28D = os.getenv("BOOTSTRAP_28D", "false").lower() == "true"
FORCE_BOOTSTRAP = os.getenv("FORCE_BOOTSTRAP", "false").lower() == "true"

EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "8080"))

VM_IMPORT_URL = os.environ["VM_IMPORT_URL"]
VM_SERIES_URL = os.getenv(
    "VM_SERIES_URL",
    "http://dev-victoriametrics-victoria-metrics-single-server.dev-keystone.svc.cluster.local:8428/prometheus/api/v1/series",
)

VM_USERNAME = os.getenv("VM_USERNAME", "")
VM_PASSWORD = os.getenv("VM_PASSWORD", "")
VM_BEARER_TOKEN = os.getenv("VM_BEARER_TOKEN", "")

HTTP_TIMEOUT = 60

EXPORTER_UP = Gauge(
    "github_copilot_exporter_up",
    "1 if the last collector cycle succeeded",
    ["enterprise"],
)

LAST_SUCCESS = Gauge(
    "github_copilot_exporter_last_success_unixtime_seconds",
    "Unix time of the last successful collector cycle",
    ["enterprise"],
)

LAST_DURATION = Gauge(
    "github_copilot_exporter_last_run_duration_seconds",
    "Duration of the last collector cycle in seconds",
    ["enterprise"],
)

IMPORTED_POINTS = Counter(
    "github_copilot_exporter_imported_points_total",
    "How many VictoriaMetrics points were imported",
    ["enterprise"],
)

ERRORS = Counter(
    "github_copilot_exporter_errors_total",
    "How many collector cycles failed",
    ["enterprise"],
)

github_session = requests.Session()
github_session.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": GH_API_VERSION,
    }
)

last_daily_import_day: Optional[str] = None
bootstrapped = False


def vm_auth() -> Tuple[Optional[Tuple[str, str]], Dict[str, str]]:
    if VM_BEARER_TOKEN:
        return None, {"Authorization": f"Bearer {VM_BEARER_TOKEN}"}
    if VM_USERNAME and VM_PASSWORD:
        return (VM_USERNAME, VM_PASSWORD), {}
    return None, {}


def github_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    resp = github_session.get(url, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def parse_json_or_ndjson(text: str) -> Any:
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
    download_session = requests.Session()

    for link in download_links:
        resp = download_session.get(link, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        parsed = parse_json_or_ndjson(resp.text)

        if isinstance(parsed, list):
            chunks.extend(parsed)
        else:
            chunks.append(parsed)

    return chunks


def fetch_enterprise_usage_28d() -> List[Any]:
    url = f"{GH_API_BASE}/enterprises/{GH_ENTERPRISE}/copilot/metrics/reports/enterprise-28-day/latest"
    meta = github_get_json(url)
    return download_report_chunks(meta.get("download_links", []))


def fetch_enterprise_usage_for_day(day: str) -> List[Any]:
    url = f"{GH_API_BASE}/enterprises/{GH_ENTERPRISE}/copilot/metrics/reports/enterprise-1-day"
    meta = github_get_json(url, params={"day": day})
    return download_report_chunks(meta.get("download_links", []))


def fetch_enterprise_users_usage_28d() -> List[Any]:
    url = f"{GH_API_BASE}/enterprises/{GH_ENTERPRISE}/copilot/metrics/reports/users-28-day/latest"
    meta = github_get_json(url)
    return download_report_chunks(meta.get("download_links", []))


def fetch_enterprise_users_usage_for_day(day: str) -> List[Any]:
    url = f"{GH_API_BASE}/enterprises/{GH_ENTERPRISE}/copilot/metrics/reports/users-1-day"
    meta = github_get_json(url, params={"day": day})
    return download_report_chunks(meta.get("download_links", []))


def fetch_enterprise_seats() -> Dict[str, Any]:
    url = f"{GH_API_BASE}/enterprises/{GH_ENTERPRISE}/copilot/billing/seats"
    page = 1
    total_seats = None
    seats: List[Dict[str, Any]] = []

    while True:
        resp = github_session.get(url, params={"page": page}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()

        if total_seats is None:
            total_seats = payload.get("total_seats", 0)

        seats.extend(payload.get("seats", []))

        # GitHub paginates this endpoint. We stop when fewer than 50 rows are returned.
        page_rows = payload.get("seats", [])
        if not page_rows or len(page_rows) < 50:
            break

        page += 1

    return {
        "total_seats": total_seats or 0,
        "seat_rows_returned": len(seats),
        "seats": seats,
    }


def day_to_ms(day_str: str) -> int:
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def day_bounds(day_str: str) -> Tuple[str, str]:
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


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


def extract_rows(chunks: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for chunk in chunks:
        if isinstance(chunk, list):
            for item in chunk:
                if isinstance(item, dict) and "day" in item:
                    rows.append(item)
                elif isinstance(item, dict) and "day_totals" in item:
                    rows.extend([r for r in item["day_totals"] if isinstance(r, dict)])
        elif isinstance(chunk, dict) and "day_totals" in chunk:
            rows.extend([r for r in chunk["day_totals"] if isinstance(r, dict)])
        elif isinstance(chunk, dict) and "day" in chunk:
            rows.append(chunk)

    return rows


def vm_series_exists(matchers: List[str], start: str, end: str) -> bool:
    auth, extra_headers = vm_auth()
    data = [("match[]", m) for m in matchers]
    data.append(("start", start))
    data.append(("end", end))

    resp = requests.post(
        VM_SERIES_URL,
        data=data,
        headers=extra_headers,
        auth=auth,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()

    payload = resp.json()
    series = payload.get("data", [])
    return len(series) > 0


def enterprise_bootstrap_already_present() -> bool:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    matcher = f'github_copilot_daily_active_users{{enterprise="{GH_ENTERPRISE}"}}'
    return vm_series_exists([matcher], start.isoformat(), end.isoformat())


def enterprise_day_already_present(day: str) -> bool:
    start, end = day_bounds(day)
    matcher = f'github_copilot_daily_active_users{{enterprise="{GH_ENTERPRISE}"}}'
    return vm_series_exists([matcher], start, end)


def user_day_already_present(day: str) -> bool:
    start, end = day_bounds(day)
    matcher = f'github_copilot_user_daily_record{{enterprise="{GH_ENTERPRISE}"}}'
    return vm_series_exists([matcher], start, end)


def build_enterprise_usage_series(row: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    day = row["day"]
    ts_ms = day_to_ms(day)
    base = {"enterprise": GH_ENTERPRISE}

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

    for item in row.get("totals_by_feature") or []:
        labels = {"enterprise": GH_ENTERPRISE, "feature": str(item.get("feature", "unknown"))}
        append_point(lines, "github_copilot_feature_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)
        append_point(lines, "github_copilot_feature_loc_deleted_sum", labels, item.get("loc_deleted_sum"), ts_ms)
        append_point(lines, "github_copilot_feature_code_generation_activity_count", labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_feature_code_acceptance_activity_count", labels, item.get("code_acceptance_activity_count"), ts_ms)
        append_point(lines, "github_copilot_feature_user_initiated_interaction_count", labels, item.get("user_initiated_interaction_count"), ts_ms)

    for item in row.get("totals_by_ide") or []:
        labels = {"enterprise": GH_ENTERPRISE, "ide": str(item.get("ide", "unknown"))}
        append_point(lines, "github_copilot_ide_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)
        append_point(lines, "github_copilot_ide_loc_deleted_sum", labels, item.get("loc_deleted_sum"), ts_ms)
        append_point(lines, "github_copilot_ide_code_generation_activity_count", labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_ide_code_acceptance_activity_count", labels, item.get("code_acceptance_activity_count"), ts_ms)
        append_point(lines, "github_copilot_ide_user_initiated_interaction_count", labels, item.get("user_initiated_interaction_count"), ts_ms)

    for item in row.get("totals_by_language_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "language": str(item.get("language", "unknown")),
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(lines, "github_copilot_language_feature_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)
        append_point(lines, "github_copilot_language_feature_code_generation_activity_count", labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_language_feature_code_acceptance_activity_count", labels, item.get("code_acceptance_activity_count"), ts_ms)

    cli = row.get("totals_by_cli") or {}
    cli_token = cli.get("token_usage") or {}
    append_point(lines, "github_copilot_cli_session_count", base, cli.get("session_count"), ts_ms)
    append_point(lines, "github_copilot_cli_request_count", base, cli.get("request_count"), ts_ms)
    append_point(lines, "github_copilot_cli_prompt_count", base, cli.get("prompt_count"), ts_ms)
    append_point(lines, "github_copilot_cli_output_tokens_sum", base, cli_token.get("output_tokens_sum"), ts_ms)
    append_point(lines, "github_copilot_cli_prompt_tokens_sum", base, cli_token.get("prompt_tokens_sum"), ts_ms)
    append_point(lines, "github_copilot_cli_avg_tokens_per_request", base, cli_token.get("avg_tokens_per_request"), ts_ms)

    return lines


def build_user_usage_series(row: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    day = row["day"]
    ts_ms = day_to_ms(day)

    user_login = str(row.get("user_login", "unknown"))
    user_id = str(row.get("user_id", "unknown"))

    base = {
        "enterprise": GH_ENTERPRISE,
        "user_login": user_login,
        "user_id": user_id,
    }

    append_point(lines, "github_copilot_user_daily_record", base, 1, ts_ms)

    root_fields = {
        "github_copilot_user_prompt_count": row.get("user_initiated_interaction_count"),
        "github_copilot_user_code_generation_activity_count": row.get("code_generation_activity_count"),
        "github_copilot_user_code_acceptance_activity_count": row.get("code_acceptance_activity_count"),
        "github_copilot_user_loc_suggested_to_add_sum": row.get("loc_suggested_to_add_sum"),
        "github_copilot_user_loc_suggested_to_delete_sum": row.get("loc_suggested_to_delete_sum"),
        "github_copilot_user_loc_added_sum": row.get("loc_added_sum"),
        "github_copilot_user_loc_deleted_sum": row.get("loc_deleted_sum"),
        "github_copilot_user_used_chat": row.get("used_chat"),
        "github_copilot_user_used_agent": row.get("used_agent"),
        "github_copilot_user_used_cli": row.get("used_cli"),
        "github_copilot_user_chat_panel_agent_mode_count": row.get("chat_panel_agent_mode"),
        "github_copilot_user_chat_panel_ask_mode_count": row.get("chat_panel_ask_mode"),
        "github_copilot_user_chat_panel_custom_mode_count": row.get("chat_panel_custom_mode"),
        "github_copilot_user_chat_panel_edit_mode_count": row.get("chat_panel_edit_mode"),
        "github_copilot_user_chat_panel_unknown_mode_count": row.get("chat_panel_unknown_mode"),
    }

    for metric_name, value in root_fields.items():
        append_point(lines, metric_name, base, value, ts_ms)

    for item in row.get("totals_by_ide") or []:
        ide = str(item.get("ide", "unknown"))
        ide_version = ""
        plugin_version = ""

        last_known_ide_version = item.get("last_known_ide_version") or {}
        if isinstance(last_known_ide_version, dict):
            ide_version = str(last_known_ide_version.get("ide_version", ""))

        last_known_plugin_version = item.get("last_known_plugin_version") or {}
        if isinstance(last_known_plugin_version, dict):
            plugin_version = str(last_known_plugin_version.get("plugin_version", ""))

        ide_labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "ide": ide,
            "ide_version": ide_version,
            "plugin_version": plugin_version,
        }

        append_point(lines, "github_copilot_user_ide_daily_record", ide_labels, 1, ts_ms)
        append_point(lines, "github_copilot_user_ide_prompt_count", ide_labels, item.get("user_initiated_interaction_count"), ts_ms)
        append_point(lines, "github_copilot_user_ide_code_generation_activity_count", ide_labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_user_ide_code_acceptance_activity_count", ide_labels, item.get("code_acceptance_activity_count"), ts_ms)
        append_point(lines, "github_copilot_user_ide_loc_added_sum", ide_labels, item.get("loc_added_sum"), ts_ms)

    totals_by_cli = row.get("totals_by_cli") or {}
    last_known_cli_version = totals_by_cli.get("last_known_cli_version") or {}
    cli_version = ""
    if isinstance(last_known_cli_version, dict):
        cli_version = str(last_known_cli_version.get("cli_version", ""))

    if cli_version:
        cli_labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "cli_version": cli_version,
        }
        append_point(lines, "github_copilot_user_cli_daily_record", cli_labels, 1, ts_ms)

    for item in row.get("totals_by_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(lines, "github_copilot_user_feature_prompt_count", labels, item.get("user_initiated_interaction_count"), ts_ms)
        append_point(lines, "github_copilot_user_feature_code_generation_activity_count", labels, item.get("code_generation_activity_count"), ts_ms)
        append_point(lines, "github_copilot_user_feature_code_acceptance_activity_count", labels, item.get("code_acceptance_activity_count"), ts_ms)
        append_point(lines, "github_copilot_user_feature_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)

    return lines


def build_enterprise_seat_series(seat_payload: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    ts_ms = now_ms()
    labels = {"enterprise": GH_ENTERPRISE}

    append_point(lines, "github_copilot_enterprise_seat_total", labels, seat_payload.get("total_seats"), ts_ms)
    append_point(lines, "github_copilot_enterprise_seat_rows_returned", labels, seat_payload.get("seat_rows_returned"), ts_ms)

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
    global bootstrapped, last_daily_import_day

    if not BOOTSTRAP_28D or bootstrapped:
        return

    if not FORCE_BOOTSTRAP and enterprise_bootstrap_already_present():
        logging.info(
            "Skipping 28-day bootstrap for enterprise=%s because Copilot daily series already exist in VictoriaMetrics",
            GH_ENTERPRISE,
        )
        bootstrapped = True
        return

    logging.info("Starting one-time 28-day bootstrap import for enterprise=%s", GH_ENTERPRISE)

    enterprise_chunks = fetch_enterprise_usage_28d()
    enterprise_rows = extract_rows(enterprise_chunks)

    user_chunks = fetch_enterprise_users_usage_28d()
    user_rows = extract_rows(user_chunks)

    lines: List[str] = []
    latest_day = None

    for row in enterprise_rows:
        if row.get("day"):
            latest_day = max(latest_day, row["day"]) if latest_day else row["day"]
        lines.extend(build_enterprise_usage_series(row))

    for row in user_rows:
        if row.get("day"):
            latest_day = max(latest_day, row["day"]) if latest_day else row["day"]
        lines.extend(build_user_usage_series(row))

    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)

    bootstrapped = True
    if latest_day:
        last_daily_import_day = latest_day

    logging.info(
        "Completed 28-day bootstrap import for enterprise=%s imported_points=%s latest_day=%s",
        GH_ENTERPRISE,
        count,
        latest_day,
    )


def import_latest_stable_day_if_needed():
    global last_daily_import_day

    target_day = (datetime.now(timezone.utc).date() - timedelta(days=DATA_LAG_DAYS)).isoformat()

    if last_daily_import_day == target_day:
        logging.info("Stable day %s already imported in this pod lifecycle; skipping", target_day)
        return

    if enterprise_day_already_present(target_day) and user_day_already_present(target_day):
        logging.info(
            "Skipping stable day import for enterprise=%s day=%s because data already exists in VictoriaMetrics",
            GH_ENTERPRISE,
            target_day,
        )
        last_daily_import_day = target_day
        return

    logging.info("Importing stable day %s for enterprise=%s", target_day, GH_ENTERPRISE)

    enterprise_chunks = fetch_enterprise_usage_for_day(target_day)
    enterprise_rows = extract_rows(enterprise_chunks)

    user_chunks = fetch_enterprise_users_usage_for_day(target_day)
    user_rows = extract_rows(user_chunks)

    lines: List[str] = []
    for row in enterprise_rows:
        lines.extend(build_enterprise_usage_series(row))
    for row in user_rows:
        lines.extend(build_user_usage_series(row))

    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)

    last_daily_import_day = target_day
    logging.info(
        "Completed stable day import for enterprise=%s day=%s imported_points=%s",
        GH_ENTERPRISE,
        target_day,
        count,
    )


def import_enterprise_seat_snapshot():
    seat_payload = fetch_enterprise_seats()
    lines = build_enterprise_seat_series(seat_payload)
    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)

    logging.info(
        "Imported enterprise seat snapshot for enterprise=%s imported_points=%s total_seats=%s seat_rows_returned=%s",
        GH_ENTERPRISE,
        count,
        seat_payload.get("total_seats"),
        seat_payload.get("seat_rows_returned"),
    )


def run_cycle():
    start = time.time()

    try:
        bootstrap_28d_once()
        import_latest_stable_day_if_needed()
        import_enterprise_seat_snapshot()

        EXPORTER_UP.labels(enterprise=GH_ENTERPRISE).set(1)
        LAST_SUCCESS.labels(enterprise=GH_ENTERPRISE).set(time.time())
    except Exception:
        ERRORS.labels(enterprise=GH_ENTERPRISE).inc()
        EXPORTER_UP.labels(enterprise=GH_ENTERPRISE).set(0)
        logging.exception("Collector cycle failed for enterprise=%s", GH_ENTERPRISE)
        raise
    finally:
        LAST_DURATION.labels(enterprise=GH_ENTERPRISE).set(time.time() - start)


def main():
    logging.info("Starting GitHub Copilot enterprise collector on :%s for enterprise=%s", EXPORTER_PORT, GH_ENTERPRISE)
    start_http_server(EXPORTER_PORT)

    while True:
        try:
            run_cycle()
        except Exception:
            pass

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()