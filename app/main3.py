import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from prometheus_client import Counter, Gauge, start_http_server


# -------------------------------------------------------------------------
# Runtime / logging configuration
# -------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

# -------------------------------------------------------------------------
# Required environment variables and runtime switches
# These control GitHub access, VictoriaMetrics access, cadence, and backfills
# -------------------------------------------------------------------------
GH_TOKEN = os.environ["GH_TOKEN"]
GH_ENTERPRISE = os.environ["GH_ENTERPRISE"]
GH_API_BASE = os.getenv("GH_API_BASE", "https://api.github.com").rstrip("/")
GH_API_VERSION = os.getenv("GH_API_VERSION", "2026-03-10")

DATA_LAG_DAYS = int(os.getenv("DATA_LAG_DAYS", "2"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "21600"))

BOOTSTRAP_28D = os.getenv("BOOTSTRAP_28D", "false").lower() == "true"
FORCE_BOOTSTRAP = os.getenv("FORCE_BOOTSTRAP", "false").lower() == "true"

ENABLE_DATE_RANGE_BACKFILL = os.getenv("ENABLE_DATE_RANGE_BACKFILL", "false").lower() == "true"
BACKFILL_START_DAY = os.getenv("BACKFILL_START_DAY", "")
BACKFILL_END_DAY = os.getenv("BACKFILL_END_DAY", "")

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

# -------------------------------------------------------------------------
# Exporter self-metrics
# These are health / operational metrics for the exporter itself
# -------------------------------------------------------------------------
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

# -------------------------------------------------------------------------
# Shared GitHub session
# This session is used only for GitHub API metadata endpoints.
# Signed report downloads are fetched with a separate session.
# -------------------------------------------------------------------------
github_session = requests.Session()
github_session.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": GH_API_VERSION,
    }
)

# -------------------------------------------------------------------------
# In-memory guardrails
# These reduce repeated work within a single pod lifecycle.
# Persistent duplicate prevention is enforced through VictoriaMetrics checks.
# -------------------------------------------------------------------------
last_daily_import_day: Optional[str] = None
bootstrapped = False
date_range_backfill_done = False


# -------------------------------------------------------------------------
# Generic helper functions
# These helpers cover auth, parsing, date handling, metric naming, and
# lightweight value normalization.
# -------------------------------------------------------------------------
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
    """
    GitHub report APIs return signed URLs. These URLs should be fetched without
    reusing the GitHub auth/session headers.
    """
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


def safe_parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def datetime_to_unix_seconds(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return float(dt.timestamp())


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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def coerce_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def normalize_metric_suffix(name: str) -> str:
    chars = []
    for ch in str(name).strip().lower():
        if ch.isalnum():
            chars.append(ch)
        else:
            chars.append("_")
    out = "".join(chars)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "value"


def first_editor_name(last_activity_editor: str) -> str:
    """
    Example input: vscode/1.77.3/copilot/1.86.82
    Returns: vscode
    """
    if not last_activity_editor:
        return "unknown"
    return str(last_activity_editor).split("/", 1)[0] or "unknown"


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


def append_timestamp_value(
    lines: List[str],
    metric_name: str,
    labels: Dict[str, str],
    dt_value: Optional[str],
    ts_ms: int,
):
    dt = safe_parse_iso_datetime(dt_value)
    if dt is None:
        return
    append_point(lines, metric_name, labels, datetime_to_unix_seconds(dt), ts_ms)


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


def flatten_numeric_fields(prefix: str, obj: Dict[str, Any]) -> Dict[str, float]:
    """
    Flattens nested numeric dictionaries into:
    prefix_field_subfield -> value

    This is used where GitHub adds new numeric metrics over time and we want to
    ingest them safely without hard-coding every key.
    """
    out: Dict[str, float] = {}

    def _walk(current_prefix: str, value: Any):
        if isinstance(value, dict):
            for key, nested in value.items():
                _walk(f"{current_prefix}_{normalize_metric_suffix(key)}", nested)
        else:
            num = coerce_number(value)
            if num is not None:
                out[current_prefix] = num

    _walk(prefix, obj)
    return out


def ai_phase_labels(value: Any) -> Tuple[str, str]:
    """
    Supports either:
    - {"phase": "phase_1", "version": "v1"}
    - {"name": "phase_1", "version": "v1"}
    - {"ai_adoption_phase": "phase_1", "version": "v1"}
    - "phase_1"
    """
    if isinstance(value, dict):
        phase = (
            value.get("phase")
            or value.get("name")
            or value.get("ai_adoption_phase")
            or value.get("value")
            or "unknown"
        )
        version = value.get("version", "v1")
        return str(phase), str(version)

    if value is None:
        return "unknown", "v1"

    return str(value), "v1"


# -------------------------------------------------------------------------
# GitHub report fetchers
# These call the enterprise Copilot usage report APIs and seat assignment APIs.
# -------------------------------------------------------------------------
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
    """
    Seat assignment details are used as enterprise billing / license telemetry.
    This also gives us user-level seat assignment and last-activity information.
    """
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

        page_rows = payload.get("seats", [])
        seats.extend(page_rows)

        if not page_rows or len(page_rows) < 50:
            break

        page += 1

    return {
        "total_seats": total_seats or 0,
        "seat_rows_returned": len(seats),
        "seats": seats,
    }


# -------------------------------------------------------------------------
# VictoriaMetrics existence checks
# These checks make the importer idempotent across restarts and manual reruns.
# -------------------------------------------------------------------------
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
    end = now_utc()
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


# -------------------------------------------------------------------------
# Enterprise usage metric builders
# These translate enterprise usage report rows into time-series metrics.
# -------------------------------------------------------------------------
def build_enterprise_ai_adoption_phase_series(row: Dict[str, Any], ts_ms: int) -> List[str]:
    lines: List[str] = []

    for item in row.get("totals_by_ai_adoption_phase") or []:
        phase_value = item.get("ai_adoption_phase") or item.get("phase") or item.get("name")
        phase, version = ai_phase_labels(phase_value if phase_value is not None else item)

        labels = {
            "enterprise": GH_ENTERPRISE,
            "ai_adoption_phase": phase,
            "ai_adoption_phase_version": version,
        }

        for key, value in item.items():
            if key in {"ai_adoption_phase", "phase", "name", "version"}:
                continue

            # Flatten nested numeric objects if GitHub expands this structure later.
            if isinstance(value, dict):
                for metric_suffix, metric_value in flatten_numeric_fields(
                    f"github_copilot_ai_adoption_phase_{normalize_metric_suffix(key)}",
                    value,
                ).items():
                    append_point(lines, metric_suffix, labels, metric_value, ts_ms)
                continue

            append_point(
                lines,
                f"github_copilot_ai_adoption_phase_{normalize_metric_suffix(key)}",
                labels,
                value,
                ts_ms,
            )

        # Helpful presence metric for phase breakdown dashboards.
        append_point(lines, "github_copilot_ai_adoption_phase_record", labels, 1, ts_ms)

    return lines


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

    # New GitHub-published AI adoption cohorts / phases.
    lines.extend(build_enterprise_ai_adoption_phase_series(row, ts_ms))

    return lines


# -------------------------------------------------------------------------
# User usage metric builders
# These translate user rows into per-user time-series metrics and labels.
# -------------------------------------------------------------------------
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

    # New GitHub-published user AI adoption phase.
    phase, phase_version = ai_phase_labels(row.get("ai_adoption_phase"))
    ai_phase_labels_map = {
        "enterprise": GH_ENTERPRISE,
        "user_login": user_login,
        "user_id": user_id,
        "ai_adoption_phase": phase,
        "ai_adoption_phase_version": phase_version,
    }
    append_point(lines, "github_copilot_user_ai_adoption_phase_daily_record", ai_phase_labels_map, 1, ts_ms)

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

    for item in row.get("totals_by_language_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "language": str(item.get("language", "unknown")),
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(lines, "github_copilot_user_language_feature_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)
        append_point(lines, "github_copilot_user_language_feature_prompt_count", labels, item.get("user_initiated_interaction_count"), ts_ms)

    for item in row.get("totals_by_model_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "model": str(item.get("model", "unknown")),
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(lines, "github_copilot_user_model_feature_prompt_count", labels, item.get("user_initiated_interaction_count"), ts_ms)
        append_point(lines, "github_copilot_user_model_feature_loc_added_sum", labels, item.get("loc_added_sum"), ts_ms)

    return lines


# -------------------------------------------------------------------------
# Enterprise billing / seat metric builders
# These translate seat assignment data into both aggregate billing metrics and
# user-level seat / activity metrics.
# -------------------------------------------------------------------------
def build_enterprise_seat_series(seat_payload: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    ts_ms = now_ms()
    labels = {"enterprise": GH_ENTERPRISE}
    seats = seat_payload.get("seats", []) or []

    append_point(lines, "github_copilot_enterprise_seat_total", labels, seat_payload.get("total_seats"), ts_ms)
    append_point(lines, "github_copilot_enterprise_seat_rows_returned", labels, seat_payload.get("seat_rows_returned"), ts_ms)

    pending_cancellation_total = 0
    never_active_total = 0
    active_last_7d = 0
    active_last_28d = 0
    active_last_90d = 0

    now = now_utc()
    plan_type_counts: Dict[str, int] = {}
    assigning_team_counts: Dict[str, int] = {}
    activity_tool_counts: Dict[str, int] = {}

    for seat in seats:
        assignee = seat.get("assignee") or {}
        user_login = str(assignee.get("login", "unknown"))
        user_id = str(assignee.get("id", "unknown"))
        plan_type = str(seat.get("plan_type", "unknown"))
        assigning_team = "direct"
        if isinstance(seat.get("assigning_team"), dict):
            assigning_team = str(
                seat["assigning_team"].get("slug")
                or seat["assigning_team"].get("name")
                or "team"
            )

        pending_cancellation = seat.get("pending_cancellation_date") is not None
        if pending_cancellation:
            pending_cancellation_total += 1

        plan_type_counts[plan_type] = plan_type_counts.get(plan_type, 0) + 1
        assigning_team_counts[assigning_team] = assigning_team_counts.get(assigning_team, 0) + 1

        last_activity_editor = str(seat.get("last_activity_editor") or "")
        activity_tool = first_editor_name(last_activity_editor) if last_activity_editor else "unknown"
        activity_tool_counts[activity_tool] = activity_tool_counts.get(activity_tool, 0) + 1

        last_activity_dt = safe_parse_iso_datetime(seat.get("last_activity_at"))
        if last_activity_dt is None:
            never_active_total += 1
        else:
            age = now - last_activity_dt
            if age <= timedelta(days=7):
                active_last_7d += 1
            if age <= timedelta(days=28):
                active_last_28d += 1
            if age <= timedelta(days=90):
                active_last_90d += 1

        user_labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "plan_type": plan_type,
            "assigning_team": assigning_team,
            "pending_cancellation": "true" if pending_cancellation else "false",
        }

        append_point(lines, "github_copilot_enterprise_seat_user_record", user_labels, 1, ts_ms)

        if last_activity_editor:
            tool_labels = {
                **user_labels,
                "activity_tool": activity_tool,
            }
            append_point(lines, "github_copilot_enterprise_seat_user_last_activity_tool_record", tool_labels, 1, ts_ms)

        append_timestamp_value(
            lines,
            "github_copilot_enterprise_seat_user_created_unixtime_seconds",
            user_labels,
            seat.get("created_at"),
            ts_ms,
        )
        append_timestamp_value(
            lines,
            "github_copilot_enterprise_seat_user_updated_unixtime_seconds",
            user_labels,
            seat.get("updated_at"),
            ts_ms,
        )
        append_timestamp_value(
            lines,
            "github_copilot_enterprise_seat_user_last_activity_unixtime_seconds",
            user_labels,
            seat.get("last_activity_at"),
            ts_ms,
        )
        append_timestamp_value(
            lines,
            "github_copilot_enterprise_seat_user_last_authenticated_unixtime_seconds",
            user_labels,
            seat.get("last_authenticated_at"),
            ts_ms,
        )
        append_timestamp_value(
            lines,
            "github_copilot_enterprise_seat_user_pending_cancellation_unixtime_seconds",
            user_labels,
            seat.get("pending_cancellation_date"),
            ts_ms,
        )

    append_point(lines, "github_copilot_enterprise_seat_pending_cancellation_total", labels, pending_cancellation_total, ts_ms)
    append_point(lines, "github_copilot_enterprise_seat_never_active_total", labels, never_active_total, ts_ms)
    append_point(lines, "github_copilot_enterprise_seat_active_last_7d", labels, active_last_7d, ts_ms)
    append_point(lines, "github_copilot_enterprise_seat_active_last_28d", labels, active_last_28d, ts_ms)
    append_point(lines, "github_copilot_enterprise_seat_active_last_90d", labels, active_last_90d, ts_ms)

    for plan_type, count in plan_type_counts.items():
        append_point(
            lines,
            "github_copilot_enterprise_seat_plan_type_total",
            {"enterprise": GH_ENTERPRISE, "plan_type": plan_type},
            count,
            ts_ms,
        )

    for assigning_team, count in assigning_team_counts.items():
        append_point(
            lines,
            "github_copilot_enterprise_seat_assigning_team_total",
            {"enterprise": GH_ENTERPRISE, "assigning_team": assigning_team},
            count,
            ts_ms,
        )

    for activity_tool, count in activity_tool_counts.items():
        append_point(
            lines,
            "github_copilot_enterprise_seat_last_activity_tool_total",
            {"enterprise": GH_ENTERPRISE, "activity_tool": activity_tool},
            count,
            ts_ms,
        )

    return lines


# -------------------------------------------------------------------------
# VictoriaMetrics writer
# This writes the transformed metrics to the JSON-line import API.
# -------------------------------------------------------------------------
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


# -------------------------------------------------------------------------
# Import orchestration
# These functions handle day-level imports, backfills, bootstraps, and seats.
# -------------------------------------------------------------------------
def import_enterprise_day(day: str) -> int:
    if enterprise_day_already_present(day) and user_day_already_present(day):
        logging.info(
            "Skipping enterprise day=%s because enterprise and user data already exist in VictoriaMetrics",
            day,
        )
        return 0

    enterprise_chunks = fetch_enterprise_usage_for_day(day)
    enterprise_rows = extract_rows(enterprise_chunks)

    user_chunks = fetch_enterprise_users_usage_for_day(day)
    user_rows = extract_rows(user_chunks)

    lines: List[str] = []

    for row in enterprise_rows:
        lines.extend(build_enterprise_usage_series(row))

    for row in user_rows:
        lines.extend(build_user_usage_series(row))

    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)

    logging.info(
        "Imported enterprise day=%s imported_points=%s",
        day,
        count,
    )
    return count


def backfill_date_range_once():
    global date_range_backfill_done

    if not ENABLE_DATE_RANGE_BACKFILL or date_range_backfill_done:
        return

    if not BACKFILL_START_DAY or not BACKFILL_END_DAY:
        raise RuntimeError("ENABLE_DATE_RANGE_BACKFILL=true but BACKFILL_START_DAY or BACKFILL_END_DAY is missing")

    start_date = datetime.strptime(BACKFILL_START_DAY, "%Y-%m-%d").date()
    end_date = datetime.strptime(BACKFILL_END_DAY, "%Y-%m-%d").date()

    if end_date < start_date:
        raise RuntimeError("BACKFILL_END_DAY must be >= BACKFILL_START_DAY")

    logging.info(
        "Starting enterprise date-range backfill for enterprise=%s start=%s end=%s",
        GH_ENTERPRISE,
        BACKFILL_START_DAY,
        BACKFILL_END_DAY,
    )

    total_points = 0
    current = start_date
    while current <= end_date:
        day = current.isoformat()
        total_points += import_enterprise_day(day)
        current += timedelta(days=1)

    date_range_backfill_done = True

    logging.info(
        "Completed enterprise date-range backfill for enterprise=%s start=%s end=%s imported_points=%s",
        GH_ENTERPRISE,
        BACKFILL_START_DAY,
        BACKFILL_END_DAY,
        total_points,
    )


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

    target_day = (now_utc().date() - timedelta(days=DATA_LAG_DAYS)).isoformat()

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

    count = import_enterprise_day(target_day)
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


# -------------------------------------------------------------------------
# Main collector cycle
# This is the top-level orchestration loop used by the running exporter.
# -------------------------------------------------------------------------
def run_cycle():
    start = time.time()

    try:
        backfill_date_range_once()
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
    logging.info(
        "Starting GitHub Copilot enterprise collector on :%s for enterprise=%s",
        EXPORTER_PORT,
        GH_ENTERPRISE,
    )
    start_http_server(EXPORTER_PORT)

    while True:
        try:
            run_cycle()
        except Exception:
            # Keep the process alive so health metrics remain scrapeable.
            pass

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
