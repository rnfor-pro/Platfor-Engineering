import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from prometheus_client import Counter, Gauge, start_http_server


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)


# -----------------------------------------------------------------------------
# Runtime configuration
# -----------------------------------------------------------------------------
GH_TOKEN = os.environ["GH_TOKEN"]
GH_ENTERPRISE = os.environ["GH_ENTERPRISE"]
GH_API_BASE = os.getenv("GH_API_BASE", "https://api.github.com").rstrip("/")
GH_API_VERSION = os.getenv("GH_API_VERSION", "2026-03-10")

GH_BILLING_ORGS = [
    org.strip()
    for org in os.getenv("GH_BILLING_ORGS", "").split(",")
    if org.strip()
]

DATA_LAG_DAYS = int(os.getenv("DATA_LAG_DAYS", "2"))

# POLL_INTERVAL_SECONDS controls how often the main loop runs.
# Default is 21600 (6 hours).  Set via manifest env var.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "21600"))

BOOTSTRAP_28D = os.getenv("BOOTSTRAP_28D", "false").lower() == "true"
FORCE_BOOTSTRAP = os.getenv("FORCE_BOOTSTRAP", "false").lower() == "true"

ENABLE_DATE_RANGE_BACKFILL = (
    os.getenv("ENABLE_DATE_RANGE_BACKFILL", "false").lower() == "true"
)
BACKFILL_START_DAY = os.getenv("BACKFILL_START_DAY", "")
BACKFILL_END_DAY = os.getenv("BACKFILL_END_DAY", "")

ENABLE_BILLING_REPORTS = (
    os.getenv("ENABLE_BILLING_REPORTS", "true").lower() == "true"
)

EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "8080"))

VM_IMPORT_URL = os.environ["VM_IMPORT_URL"]
VM_SERIES_URL = os.getenv(
    "VM_SERIES_URL",
    "http://dev-victoriametrics-victoria-metrics-single-server"
    ".dev-keystone.svc.cluster.local:8428/prometheus/api/v1/series",
)
VM_USERNAME = os.getenv("VM_USERNAME", "")
VM_PASSWORD = os.getenv("VM_PASSWORD", "")
VM_BEARER_TOKEN = os.getenv("VM_BEARER_TOKEN", "")

# -----------------------------------------------------------------------------
# Loki configuration
# If LOKI_ENDPOINT is not set, Loki output is disabled entirely.
# The exporter continues writing to VictoriaMetrics regardless.
# -----------------------------------------------------------------------------
LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "")
LOKI_TENANT_ID = os.getenv("LOKI_TENANT_ID", "")
LOKI_BATCH_SIZE = int(os.getenv("LOKI_BATCH_SIZE", "100"))
LOKI_TIMEOUT_SEC = int(os.getenv("LOKI_TIMEOUT_SEC", "30"))
LOKI_QUERY_URL = os.getenv(
    "LOKI_QUERY_URL",
    LOKI_ENDPOINT.replace("/loki/api/v1/push", "") if LOKI_ENDPOINT else "",
)

HTTP_TIMEOUT = 60


# -----------------------------------------------------------------------------
# Exporter self-observability
# -----------------------------------------------------------------------------
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
LOKI_PUSHED_LINES = Counter(
    "github_copilot_exporter_loki_pushed_lines_total",
    "How many Loki log lines were successfully pushed",
    ["enterprise"],
)
LOKI_PUSH_ERRORS = Counter(
    "github_copilot_exporter_loki_push_errors_total",
    "How many Loki push attempts failed",
    ["enterprise"],
)
ERRORS = Counter(
    "github_copilot_exporter_errors_total",
    "How many collector cycles failed",
    ["enterprise"],
)


# -----------------------------------------------------------------------------
# GitHub HTTP session
# -----------------------------------------------------------------------------
github_session = requests.Session()
github_session.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": GH_API_VERSION,
    }
)


# -----------------------------------------------------------------------------
# In-memory run state
# -----------------------------------------------------------------------------
last_daily_import_day: Optional[str] = None
bootstrapped = False
date_range_backfill_done = False
loki_enabled = False  # set to True after successful health check at startup


# -----------------------------------------------------------------------------
# Auth helpers
# -----------------------------------------------------------------------------
def vm_auth() -> Tuple[Optional[Tuple[str, str]], Dict[str, str]]:
    if VM_BEARER_TOKEN:
        return None, {"Authorization": f"Bearer {VM_BEARER_TOKEN}"}
    if VM_USERNAME and VM_PASSWORD:
        return (VM_USERNAME, VM_PASSWORD), {}
    return None, {}


def loki_headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if LOKI_TENANT_ID:
        h["X-Scope-OrgID"] = LOKI_TENANT_ID
    return h


# -----------------------------------------------------------------------------
# GitHub request helpers
# -----------------------------------------------------------------------------
def github_get_json(
    url: str, params: Optional[Dict[str, Any]] = None
) -> Any:
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
    Signed report URLs are short-lived and hosted outside api.github.com.
    Use a fresh unauthenticated session so GitHub auth headers are not leaked.
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


# -----------------------------------------------------------------------------
# GitHub API fetch functions (unchanged from original)
# -----------------------------------------------------------------------------
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


def fetch_enterprise_user_teams_for_day(day: str) -> List[Any]:
    url = f"{GH_API_BASE}/enterprises/{GH_ENTERPRISE}/copilot/metrics/reports/user-teams-1-day"
    meta = github_get_json(url, params={"day": day})
    return download_report_chunks(meta.get("download_links", []))


def fetch_enterprise_seats() -> Dict[str, Any]:
    url = f"{GH_API_BASE}/enterprises/{GH_ENTERPRISE}/copilot/billing/seats"
    page = 1
    total_seats = None
    seats: List[Dict[str, Any]] = []
    while True:
        resp = github_session.get(
            url, params={"page": page}, timeout=HTTP_TIMEOUT
        )
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


def fetch_org_billing_premium_request_usage(
    org: str, day: str
) -> Dict[str, Any]:
    dt = datetime.strptime(day, "%Y-%m-%d")
    url = f"{GH_API_BASE}/organizations/{org}/settings/billing/premium_request/usage"
    return github_get_json(
        url,
        params={
            "year": dt.year,
            "month": dt.month,
            "day": dt.day,
            "product": "Copilot",
        },
    )


def fetch_org_billing_ai_credit_usage(
    org: str, day: str
) -> Dict[str, Any]:
    dt = datetime.strptime(day, "%Y-%m-%d")
    url = f"{GH_API_BASE}/organizations/{org}/settings/billing/ai_credit/usage"
    return github_get_json(
        url,
        params={
            "year": dt.year,
            "month": dt.month,
            "day": dt.day,
            "product": "Copilot",
        },
    )


# -----------------------------------------------------------------------------
# Time helpers (unchanged)
# -----------------------------------------------------------------------------
def day_to_ms(day_str: str) -> int:
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def day_to_ns(day_str: str) -> int:
    """Convert YYYY-MM-DD to nanoseconds since epoch at noon UTC.
    Used for Loki timestamps which must be nanosecond strings."""
    return day_to_ms(day_str) * 1_000_000


def parse_any_time_to_ms(value: str) -> Optional[int]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def day_bounds(day_str: str) -> Tuple[str, str]:
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# -----------------------------------------------------------------------------
# Value helpers (unchanged)
# -----------------------------------------------------------------------------
def coerce_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def vm_json_line(
    metric_name: str,
    labels: Dict[str, str],
    value: float,
    ts_ms: int,
) -> str:
    obj = {
        "metric": {
            "__name__": metric_name,
            **{k: str(v) for k, v in labels.items()},
        },
        "values": [value],
        "timestamps": [ts_ms],
    }
    return json.dumps(obj, separators=(",", ":"))


def append_point(
    lines: List[str],
    metric_name: str,
    labels: Dict[str, str],
    value: Any,
    ts_ms: int,
):
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
                    rows.extend(
                        [
                            r
                            for r in item["day_totals"]
                            if isinstance(r, dict)
                        ]
                    )
        elif isinstance(chunk, dict) and "day_totals" in chunk:
            rows.extend(
                [r for r in chunk["day_totals"] if isinstance(r, dict)]
            )
        elif isinstance(chunk, dict) and "day" in chunk:
            rows.append(chunk)
    return rows


# -----------------------------------------------------------------------------
# VictoriaMetrics duplicate detection (unchanged)
# -----------------------------------------------------------------------------
def vm_series_exists(
    matchers: List[str], start: str, end: str
) -> bool:
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
    return len(payload.get("data", [])) > 0


def enterprise_bootstrap_already_present() -> bool:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    matcher = (
        f'github_copilot_daily_active_users{{enterprise="{GH_ENTERPRISE}"}}'
    )
    return vm_series_exists([matcher], start.isoformat(), end.isoformat())


def enterprise_day_already_present(day: str) -> bool:
    start, end = day_bounds(day)
    matcher = (
        f'github_copilot_daily_active_users{{enterprise="{GH_ENTERPRISE}"}}'
    )
    return vm_series_exists([matcher], start, end)


def user_day_already_present(day: str) -> bool:
    start, end = day_bounds(day)
    matcher = (
        f'github_copilot_user_daily_record{{enterprise="{GH_ENTERPRISE}"}}'
    )
    return vm_series_exists([matcher], start, end)


def user_teams_day_already_present(day: str) -> bool:
    start, end = day_bounds(day)
    matcher = (
        f'github_copilot_user_team_membership{{enterprise="{GH_ENTERPRISE}"}}'
    )
    return vm_series_exists([matcher], start, end)


def billing_day_already_present(day: str) -> bool:
    start, end = day_bounds(day)
    matcher = (
        f'github_copilot_billing_day_marker{{enterprise="{GH_ENTERPRISE}"}}'
    )
    return vm_series_exists([matcher], start, end)


# -----------------------------------------------------------------------------
# Loki duplicate detection
# One GET per day (not per user) guards the entire batch.
# Returns True if any log lines for this enterprise+day already exist in Loki.
# -----------------------------------------------------------------------------
def loki_day_already_present(day: str) -> bool:
    """
    Query Loki for any stream matching:
      {enterprise="...", log_source="copilot_exporter"}
    within the day's UTC window.
    Returns True if at least one log line exists → skip push.
    """
    if not LOKI_QUERY_URL:
        return False

    start_ns, end_ns = _day_ns_bounds(day)
    query = (
        f'{{enterprise="{GH_ENTERPRISE}",log_source="copilot_exporter"}}'
    )
    try:
        resp = requests.get(
            f"{LOKI_QUERY_URL}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": "1",
                "direction": "backward",
            },
            headers=loki_headers(),
            timeout=LOKI_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        return len(results) > 0
    except Exception as exc:
        logging.warning(
            "loki: dedup check failed for day=%s, will attempt push: %s",
            day,
            exc,
        )
        return False


def _day_ns_bounds(day: str) -> Tuple[int, int]:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    start_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    return (
        int(start_dt.timestamp() * 1_000_000_000),
        int(end_dt.timestamp() * 1_000_000_000),
    )


# -----------------------------------------------------------------------------
# Loki health check — called at startup
# Sets the module-level loki_enabled flag.
# -----------------------------------------------------------------------------
def check_loki_health() -> bool:
    global loki_enabled
    if not LOKI_ENDPOINT:
        logging.info("loki: LOKI_ENDPOINT not set, Loki output disabled")
        loki_enabled = False
        return False

    ready_url = LOKI_QUERY_URL.rstrip("/") + "/ready"
    try:
        resp = requests.get(ready_url, timeout=5)
        if resp.status_code == 200:
            logging.info(
                "loki: health check OK (%s), Loki output enabled", ready_url
            )
            loki_enabled = True
            return True
        logging.warning(
            "loki: /ready returned HTTP %d, Loki output disabled",
            resp.status_code,
        )
    except Exception as exc:
        logging.warning(
            "loki: health check failed (%s), Loki output disabled: %s",
            ready_url,
            exc,
        )
    loki_enabled = False
    return False


# -----------------------------------------------------------------------------
# Loki push — one structured JSON log line per user per day
# -----------------------------------------------------------------------------
def _build_loki_stream(
    user_row: Dict[str, Any],
    day: str,
) -> Dict[str, Any]:
    """
    Build one Loki stream entry for a single user-day record.

    Stream labels (low-cardinality — kept to 5):
      enterprise, user_login, ai_adoption_phase, log_source, deployment_env

    Log body (JSON string, parseable with | json in LogQL):
      All available fields from the users-1-day NDJSON record.

    Timestamp: noon UTC of the report day, nanoseconds, as a string.
    This matches the VictoriaMetrics timestamp convention for the same record
    so the two backends can be correlated by day.
    """
    login = str(user_row.get("user_login") or "unknown")
    user_id = str(user_row.get("user_id") or "unknown")

    # Resolve ai_adoption_phase — same normalisation as build_user_usage_series
    phase = user_row.get("ai_adoption_phase")
    if isinstance(phase, dict):
        phase_name = str(phase.get("name", "unknown"))
        phase_version = str(phase.get("version", "unknown"))
    elif phase:
        phase_name = str(phase)
        phase_version = "unknown"
    else:
        phase_name = "unknown"
        phase_version = "unknown"

    # CLI token fields — only present when CLI usage exists
    cli = user_row.get("totals_by_cli") or {}
    cli_token = cli.get("token_usage") or {}
    cli_version_obj = cli.get("last_known_cli_version") or {}
    cli_version = (
        str(cli_version_obj.get("cli_version", ""))
        if isinstance(cli_version_obj, dict)
        else ""
    )

    # Build log body — everything the dashboard may need
    body: Dict[str, Any] = {
        # Identifiers
        "day": day,
        "enterprise": GH_ENTERPRISE,
        "user_id": user_id,
        "user_login": login,
        # Adoption phase
        "ai_adoption_phase": phase_name,
        "ai_adoption_phase_version": phase_version,
        # AI credits (June 19 2026 field — overall per-user daily total)
        # 1 AI Credit = $0.01 USD
        "ai_credits_used": user_row.get("ai_credits_used"),
        "ai_credits_usd": (
            round(float(user_row["ai_credits_used"]) * 0.01, 6)
            if user_row.get("ai_credits_used") is not None
            else None
        ),
        # Feature activity flags
        "used_chat": bool(user_row.get("used_chat")),
        "used_agent": bool(user_row.get("used_agent")),
        "used_cli": bool(user_row.get("used_cli")),
        # Top-level activity totals
        "prompt_count": user_row.get("user_initiated_interaction_count"),
        "code_generation_activity_count": user_row.get(
            "code_generation_activity_count"
        ),
        "code_acceptance_activity_count": user_row.get(
            "code_acceptance_activity_count"
        ),
        "loc_suggested_to_add_sum": user_row.get("loc_suggested_to_add_sum"),
        "loc_suggested_to_delete_sum": user_row.get(
            "loc_suggested_to_delete_sum"
        ),
        "loc_added_sum": user_row.get("loc_added_sum"),
        "loc_deleted_sum": user_row.get("loc_deleted_sum"),
        # Chat panel mode breakdown
        "chat_panel_agent_mode": user_row.get("chat_panel_agent_mode"),
        "chat_panel_ask_mode": user_row.get("chat_panel_ask_mode"),
        "chat_panel_edit_mode": user_row.get("chat_panel_edit_mode"),
        "chat_panel_custom_mode": user_row.get("chat_panel_custom_mode"),
        # CLI specifics
        "cli_session_count": cli.get("session_count"),
        "cli_request_count": cli.get("request_count"),
        "cli_prompt_count": cli.get("prompt_count"),
        "cli_output_tokens_sum": cli_token.get("output_tokens_sum"),
        "cli_prompt_tokens_sum": cli_token.get("prompt_tokens_sum"),
        "cli_avg_tokens_per_request": cli_token.get("avg_tokens_per_request"),
        "cli_version": cli_version or None,
        # Model breakdown — array preserved for LogQL unwrap queries
        "totals_by_model_feature": _summarise_model_feature(user_row),
        # Feature breakdown
        "totals_by_feature": _summarise_feature(user_row),
        # IDE breakdown (without version noise in the summary)
        "totals_by_ide": _summarise_ide(user_row),
        # Language breakdown
        "totals_by_language_feature": _summarise_language_feature(user_row),
    }

    # Timestamp must be a STRING in nanoseconds per Loki API spec.
    ts_ns_str = str(day_to_ns(day))

    return {
        "stream": {
            "enterprise": GH_ENTERPRISE,
            "user_login": login,
            "ai_adoption_phase": phase_name,
            "log_source": "copilot_exporter",
            "deployment_env": os.getenv("DEPLOYMENT_ENV", "prod"),
        },
        "values": [
            [ts_ns_str, json.dumps(body, default=str)]
        ],
    }


def _summarise_model_feature(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Aggregate totals_by_model_feature across all IDEs into a flat list."""
    agg: Dict[str, Dict[str, Any]] = {}
    for item in row.get("totals_by_model_feature") or []:
        key = f"{item.get('model','unknown')}|{item.get('feature','unknown')}"
        if key not in agg:
            agg[key] = {
                "model": item.get("model", "unknown"),
                "feature": item.get("feature", "unknown"),
                "prompt_count": 0,
                "loc_added_sum": 0,
            }
        agg[key]["prompt_count"] += int(
            item.get("user_initiated_interaction_count") or 0
        )
        agg[key]["loc_added_sum"] += int(item.get("loc_added_sum") or 0)
    return sorted(
        agg.values(), key=lambda x: x["prompt_count"], reverse=True
    )


def _summarise_feature(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for item in row.get("totals_by_feature") or []:
        feat = str(item.get("feature", "unknown"))
        if feat not in agg:
            agg[feat] = {"feature": feat, "prompt_count": 0, "loc_added_sum": 0}
        agg[feat]["prompt_count"] += int(
            item.get("user_initiated_interaction_count") or 0
        )
        agg[feat]["loc_added_sum"] += int(item.get("loc_added_sum") or 0)
    return sorted(
        agg.values(), key=lambda x: x["prompt_count"], reverse=True
    )


def _summarise_ide(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for item in row.get("totals_by_ide") or []:
        ide = str(item.get("ide", "unknown"))
        if ide not in agg:
            agg[ide] = {"ide": ide, "prompt_count": 0, "loc_added_sum": 0}
        agg[ide]["prompt_count"] += int(
            item.get("user_initiated_interaction_count") or 0
        )
        agg[ide]["loc_added_sum"] += int(item.get("loc_added_sum") or 0)
    return sorted(
        agg.values(), key=lambda x: x["prompt_count"], reverse=True
    )


def _summarise_language_feature(
    row: Dict[str, Any],
) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for item in row.get("totals_by_language_feature") or []:
        key = f"{item.get('language','unknown')}|{item.get('feature','unknown')}"
        if key not in agg:
            agg[key] = {
                "language": item.get("language", "unknown"),
                "feature": item.get("feature", "unknown"),
                "prompt_count": 0,
                "loc_added_sum": 0,
            }
        agg[key]["prompt_count"] += int(
            item.get("user_initiated_interaction_count") or 0
        )
        agg[key]["loc_added_sum"] += int(item.get("loc_added_sum") or 0)
    return sorted(
        agg.values(), key=lambda x: x["prompt_count"], reverse=True
    )


def push_user_day_to_loki(user_rows: List[Dict[str, Any]], day: str) -> int:
    """
    Push all user rows for a given day to Loki.

    - Checks dedup first (one GET per day).
    - Batches into LOKI_BATCH_SIZE streams per HTTP POST.
    - Returns total lines pushed.
    - Does NOT raise — errors are logged and counted but do not fail the cycle.
      VictoriaMetrics write already succeeded at this point.
    """
    if not loki_enabled:
        return 0

    if loki_day_already_present(day):
        logging.info(
            "loki: day=%s already present for enterprise=%s, skipping push",
            day,
            GH_ENTERPRISE,
        )
        return 0

    streams = []
    for row in user_rows:
        try:
            streams.append(_build_loki_stream(row, day))
        except Exception as exc:
            logging.warning(
                "loki: failed to build stream for user=%s day=%s: %s",
                row.get("user_login", "?"),
                day,
                exc,
            )

    if not streams:
        return 0

    total_pushed = 0
    headers = loki_headers()

    for batch_start in range(0, len(streams), LOKI_BATCH_SIZE):
        batch = streams[batch_start : batch_start + LOKI_BATCH_SIZE]
        payload = json.dumps({"streams": batch}, default=str)
        try:
            resp = requests.post(
                LOKI_ENDPOINT,
                data=payload,
                headers=headers,
                timeout=LOKI_TIMEOUT_SEC,
            )
            if resp.status_code in (200, 204):
                total_pushed += len(batch)
                LOKI_PUSHED_LINES.labels(enterprise=GH_ENTERPRISE).inc(
                    len(batch)
                )
                logging.debug(
                    "loki: pushed %d lines day=%s batch_start=%d",
                    len(batch),
                    day,
                    batch_start,
                )
            else:
                LOKI_PUSH_ERRORS.labels(enterprise=GH_ENTERPRISE).inc()
                logging.error(
                    "loki: HTTP %d for day=%s batch_start=%d: %s",
                    resp.status_code,
                    day,
                    batch_start,
                    resp.text[:300],
                )
        except requests.RequestException as exc:
            LOKI_PUSH_ERRORS.labels(enterprise=GH_ENTERPRISE).inc()
            logging.error(
                "loki: request failed day=%s batch_start=%d: %s",
                day,
                batch_start,
                exc,
            )

    logging.info(
        "loki: completed day=%s enterprise=%s pushed=%d/%d",
        day,
        GH_ENTERPRISE,
        total_pushed,
        len(streams),
    )
    return total_pushed


# -----------------------------------------------------------------------------
# Enterprise usage metric builders (unchanged from original)
# -----------------------------------------------------------------------------
def build_enterprise_usage_series(row: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    day = row["day"]
    ts_ms = day_to_ms(day)
    base = {"enterprise": GH_ENTERPRISE}

    root_fields = {
        "github_copilot_daily_active_users": row.get("daily_active_users"),
        "github_copilot_weekly_active_users": row.get("weekly_active_users"),
        "github_copilot_monthly_active_users": row.get("monthly_active_users"),
        "github_copilot_monthly_active_chat_users": row.get(
            "monthly_active_chat_users"
        ),
        "github_copilot_monthly_active_agent_users": row.get(
            "monthly_active_agent_users"
        ),
        "github_copilot_daily_active_cli_users": row.get(
            "daily_active_cli_users"
        ),
        "github_copilot_user_initiated_interaction_count": row.get(
            "user_initiated_interaction_count"
        ),
        "github_copilot_code_generation_activity_count": row.get(
            "code_generation_activity_count"
        ),
        "github_copilot_code_acceptance_activity_count": row.get(
            "code_acceptance_activity_count"
        ),
        "github_copilot_loc_suggested_to_add_sum": row.get(
            "loc_suggested_to_add_sum"
        ),
        "github_copilot_loc_suggested_to_delete_sum": row.get(
            "loc_suggested_to_delete_sum"
        ),
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
        "github_copilot_pr_median_minutes_to_merge": pr.get(
            "median_minutes_to_merge"
        ),
        "github_copilot_pr_total_suggestions": pr.get("total_suggestions"),
        "github_copilot_pr_total_applied_suggestions": pr.get(
            "total_applied_suggestions"
        ),
        "github_copilot_pr_total_created_by_copilot": pr.get(
            "total_created_by_copilot"
        ),
        "github_copilot_pr_total_reviewed_by_copilot": pr.get(
            "total_reviewed_by_copilot"
        ),
        "github_copilot_pr_total_merged_created_by_copilot": pr.get(
            "total_merged_created_by_copilot"
        ),
        "github_copilot_pr_median_minutes_to_merge_copilot_authored": pr.get(
            "median_minutes_to_merge_copilot_authored"
        ),
        "github_copilot_pr_total_copilot_suggestions": pr.get(
            "total_copilot_suggestions"
        ),
        "github_copilot_pr_total_copilot_applied_suggestions": pr.get(
            "total_copilot_applied_suggestions"
        ),
    }
    for metric_name, value in pr_fields.items():
        append_point(lines, metric_name, base, value, ts_ms)

    for item in row.get("totals_by_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(
            lines,
            "github_copilot_feature_loc_added_sum",
            labels,
            item.get("loc_added_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_feature_loc_deleted_sum",
            labels,
            item.get("loc_deleted_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_feature_code_generation_activity_count",
            labels,
            item.get("code_generation_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_feature_code_acceptance_activity_count",
            labels,
            item.get("code_acceptance_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_feature_user_initiated_interaction_count",
            labels,
            item.get("user_initiated_interaction_count"),
            ts_ms,
        )

    for item in row.get("totals_by_ide") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "ide": str(item.get("ide", "unknown")),
        }
        append_point(
            lines,
            "github_copilot_ide_loc_added_sum",
            labels,
            item.get("loc_added_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ide_loc_deleted_sum",
            labels,
            item.get("loc_deleted_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ide_code_generation_activity_count",
            labels,
            item.get("code_generation_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ide_code_acceptance_activity_count",
            labels,
            item.get("code_acceptance_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ide_user_initiated_interaction_count",
            labels,
            item.get("user_initiated_interaction_count"),
            ts_ms,
        )

    for item in row.get("totals_by_language_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "language": str(item.get("language", "unknown")),
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(
            lines,
            "github_copilot_language_feature_loc_added_sum",
            labels,
            item.get("loc_added_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_language_feature_code_generation_activity_count",
            labels,
            item.get("code_generation_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_language_feature_code_acceptance_activity_count",
            labels,
            item.get("code_acceptance_activity_count"),
            ts_ms,
        )

    for item in row.get("totals_by_ai_adoption_phase") or []:
        phase = item.get("ai_adoption_phase")
        if isinstance(phase, dict):
            phase_name = str(phase.get("name", "unknown"))
            phase_version = str(phase.get("version", "unknown"))
        else:
            phase_name = str(phase or "unknown")
            phase_version = "unknown"

        labels = {
            "enterprise": GH_ENTERPRISE,
            "ai_adoption_phase": phase_name,
            "ai_adoption_phase_version": phase_version,
        }
        append_point(
            lines,
            "github_copilot_ai_adoption_phase_user_count",
            labels,
            item.get("user_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ai_adoption_phase_code_generation_activity_count",
            labels,
            item.get("code_generation_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ai_adoption_phase_code_acceptance_activity_count",
            labels,
            item.get("code_acceptance_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ai_adoption_phase_user_initiated_interaction_count",
            labels,
            item.get("user_initiated_interaction_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ai_adoption_phase_loc_added_sum",
            labels,
            item.get("loc_added_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_ai_adoption_phase_loc_deleted_sum",
            labels,
            item.get("loc_deleted_sum"),
            ts_ms,
        )

    cli = row.get("totals_by_cli") or {}
    cli_token = cli.get("token_usage") or {}
    append_point(
        lines,
        "github_copilot_cli_session_count",
        base,
        cli.get("session_count"),
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_cli_request_count",
        base,
        cli.get("request_count"),
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_cli_prompt_count",
        base,
        cli.get("prompt_count"),
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_cli_output_tokens_sum",
        base,
        cli_token.get("output_tokens_sum"),
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_cli_prompt_tokens_sum",
        base,
        cli_token.get("prompt_tokens_sum"),
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_cli_avg_tokens_per_request",
        base,
        cli_token.get("avg_tokens_per_request"),
        ts_ms,
    )
    # NEW: avg_total_tokens_per_session — present in enterprise NDJSON,
    # was not emitted in previous version.
    append_point(
        lines,
        "github_copilot_cli_avg_total_tokens_per_session",
        base,
        cli_token.get("avg_total_tokens_per_session"),
        ts_ms,
    )

    return lines


# -----------------------------------------------------------------------------
# User usage metric builders
# CHANGES vs original:
#   1. Added github_copilot_user_ai_credits_used from row.get("ai_credits_used")
#   2. Added user-level CLI token fields from totals_by_cli.token_usage
# -----------------------------------------------------------------------------
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
        "github_copilot_user_prompt_count": row.get(
            "user_initiated_interaction_count"
        ),
        "github_copilot_user_code_generation_activity_count": row.get(
            "code_generation_activity_count"
        ),
        "github_copilot_user_code_acceptance_activity_count": row.get(
            "code_acceptance_activity_count"
        ),
        "github_copilot_user_loc_suggested_to_add_sum": row.get(
            "loc_suggested_to_add_sum"
        ),
        "github_copilot_user_loc_suggested_to_delete_sum": row.get(
            "loc_suggested_to_delete_sum"
        ),
        "github_copilot_user_loc_added_sum": row.get("loc_added_sum"),
        "github_copilot_user_loc_deleted_sum": row.get("loc_deleted_sum"),
        "github_copilot_user_used_chat": row.get("used_chat"),
        "github_copilot_user_used_agent": row.get("used_agent"),
        "github_copilot_user_used_cli": row.get("used_cli"),
        "github_copilot_user_chat_panel_agent_mode_count": row.get(
            "chat_panel_agent_mode"
        ),
        "github_copilot_user_chat_panel_ask_mode_count": row.get(
            "chat_panel_ask_mode"
        ),
        "github_copilot_user_chat_panel_custom_mode_count": row.get(
            "chat_panel_custom_mode"
        ),
        "github_copilot_user_chat_panel_edit_mode_count": row.get(
            "chat_panel_edit_mode"
        ),
        "github_copilot_user_chat_panel_unknown_mode_count": row.get(
            "chat_panel_unknown_mode"
        ),
    }
    for metric_name, value in root_fields.items():
        append_point(lines, metric_name, base, value, ts_ms)

    # CHANGE 1 — ai_credits_used
    # Added June 19 2026. Overall per-user daily total across all surfaces.
    # 1 AI Credit = $0.01 USD.
    ai_credits = row.get("ai_credits_used")
    if ai_credits is not None:
        append_point(
            lines,
            "github_copilot_user_ai_credits_used",
            base,
            ai_credits,
            ts_ms,
        )

    # Adoption phase
    phase = row.get("ai_adoption_phase")
    if isinstance(phase, dict):
        phase_name = str(phase.get("name", "unknown"))
        phase_version = str(phase.get("version", "unknown"))
    elif phase:
        phase_name = str(phase)
        phase_version = "unknown"
    else:
        phase_name = None
        phase_version = None

    if phase_name:
        phase_labels = {
            **base,
            "ai_adoption_phase": phase_name,
            "ai_adoption_phase_version": phase_version or "unknown",
        }
        append_point(
            lines, "github_copilot_user_ai_adoption_phase", phase_labels, 1, ts_ms
        )

    # IDE breakdown
    for item in row.get("totals_by_ide") or []:
        ide = str(item.get("ide", "unknown"))
        ide_version = ""
        plugin_version = ""

        last_known_ide_version = item.get("last_known_ide_version") or {}
        if isinstance(last_known_ide_version, dict):
            ide_version = str(last_known_ide_version.get("ide_version", ""))

        last_known_plugin_version = (
            item.get("last_known_plugin_version") or {}
        )
        if isinstance(last_known_plugin_version, dict):
            plugin_version = str(
                last_known_plugin_version.get("plugin_version", "")
            )

        ide_labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "ide": ide,
            "ide_version": ide_version,
            "plugin_version": plugin_version,
        }
        append_point(
            lines,
            "github_copilot_user_ide_daily_record",
            ide_labels,
            1,
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_ide_prompt_count",
            ide_labels,
            item.get("user_initiated_interaction_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_ide_code_generation_activity_count",
            ide_labels,
            item.get("code_generation_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_ide_code_acceptance_activity_count",
            ide_labels,
            item.get("code_acceptance_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_ide_loc_added_sum",
            ide_labels,
            item.get("loc_added_sum"),
            ts_ms,
        )

    # CLI breakdown
    totals_by_cli = row.get("totals_by_cli") or {}
    last_known_cli_version = totals_by_cli.get("last_known_cli_version") or {}
    cli_version = ""
    if isinstance(last_known_cli_version, dict):
        cli_version = str(last_known_cli_version.get("cli_version", ""))

    if totals_by_cli:
        cli_labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "cli_version": cli_version,
        }
        append_point(
            lines,
            "github_copilot_user_cli_daily_record",
            cli_labels,
            1,
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_cli_prompt_count",
            cli_labels,
            totals_by_cli.get("prompt_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_cli_request_count",
            cli_labels,
            totals_by_cli.get("request_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_cli_session_count",
            cli_labels,
            totals_by_cli.get("session_count"),
            ts_ms,
        )

        # CHANGE 2 — user-level CLI token fields
        # Added to enterprise API April 2026 (changelog 2026-04-02).
        # Field names confirmed: output_tokens_sum, prompt_tokens_sum,
        # avg_tokens_per_request inside totals_by_cli.token_usage.
        cli_token = totals_by_cli.get("token_usage") or {}
        append_point(
            lines,
            "github_copilot_user_cli_output_tokens_sum",
            cli_labels,
            cli_token.get("output_tokens_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_cli_prompt_tokens_sum",
            cli_labels,
            cli_token.get("prompt_tokens_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_cli_avg_tokens_per_request",
            cli_labels,
            cli_token.get("avg_tokens_per_request"),
            ts_ms,
        )

    # Feature breakdown
    for item in row.get("totals_by_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(
            lines,
            "github_copilot_user_feature_prompt_count",
            labels,
            item.get("user_initiated_interaction_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_feature_code_generation_activity_count",
            labels,
            item.get("code_generation_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_feature_code_acceptance_activity_count",
            labels,
            item.get("code_acceptance_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_feature_loc_added_sum",
            labels,
            item.get("loc_added_sum"),
            ts_ms,
        )

    # Language + feature breakdown
    for item in row.get("totals_by_language_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "language": str(item.get("language", "unknown")),
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(
            lines,
            "github_copilot_user_language_feature_loc_added_sum",
            labels,
            item.get("loc_added_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_language_feature_prompt_count",
            labels,
            item.get("user_initiated_interaction_count"),
            ts_ms,
        )

    # Model + feature breakdown
    for item in row.get("totals_by_model_feature") or []:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "model": str(item.get("model", "unknown")),
            "feature": str(item.get("feature", "unknown")),
        }
        append_point(
            lines,
            "github_copilot_user_model_feature_prompt_count",
            labels,
            item.get("user_initiated_interaction_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_model_feature_loc_added_sum",
            labels,
            item.get("loc_added_sum"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_model_feature_code_generation_activity_count",
            labels,
            item.get("code_generation_activity_count"),
            ts_ms,
        )
        append_point(
            lines,
            "github_copilot_user_model_feature_code_acceptance_activity_count",
            labels,
            item.get("code_acceptance_activity_count"),
            ts_ms,
        )

    return lines


# -----------------------------------------------------------------------------
# User-team metric builders (unchanged)
# -----------------------------------------------------------------------------
def build_user_teams_series(rows: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    team_counts: Dict[Tuple[str, str], int] = {}

    for row in rows:
        day = row.get("day")
        if not day:
            continue
        ts_ms = day_to_ms(day)

        team_id = str(row.get("team_id", "unknown"))
        team_slug = str(row.get("slug", "unknown"))
        user_id = str(row.get("user_id", "unknown"))
        user_login = str(row.get("user_login", "unknown"))

        labels = {
            "enterprise": GH_ENTERPRISE,
            "team_id": team_id,
            "team_slug": team_slug,
            "user_id": user_id,
            "user_login": user_login,
        }
        append_point(
            lines, "github_copilot_user_team_membership", labels, 1, ts_ms
        )
        team_counts[(team_id, team_slug)] = (
            team_counts.get((team_id, team_slug), 0) + 1
        )

    if rows:
        ts_ms = day_to_ms(rows[0]["day"])
        for (team_id, team_slug), count in team_counts.items():
            labels = {
                "enterprise": GH_ENTERPRISE,
                "team_id": team_id,
                "team_slug": team_slug,
            }
            append_point(
                lines,
                "github_copilot_team_member_count",
                labels,
                count,
                ts_ms,
            )

    return lines


# -----------------------------------------------------------------------------
# Enterprise seat metric builders (unchanged)
# -----------------------------------------------------------------------------
def build_enterprise_seat_series(
    seat_payload: Dict[str, Any],
) -> List[str]:
    lines: List[str] = []
    ts_ms = now_ms()
    labels = {"enterprise": GH_ENTERPRISE}

    append_point(
        lines,
        "github_copilot_enterprise_seat_total",
        labels,
        seat_payload.get("total_seats"),
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_enterprise_seat_rows_returned",
        labels,
        seat_payload.get("seat_rows_returned"),
        ts_ms,
    )

    seats = seat_payload.get("seats") or []
    plan_counts: Dict[str, int] = {}
    team_counts: Dict[str, int] = {}
    active_7d = active_28d = active_90d = never_active = pending_cancel = 0

    for seat in seats:
        assignee = seat.get("assignee") or {}
        user_login = str(assignee.get("login", "unknown"))
        user_id = str(assignee.get("id", "unknown"))
        org_login = str(
            seat.get(
                "organization_login",
                assignee.get("organization_login", "unknown"),
            )
        )
        plan_type = str(seat.get("plan_type", "unknown"))
        team_name = str(seat.get("assigning_team", "") or "none")
        team_counts[team_name] = team_counts.get(team_name, 0) + 1
        plan_counts[plan_type] = plan_counts.get(plan_type, 0) + 1

        last_activity_at = seat.get("last_activity_at") or ""
        last_authenticated_at = seat.get("last_authenticated_at") or ""
        created_at = seat.get("created_at") or ""
        updated_at = seat.get("updated_at") or ""
        pending_cancellation_date = seat.get("pending_cancellation_date") or ""
        last_activity_editor = str(
            seat.get("last_activity_editor", "unknown")
        )

        seat_labels = {
            "enterprise": GH_ENTERPRISE,
            "user_login": user_login,
            "user_id": user_id,
            "organization_login": org_login,
            "plan_type": plan_type,
            "assigning_team": team_name,
            "last_activity_editor": last_activity_editor,
        }

        append_point(
            lines,
            "github_copilot_enterprise_seat_assigned",
            seat_labels,
            1,
            ts_ms,
        )

        if pending_cancellation_date:
            pending_cancel += 1
            append_point(
                lines,
                "github_copilot_enterprise_seat_pending_cancellation",
                seat_labels,
                1,
                ts_ms,
            )
        else:
            append_point(
                lines,
                "github_copilot_enterprise_seat_pending_cancellation",
                seat_labels,
                0,
                ts_ms,
            )

        last_activity_ms = parse_any_time_to_ms(last_activity_at)
        if last_activity_ms:
            append_point(
                lines,
                "github_copilot_enterprise_seat_last_activity_timestamp_seconds",
                seat_labels,
                last_activity_ms / 1000.0,
                ts_ms,
            )
            age_days = (
                time.time() - (last_activity_ms / 1000.0)
            ) / 86400.0
            if age_days <= 7:
                active_7d += 1
            if age_days <= 28:
                active_28d += 1
            if age_days <= 90:
                active_90d += 1
        else:
            never_active += 1

        last_auth_ms = parse_any_time_to_ms(last_authenticated_at)
        if last_auth_ms:
            append_point(
                lines,
                "github_copilot_enterprise_seat_last_authenticated_timestamp_seconds",
                seat_labels,
                last_auth_ms / 1000.0,
                ts_ms,
            )

        created_ms = parse_any_time_to_ms(created_at)
        if created_ms:
            append_point(
                lines,
                "github_copilot_enterprise_seat_created_timestamp_seconds",
                seat_labels,
                created_ms / 1000.0,
                ts_ms,
            )

        updated_ms = parse_any_time_to_ms(updated_at)
        if updated_ms:
            append_point(
                lines,
                "github_copilot_enterprise_seat_updated_timestamp_seconds",
                seat_labels,
                updated_ms / 1000.0,
                ts_ms,
            )

        pending_ms = parse_any_time_to_ms(pending_cancellation_date)
        if pending_ms:
            append_point(
                lines,
                "github_copilot_enterprise_seat_pending_cancellation_timestamp_seconds",
                seat_labels,
                pending_ms / 1000.0,
                ts_ms,
            )

    append_point(
        lines,
        "github_copilot_enterprise_seat_pending_cancellation_total",
        labels,
        pending_cancel,
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_enterprise_seat_active_last_7d_total",
        labels,
        active_7d,
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_enterprise_seat_active_last_28d_total",
        labels,
        active_28d,
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_enterprise_seat_active_last_90d_total",
        labels,
        active_90d,
        ts_ms,
    )
    append_point(
        lines,
        "github_copilot_enterprise_seat_never_active_total",
        labels,
        never_active,
        ts_ms,
    )

    for plan_type, count in plan_counts.items():
        append_point(
            lines,
            "github_copilot_enterprise_seat_plan_total",
            {"enterprise": GH_ENTERPRISE, "plan_type": plan_type},
            count,
            ts_ms,
        )

    for team_name, count in team_counts.items():
        append_point(
            lines,
            "github_copilot_enterprise_seat_assigning_team_total",
            {"enterprise": GH_ENTERPRISE, "assigning_team": team_name},
            count,
            ts_ms,
        )

    return lines


# -----------------------------------------------------------------------------
# Billing metric builders (unchanged)
# -----------------------------------------------------------------------------
def build_billing_usage_series(
    org: str,
    day: str,
    report: Dict[str, Any],
    metric_kind: str,
) -> List[str]:
    lines: List[str] = []
    ts_ms = day_to_ms(day)

    usage_items = report.get("usageItems") or []
    totals: Dict[str, float] = {
        "gross_quantity": 0.0,
        "gross_amount": 0.0,
        "discount_quantity": 0.0,
        "discount_amount": 0.0,
        "net_quantity": 0.0,
        "net_amount": 0.0,
    }

    for item in usage_items:
        labels = {
            "enterprise": GH_ENTERPRISE,
            "org": org,
            "billing_metric": metric_kind,
            "product": str(item.get("product", "unknown")),
            "sku": str(item.get("sku", "unknown")),
            "model": str(item.get("model", "unknown")),
            "unit_type": str(item.get("unitType", "unknown")),
        }
        fields = {
            "gross_quantity": item.get("grossQuantity"),
            "gross_amount_usd": item.get("grossAmount"),
            "discount_quantity": item.get("discountQuantity"),
            "discount_amount_usd": item.get("discountAmount"),
            "net_quantity": item.get("netQuantity"),
            "net_amount_usd": item.get("netAmount"),
            "price_per_unit_usd": item.get("pricePerUnit"),
        }
        for field_name, value in fields.items():
            append_point(
                lines,
                f"github_copilot_billing_{metric_kind}_{field_name}",
                labels,
                value,
                ts_ms,
            )

        totals["gross_quantity"] += float(item.get("grossQuantity") or 0)
        totals["gross_amount"] += float(item.get("grossAmount") or 0)
        totals["discount_quantity"] += float(
            item.get("discountQuantity") or 0
        )
        totals["discount_amount"] += float(item.get("discountAmount") or 0)
        totals["net_quantity"] += float(item.get("netQuantity") or 0)
        totals["net_amount"] += float(item.get("netAmount") or 0)

    aggregate_labels = {
        "enterprise": GH_ENTERPRISE,
        "org": org,
        "billing_metric": metric_kind,
    }
    append_point(
        lines,
        f"github_copilot_billing_{metric_kind}_total_gross_quantity",
        aggregate_labels,
        totals["gross_quantity"],
        ts_ms,
    )
    append_point(
        lines,
        f"github_copilot_billing_{metric_kind}_total_gross_amount_usd",
        aggregate_labels,
        totals["gross_amount"],
        ts_ms,
    )
    append_point(
        lines,
        f"github_copilot_billing_{metric_kind}_total_discount_quantity",
        aggregate_labels,
        totals["discount_quantity"],
        ts_ms,
    )
    append_point(
        lines,
        f"github_copilot_billing_{metric_kind}_total_discount_amount_usd",
        aggregate_labels,
        totals["discount_amount"],
        ts_ms,
    )
    append_point(
        lines,
        f"github_copilot_billing_{metric_kind}_total_net_quantity",
        aggregate_labels,
        totals["net_quantity"],
        ts_ms,
    )
    append_point(
        lines,
        f"github_copilot_billing_{metric_kind}_total_net_amount_usd",
        aggregate_labels,
        totals["net_amount"],
        ts_ms,
    )

    return lines


def build_billing_day_marker(day: str) -> List[str]:
    ts_ms = day_to_ms(day)
    return [
        vm_json_line(
            "github_copilot_billing_day_marker",
            {"enterprise": GH_ENTERPRISE},
            1.0,
            ts_ms,
        )
    ]


# -----------------------------------------------------------------------------
# VictoriaMetrics writer (unchanged)
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Daily import orchestration
# CHANGE: push_user_day_to_loki() called after VM write succeeds.
# -----------------------------------------------------------------------------
def import_enterprise_day(day: str) -> int:
    usage_exists = enterprise_day_already_present(
        day
    ) and user_day_already_present(day)
    teams_exists = user_teams_day_already_present(day)
    billing_exists = billing_day_already_present(day)

    total_count = 0
    user_rows_for_loki: List[Dict[str, Any]] = []

    if not usage_exists:
        enterprise_chunks = fetch_enterprise_usage_for_day(day)
        enterprise_rows = extract_rows(enterprise_chunks)

        user_chunks = fetch_enterprise_users_usage_for_day(day)
        user_rows = extract_rows(user_chunks)

        lines: List[str] = []
        for row in enterprise_rows:
            lines.extend(build_enterprise_usage_series(row))
        for row in user_rows:
            lines.extend(build_user_usage_series(row))
            user_rows_for_loki.append(row)

        count = import_to_victoriametrics(lines)
        IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)
        total_count += count
        logging.info(
            "Imported enterprise usage day=%s imported_points=%s", day, count
        )

        # Push to Loki — only runs if VM write succeeded and loki_enabled
        if user_rows_for_loki:
            push_user_day_to_loki(user_rows_for_loki, day)
    else:
        logging.info(
            "Skipping enterprise usage day=%s because data already exists", day
        )

    if not teams_exists:
        team_rows = extract_rows(fetch_enterprise_user_teams_for_day(day))
        team_lines = build_user_teams_series(team_rows)
        if team_lines:
            count = import_to_victoriametrics(team_lines)
            IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)
            total_count += count
            logging.info(
                "Imported enterprise user-teams day=%s imported_points=%s",
                day,
                count,
            )
    else:
        logging.info(
            "Skipping enterprise user-teams day=%s because data already exists",
            day,
        )

    if ENABLE_BILLING_REPORTS and GH_BILLING_ORGS and not billing_exists:
        billing_lines: List[str] = []
        for org in GH_BILLING_ORGS:
            try:
                premium_report = fetch_org_billing_premium_request_usage(
                    org, day
                )
                billing_lines.extend(
                    build_billing_usage_series(
                        org, day, premium_report, "premium_request"
                    )
                )
            except requests.HTTPError as exc:
                logging.warning(
                    "Premium request billing failed org=%s day=%s status=%s",
                    org,
                    day,
                    getattr(exc.response, "status_code", "unknown"),
                )

            try:
                ai_credit_report = fetch_org_billing_ai_credit_usage(
                    org, day
                )
                billing_lines.extend(
                    build_billing_usage_series(
                        org, day, ai_credit_report, "ai_credit"
                    )
                )
            except requests.HTTPError as exc:
                logging.warning(
                    "AI credit billing failed org=%s day=%s status=%s",
                    org,
                    day,
                    getattr(exc.response, "status_code", "unknown"),
                )

        billing_lines.extend(build_billing_day_marker(day))
        if billing_lines:
            count = import_to_victoriametrics(billing_lines)
            IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)
            total_count += count
            logging.info(
                "Imported billing usage day=%s imported_points=%s org_count=%s",
                day,
                count,
                len(GH_BILLING_ORGS),
            )
    elif ENABLE_BILLING_REPORTS and GH_BILLING_ORGS:
        logging.info(
            "Skipping billing day=%s because data already exists", day
        )

    logging.info(
        "Imported enterprise day=%s total_imported_points=%s",
        day,
        total_count,
    )
    return total_count


# -----------------------------------------------------------------------------
# Backfill logic (unchanged)
# -----------------------------------------------------------------------------
def backfill_date_range_once():
    global date_range_backfill_done

    if not ENABLE_DATE_RANGE_BACKFILL or date_range_backfill_done:
        return

    if not BACKFILL_START_DAY or not BACKFILL_END_DAY:
        raise RuntimeError(
            "ENABLE_DATE_RANGE_BACKFILL=true but BACKFILL_START_DAY or "
            "BACKFILL_END_DAY is missing"
        )

    start_date = datetime.strptime(BACKFILL_START_DAY, "%Y-%m-%d").date()
    end_date = datetime.strptime(BACKFILL_END_DAY, "%Y-%m-%d").date()

    if end_date < start_date:
        raise RuntimeError(
            "BACKFILL_END_DAY must be >= BACKFILL_START_DAY"
        )

    logging.info(
        "Starting date-range backfill enterprise=%s start=%s end=%s",
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
        "Completed date-range backfill enterprise=%s start=%s end=%s "
        "imported_points=%s",
        GH_ENTERPRISE,
        BACKFILL_START_DAY,
        BACKFILL_END_DAY,
        total_points,
    )


# -----------------------------------------------------------------------------
# 28-day bootstrap (unchanged)
# -----------------------------------------------------------------------------
def bootstrap_28d_once():
    global bootstrapped, last_daily_import_day

    if not BOOTSTRAP_28D or bootstrapped:
        return

    if not FORCE_BOOTSTRAP and enterprise_bootstrap_already_present():
        logging.info(
            "Skipping 28-day bootstrap enterprise=%s because data already exists",
            GH_ENTERPRISE,
        )
        bootstrapped = True
        return

    logging.info(
        "Starting 28-day bootstrap enterprise=%s", GH_ENTERPRISE
    )

    enterprise_chunks = fetch_enterprise_usage_28d()
    enterprise_rows = extract_rows(enterprise_chunks)

    user_chunks = fetch_enterprise_users_usage_28d()
    user_rows = extract_rows(user_chunks)

    lines: List[str] = []
    latest_day = None

    for row in enterprise_rows:
        if row.get("day"):
            latest_day = (
                max(latest_day, row["day"]) if latest_day else row["day"]
            )
        lines.extend(build_enterprise_usage_series(row))

    # Group user rows by day so we can call push_user_day_to_loki per day
    user_rows_by_day: Dict[str, List[Dict[str, Any]]] = {}
    for row in user_rows:
        if row.get("day"):
            latest_day = (
                max(latest_day, row["day"]) if latest_day else row["day"]
            )
            user_rows_by_day.setdefault(row["day"], []).append(row)
        lines.extend(build_user_usage_series(row))

    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)

    # Push each bootstrap day to Loki
    for day, rows in sorted(user_rows_by_day.items()):
        push_user_day_to_loki(rows, day)

    bootstrapped = True
    if latest_day:
        last_daily_import_day = latest_day

    logging.info(
        "Completed 28-day bootstrap enterprise=%s imported_points=%s "
        "latest_day=%s",
        GH_ENTERPRISE,
        count,
        latest_day,
    )


# -----------------------------------------------------------------------------
# Stable day import (unchanged)
# -----------------------------------------------------------------------------
def import_latest_stable_day_if_needed():
    global last_daily_import_day

    target_day = (
        datetime.now(timezone.utc).date() - timedelta(days=DATA_LAG_DAYS)
    ).isoformat()

    if last_daily_import_day == target_day:
        logging.info(
            "Stable day %s already imported in this pod lifecycle", target_day
        )
        return

    count = import_enterprise_day(target_day)
    last_daily_import_day = target_day

    logging.info(
        "Completed stable day import enterprise=%s day=%s imported_points=%s",
        GH_ENTERPRISE,
        target_day,
        count,
    )


# -----------------------------------------------------------------------------
# Seat snapshot import (unchanged)
# Runs every cycle (every 6h by default) — seat state changes intraday.
# -----------------------------------------------------------------------------
def import_enterprise_seat_snapshot():
    seat_payload = fetch_enterprise_seats()
    lines = build_enterprise_seat_series(seat_payload)
    count = import_to_victoriametrics(lines)
    IMPORTED_POINTS.labels(enterprise=GH_ENTERPRISE).inc(count)

    logging.info(
        "Imported seat snapshot enterprise=%s imported_points=%s "
        "total_seats=%s seat_rows_returned=%s",
        GH_ENTERPRISE,
        count,
        seat_payload.get("total_seats"),
        seat_payload.get("seat_rows_returned"),
    )


# -----------------------------------------------------------------------------
# Main collector cycle
# -----------------------------------------------------------------------------
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
        logging.exception(
            "Collector cycle failed enterprise=%s", GH_ENTERPRISE
        )
        raise
    finally:
        LAST_DURATION.labels(enterprise=GH_ENTERPRISE).set(
            time.time() - start
        )


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
def main():
    logging.info(
        "Starting GitHub Copilot enterprise collector port=%s enterprise=%s "
        "billing_orgs=%s poll_interval_seconds=%s",
        EXPORTER_PORT,
        GH_ENTERPRISE,
        ",".join(GH_BILLING_ORGS) if GH_BILLING_ORGS else "none",
        POLL_INTERVAL_SECONDS,
    )

    # Loki health check — sets loki_enabled, does not block startup
    check_loki_health()

    start_http_server(EXPORTER_PORT)

    while True:
        try:
            run_cycle()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()