import argparse
import ast
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests
from prometheus_client import Counter, Gauge, start_http_server

# -----------------------------------------------------------------------------
# Logging / process metadata
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

SCHEMA_VERSION = "2026-06-exact-diff-v1"
DEFAULT_GH_API_BASE = "https://api.github.com"
DEFAULT_GH_API_VERSION = "2026-03-10"
DEFAULT_HTTP_TIMEOUT = 60
SIGNED_URL_RETRY_DELAYS_SECONDS = (1, 3, 8)


# -----------------------------------------------------------------------------
# Environment-backed runtime configuration
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    gh_token: str
    gh_enterprise: str
    gh_api_base: str
    gh_api_version: str

    gh_billing_orgs: Tuple[str, ...]
    gh_cost_center_ids: Tuple[str, ...]
    enable_billing_reports: bool
    enable_billing_usage_summary: bool
    billing_scope: str

    data_lag_days: int
    poll_interval_seconds: int

    bootstrap_28d: bool
    force_bootstrap: bool

    enable_date_range_backfill: bool
    backfill_start_day: str
    backfill_end_day: str

    export_port: int

    vm_import_url: str
    vm_export_url: str
    vm_series_url: str
    vm_delete_url: str

    vm_username: str
    vm_password: str
    vm_bearer_token: str

    exact_diff_enabled: bool
    drift_policy: str
    dry_run: bool

    @staticmethod
    def from_env() -> "Settings":
        gh_billing_orgs = tuple(x.strip() for x in os.getenv("GH_BILLING_ORGS", "").split(",") if x.strip())
        gh_cost_center_ids = tuple(x.strip() for x in os.getenv("GH_COST_CENTER_IDS", "").split(",") if x.strip())
        return Settings(
            gh_token=os.environ["GH_TOKEN"],
            gh_enterprise=os.environ["GH_ENTERPRISE"],
            gh_api_base=os.getenv("GH_API_BASE", DEFAULT_GH_API_BASE).rstrip("/"),
            gh_api_version=os.getenv("GH_API_VERSION", DEFAULT_GH_API_VERSION),
            gh_billing_orgs=gh_billing_orgs,
            gh_cost_center_ids=gh_cost_center_ids,
            enable_billing_reports=os.getenv("ENABLE_BILLING_REPORTS", "false").lower() == "true",
            enable_billing_usage_summary=os.getenv("ENABLE_BILLING_USAGE_SUMMARY", "true").lower() == "true",
            billing_scope=os.getenv("BILLING_SCOPE", "enterprise").strip().lower(),
            data_lag_days=int(os.getenv("DATA_LAG_DAYS", "2")),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "21600")),
            bootstrap_28d=os.getenv("BOOTSTRAP_28D", "false").lower() == "true",
            force_bootstrap=os.getenv("FORCE_BOOTSTRAP", "false").lower() == "true",
            enable_date_range_backfill=os.getenv("ENABLE_DATE_RANGE_BACKFILL", "false").lower() == "true",
            backfill_start_day=os.getenv("BACKFILL_START_DAY", ""),
            backfill_end_day=os.getenv("BACKFILL_END_DAY", ""),
            export_port=int(os.getenv("EXPORTER_PORT", "8080")),
            vm_import_url=os.environ["VM_IMPORT_URL"],
            vm_export_url=os.getenv("VM_EXPORT_URL", "").rstrip("/"),
            vm_series_url=os.getenv("VM_SERIES_URL", "").rstrip("/"),
            vm_delete_url=os.getenv("VM_DELETE_URL", "").rstrip("/"),
            vm_username=os.getenv("VM_USERNAME", ""),
            vm_password=os.getenv("VM_PASSWORD", ""),
            vm_bearer_token=os.getenv("VM_BEARER_TOKEN", ""),
            exact_diff_enabled=os.getenv("EXACT_DIFF_ENABLED", "true").lower() == "true",
            drift_policy=os.getenv("DRIFT_POLICY", "skip").strip().lower(),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        )


SETTINGS = Settings.from_env()


# -----------------------------------------------------------------------------
# Exporter self-metrics
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
    ["enterprise", "family"],
)

SKIPPED_POINTS = Counter(
    "github_copilot_exporter_skipped_points_total",
    "How many points were skipped because they already existed in VictoriaMetrics",
    ["enterprise", "family"],
)

DRIFT_POINTS = Counter(
    "github_copilot_exporter_drift_points_total",
    "How many records matched by key but had a different stored value",
    ["enterprise", "family"],
)

ERRORS = Counter(
    "github_copilot_exporter_errors_total",
    "How many collector cycles failed",
    ["enterprise"],
)

FAILED_BACKFILL_DAYS = Counter(
    "github_copilot_exporter_failed_backfill_days_total",
    "How many backfill days failed and were skipped",
    ["enterprise"],
)

LAST_MISSING_POINTS = Gauge(
    "github_copilot_exporter_last_missing_points",
    "How many points were missing and therefore eligible for import in the last pass",
    ["enterprise", "family"],
)

LAST_EXPECTED_POINTS = Gauge(
    "github_copilot_exporter_last_expected_points",
    "How many points were generated from source data in the last pass",
    ["enterprise", "family"],
)

TEAM_JOIN_UNMATCHED_USERS = Gauge(
    "github_copilot_exporter_team_join_unmatched_users",
    "Members present in the user-teams report for a day but missing from the users report for the same day (likely partial backfill)",
    ["enterprise", "day"],
)


# -----------------------------------------------------------------------------
# Small value object for normalized metrics
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricPoint:
    metric: str
    labels: Dict[str, str]
    timestamp_ms: int
    value: float

    def canonical_labels(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted((str(k), str(v)) for k, v in self.labels.items()))

    def record_key(self) -> str:
        """
        Deterministic key used for exact-diff deduplication.
        """
        payload = {
            "metric": self.metric,
            "labels": list(self.canonical_labels()),
            "timestamp_ms": self.timestamp_ms,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def series_key(self) -> str:
        """
        Deterministic key used to compare the same series+timestamp even if the value differs.
        """
        return self.record_key()

    def vm_json_line(self) -> str:
        obj = {
            "metric": {"__name__": self.metric, **{k: v for k, v in self.canonical_labels()}},
            "values": [self.value],
            "timestamps": [self.timestamp_ms],
        }
        return json.dumps(obj, separators=(",", ":"))


def coerce_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_point(metric: str, labels: Dict[str, Any], value: Any, timestamp_ms: int) -> Optional[MetricPoint]:
    num = coerce_number(value)
    if num is None:
        return None
    return MetricPoint(
        metric=metric,
        labels={str(k): str(v) for k, v in labels.items()},
        timestamp_ms=timestamp_ms,
        value=num,
    )


def day_to_midday_ms(day_str: str) -> int:
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def snapshot_day_ms(target_date: date) -> int:
    dt = datetime(target_date.year, target_date.month, target_date.day, 12, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def collection_time_ms(collected_at: Optional[datetime] = None) -> int:
    """
    Mutable-state timestamp for seat snapshots.

    Unlike daily usage and billing reports, seat assignments and activity windows
    can legitimately change during the same day. Using the real collection time
    avoids same-key collisions and drift warnings when the exporter re-runs later
    on the same day.
    """
    dt = collected_at or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def day_bounds(day_str: str) -> Tuple[str, str]:
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def parse_json_or_ndjson(text: str) -> Any:
    text = text.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def normalize_scalar_label_value(value: Any) -> str:
    """
    Normalize label-like values coming back from GitHub reports.

    Protects against malformed stringified containers such as:
      "{'power_user'}"
      "['power_user']"
      '{"ai_adoption_phase":"power_user"}'
    """
    if value is None:
        return "unknown"

    if isinstance(value, (int, float, bool)):
        return str(value).strip() or "unknown"

    if isinstance(value, dict):
        for key in ("ai_adoption_phase", "phase", "value", "name", "slug"):
            if key in value and value[key] is not None:
                return normalize_scalar_label_value(value[key])
        if len(value) == 1:
            _, only_value = next(iter(value.items()))
            return normalize_scalar_label_value(only_value)
        return str(value).strip() or "unknown"

    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        if len(seq) == 1:
            return normalize_scalar_label_value(seq[0])
        return ",".join(normalize_scalar_label_value(x) for x in seq) or "unknown"

    s = str(value).strip()
    if not s:
        return "unknown"

    if s[0] in "{[(" and s[-1] in "}])":
        try:
            parsed = ast.literal_eval(s)
            return normalize_scalar_label_value(parsed)
        except Exception:
            pass
        try:
            parsed = json.loads(s)
            return normalize_scalar_label_value(parsed)
        except Exception:
            pass

    return s


# -----------------------------------------------------------------------------
# GitHub API access
# -----------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = requests.Session()
        self.api.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {settings.gh_token}",
                "X-GitHub-Api-Version": settings.gh_api_version,
            }
        )
        # Signed downloads should not reuse API auth/session assumptions.
        self.download = requests.Session()

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self.api.get(url, params=params, timeout=DEFAULT_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()


    def download_chunks(
        self,
        download_links: Sequence[str],
        refresh_links_fn: Optional[Any] = None,
        report_name: str = "",
    ) -> List[Any]:
        """
        Download signed GitHub report chunks with refresh-and-retry behavior.

        If GitHub returns 403 for a signed download URL, request fresh metadata
        and retry up to the configured backoff attempts before failing.
        """
        links = [str(x) for x in download_links if x]
        if not links:
            return []

        last_exc: Optional[Exception] = None
        for attempt_index, delay in enumerate((0, *SIGNED_URL_RETRY_DELAYS_SECONDS), start=1):
            chunks: List[Any] = []
            try:
                if delay:
                    time.sleep(delay)
                for link in links:
                    resp = self.download.get(link, timeout=DEFAULT_HTTP_TIMEOUT)
                    if resp.status_code == 403:
                        raise requests.HTTPError("403 signed url forbidden", response=resp)
                    resp.raise_for_status()
                    parsed = parse_json_or_ndjson(resp.text)
                    if isinstance(parsed, list):
                        chunks.extend(parsed)
                    else:
                        chunks.append(parsed)
                return chunks
            except requests.HTTPError as exc:
                last_exc = exc
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if status == 403 and refresh_links_fn is not None and attempt_index <= len(SIGNED_URL_RETRY_DELAYS_SECONDS):
                    logging.warning(
                        "Signed report download returned 403 for report=%s on attempt=%s. Refreshing metadata and retrying.",
                        report_name or "unknown",
                        attempt_index,
                    )
                    refreshed = refresh_links_fn()
                    links = [str(x) for x in refreshed if x]
                    if not links:
                        logging.warning(
                            "Metadata refresh for report=%s returned no download links.",
                            report_name or "unknown",
                        )
                        break
                    continue
                raise

        if last_exc:
            raise last_exc
        return []

    # ------------------------- Usage metadata/report helpers ------------------

    def _report_metadata(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.settings.gh_api_base}{path}"
        return self.get_json(url, params=params)

    def _download_links_from_metadata(self, meta: Any) -> List[str]:
        """
        Normalize GitHub report metadata into a list of signed download links.

        Expected shape is usually:
          {"download_links": ["https://..."]}

        But some endpoints/environments can return:
        - a bare signed URL string
        - a JSON-encoded string payload
        - a list containing strings and/or dicts

        This helper keeps the existing downloader logic intact while accepting
        those response variants safely.
        """
        if meta is None:
            return []

        if isinstance(meta, dict):
            links = meta.get("download_links", [])
            if isinstance(links, list):
                return [str(x) for x in links if x]
            if isinstance(links, str):
                return [links] if links else []
            return []

        if isinstance(meta, str):
            meta = meta.strip()
            if not meta:
                return []
            if meta.startswith("http://") or meta.startswith("https://"):
                return [meta]
            try:
                parsed = json.loads(meta)
            except Exception:
                return []
            return self._download_links_from_metadata(parsed)

        if isinstance(meta, list):
            links: List[str] = []
            for item in meta:
                links.extend(self._download_links_from_metadata(item))
            return links

        return []

    def fetch_enterprise_usage_day(self, day: str) -> List[Any]:
        path = f"/enterprises/{self.settings.gh_enterprise}/copilot/metrics/reports/enterprise-1-day"
        params = {"day": day}
        meta = self._report_metadata(path, params)
        return self.download_chunks(
            self._download_links_from_metadata(meta),
            refresh_links_fn=lambda: self._download_links_from_metadata(self._report_metadata(path, params)),
            report_name=f"enterprise-1-day:{day}",
        )

    def fetch_enterprise_usage_28d(self) -> List[Any]:
        path = f"/enterprises/{self.settings.gh_enterprise}/copilot/metrics/reports/enterprise-28-day/latest"
        meta = self._report_metadata(path)
        return self.download_chunks(
            self._download_links_from_metadata(meta),
            refresh_links_fn=lambda: self._download_links_from_metadata(self._report_metadata(path)),
            report_name="enterprise-28-day:latest",
        )

    def fetch_users_usage_day(self, day: str) -> List[Any]:
        path = f"/enterprises/{self.settings.gh_enterprise}/copilot/metrics/reports/users-1-day"
        params = {"day": day}
        meta = self._report_metadata(path, params)
        return self.download_chunks(
            self._download_links_from_metadata(meta),
            refresh_links_fn=lambda: self._download_links_from_metadata(self._report_metadata(path, params)),
            report_name=f"users-1-day:{day}",
        )

    def fetch_users_usage_28d(self) -> List[Any]:
        path = f"/enterprises/{self.settings.gh_enterprise}/copilot/metrics/reports/users-28-day/latest"
        meta = self._report_metadata(path)
        return self.download_chunks(
            self._download_links_from_metadata(meta),
            refresh_links_fn=lambda: self._download_links_from_metadata(self._report_metadata(path)),
            report_name="users-28-day:latest",
        )

    def fetch_user_teams_day(self, day: str) -> List[Any]:
        path = f"/enterprises/{self.settings.gh_enterprise}/copilot/metrics/reports/user-teams-1-day"
        params = {"day": day}
        meta = self._report_metadata(path, params)
        return self.download_chunks(
            self._download_links_from_metadata(meta),
            refresh_links_fn=lambda: self._download_links_from_metadata(self._report_metadata(path, params)),
            report_name=f"user-teams-1-day:{day}",
        )

    # ------------------------------ Seats / billing ---------------------------

    def fetch_enterprise_seats(self) -> Dict[str, Any]:
        url = f"{self.settings.gh_api_base}/enterprises/{self.settings.gh_enterprise}/copilot/billing/seats"
        page = 1
        total_seats = None
        seats: List[Dict[str, Any]] = []

        while True:
            resp = self.api.get(url, params={"page": page}, timeout=DEFAULT_HTTP_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            if total_seats is None:
                total_seats = payload.get("total_seats", 0)
            page_rows = payload.get("seats", [])
            seats.extend(page_rows)
            if not page_rows or len(page_rows) < 50:
                break
            page += 1

        return {"total_seats": total_seats or 0, "seat_rows_returned": len(seats), "seats": seats}

    def _safe_billing_get(self, url: str, params: Dict[str, Any], empty_payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.api.get(url, params=params, timeout=DEFAULT_HTTP_TIMEOUT)
        if resp.status_code == 404:
            logging.warning("Billing endpoint returned 404 url=%s params=%s", url, params)
            return empty_payload
        resp.raise_for_status()
        return resp.json()

    def fetch_enterprise_billing_usage(
        self,
        kind: str,
        year: int,
        month: int,
        day: Optional[int] = None,
        organization: Optional[str] = None,
        user: Optional[str] = None,
        model: Optional[str] = None,
        product: Optional[str] = None,
        cost_center_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if kind not in {"premium_request", "ai_credit"}:
            raise ValueError(f"Unsupported billing kind: {kind}")
        url = f"{self.settings.gh_api_base}/enterprises/{self.settings.gh_enterprise}/settings/billing/{kind}/usage"
        params: Dict[str, Any] = {"year": year, "month": month}
        if day is not None:
            params["day"] = day
        if organization:
            params["organization"] = organization
        if user:
            params["user"] = user
        if model:
            params["model"] = model
        if product:
            params["product"] = product
        if cost_center_id:
            params["cost_center_id"] = cost_center_id
        return self._safe_billing_get(
            url,
            params,
            {
                "enterprise": self.settings.gh_enterprise,
                "usageItems": [],
                "timePeriod": {"year": year, "month": month, "day": day},
            },
        )

    def fetch_org_billing_usage(self, org: str, kind: str, year: int, month: int, day: Optional[int] = None) -> Dict[str, Any]:
        if kind not in {"premium_request", "ai_credit"}:
            raise ValueError(f"Unsupported billing kind: {kind}")
        url = f"{self.settings.gh_api_base}/organizations/{org}/settings/billing/{kind}/usage"
        params: Dict[str, Any] = {"year": year, "month": month}
        if day is not None:
            params["day"] = day
        return self._safe_billing_get(
            url,
            params,
            {"organization": org, "usageItems": [], "timePeriod": {"year": year, "month": month, "day": day}},
        )

    def fetch_enterprise_billing_usage_summary(
        self,
        year: int,
        month: int,
        day: Optional[int] = None,
        hour: Optional[int] = None,
        cost_center_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self.settings.gh_api_base}/enterprises/{self.settings.gh_enterprise}/settings/billing/usage/summary"
        params: Dict[str, Any] = {"year": year, "month": month}
        if day is not None:
            params["day"] = day
        if hour is not None:
            params["hour"] = hour
        if cost_center_id:
            params["cost_center_id"] = cost_center_id
        return self._safe_billing_get(
            url,
            params,
            {"enterprise": self.settings.gh_enterprise, "usageItems": [], "timePeriod": {"year": year, "month": month, "day": day}},
        )


# -----------------------------------------------------------------------------
# VictoriaMetrics read-before-write / exact diff
# -----------------------------------------------------------------------------

class VictoriaMetricsClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = requests.Session()

    def auth(self) -> Tuple[Optional[Tuple[str, str]], Dict[str, str]]:
        if self.settings.vm_bearer_token:
            return None, {"Authorization": f"Bearer {self.settings.vm_bearer_token}"}
        if self.settings.vm_username and self.settings.vm_password:
            return (self.settings.vm_username, self.settings.vm_password), {}
        return None, {}

    def import_points(self, points: Sequence[MetricPoint]) -> int:
        if not points:
            return 0
        auth, extra_headers = self.auth()
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers)
        payload = ("\n".join(p.vm_json_line() for p in points) + "\n").encode("utf-8")
        resp = self.http.post(
            self.settings.vm_import_url,
            data=payload,
            headers=headers,
            auth=auth,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return len(points)

    def export_existing_points(self, metric_names: Sequence[str], day: str, extra_match_labels: Optional[Dict[str, str]] = None) -> Dict[str, float]:
        """
        Returns existing series/timestamp keys -> value for the requested metric set and day.
        """
        if not self.settings.vm_export_url:
            raise RuntimeError("VM_EXPORT_URL must be set for exact-diff mode")
        auth, extra_headers = self.auth()
        start, end = day_bounds(day)
        data: List[Tuple[str, str]] = []
        labels = extra_match_labels or {}
        for metric in sorted(set(metric_names)):
            matcher_labels = {**labels}
            label_fragments = [f'{k}="{v}"' for k, v in sorted(matcher_labels.items())]
            if label_fragments:
                matcher = f'{metric}' + "{" + ",".join(label_fragments) + "}"
            else:
                matcher = metric
            data.append(("match[]", matcher))
        data.extend([("start", start), ("end", end)])
        resp = self.http.post(
            self.settings.vm_export_url,
            data=data,
            headers=extra_headers,
            auth=auth,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        # If export endpoint is unavailable, fail loudly; exact diff depends on it.
        resp.raise_for_status()

        existing: Dict[str, float] = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            metric = row.get("metric", {})
            values = row.get("values", [])
            timestamps = row.get("timestamps", [])
            metric_name = metric.get("__name__")
            if not metric_name:
                continue
            labels_only = {k: str(v) for k, v in metric.items() if k != "__name__"}
            for ts, value in zip(timestamps, values):
                point = MetricPoint(
                    metric=metric_name,
                    labels=labels_only,
                    timestamp_ms=int(ts),
                    value=float(value),
                )
                existing[point.series_key()] = float(value)
        return existing


# -----------------------------------------------------------------------------
# Row extraction helpers
# -----------------------------------------------------------------------------

def extract_rows(chunks: Sequence[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for chunk in chunks:
        if isinstance(chunk, list):
            for item in chunk:
                if isinstance(item, dict) and "day" in item:
                    rows.append(item)
                elif isinstance(item, dict) and "day_totals" in item:
                    rows.extend([r for r in item.get("day_totals", []) if isinstance(r, dict)])
        elif isinstance(chunk, dict) and "day_totals" in chunk:
            rows.extend([r for r in chunk.get("day_totals", []) if isinstance(r, dict)])
        elif isinstance(chunk, dict) and "day" in chunk:
            rows.append(chunk)
    return rows


# -----------------------------------------------------------------------------
# Normalizers: GitHub report rows -> MetricPoint collections
# -----------------------------------------------------------------------------

def build_enterprise_usage_points(row: Dict[str, Any], settings: Settings) -> List[MetricPoint]:
    """
    Enterprise aggregate usage / adoption / cohorts / PR / feature / IDE / language / CLI.
    """
    points: List[MetricPoint] = []
    day = row["day"]
    ts_ms = day_to_midday_ms(day)
    base = {"enterprise": settings.gh_enterprise}

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
        p = build_point(metric_name, base, value, ts_ms)
        if p:
            points.append(p)

    for cohort in row.get("totals_by_ai_adoption_phase") or []:
        labels = {
            **base,
            "ai_adoption_phase": normalize_scalar_label_value(cohort.get("ai_adoption_phase", "unknown")),
        }
        cohort_fields = {
            "github_copilot_ai_adoption_phase_user_count": cohort.get("user_count"),
            "github_copilot_ai_adoption_phase_daily_active_users": cohort.get("daily_active_users"),
            "github_copilot_ai_adoption_phase_weekly_active_users": cohort.get("weekly_active_users"),
            "github_copilot_ai_adoption_phase_monthly_active_users": cohort.get("monthly_active_users"),
            "github_copilot_ai_adoption_phase_prompts_per_user_avg": cohort.get("avg_user_initiated_interaction_count"),
            "github_copilot_ai_adoption_phase_acceptance_rate": cohort.get("avg_code_acceptance_rate"),
        }
        for metric_name, value in cohort_fields.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

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
        p = build_point(metric_name, base, value, ts_ms)
        if p:
            points.append(p)

    for item in row.get("totals_by_feature") or []:
        labels = {**base, "feature": normalize_scalar_label_value(item.get("feature", "unknown"))}
        for metric_name, value in {
            "github_copilot_feature_loc_added_sum": item.get("loc_added_sum"),
            "github_copilot_feature_loc_deleted_sum": item.get("loc_deleted_sum"),
            "github_copilot_feature_code_generation_activity_count": item.get("code_generation_activity_count"),
            "github_copilot_feature_code_acceptance_activity_count": item.get("code_acceptance_activity_count"),
            "github_copilot_feature_user_initiated_interaction_count": item.get("user_initiated_interaction_count"),
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    for item in row.get("totals_by_ide") or []:
        labels = {**base, "ide": normalize_scalar_label_value(item.get("ide", "unknown"))}
        for metric_name, value in {
            "github_copilot_ide_loc_added_sum": item.get("loc_added_sum"),
            "github_copilot_ide_loc_deleted_sum": item.get("loc_deleted_sum"),
            "github_copilot_ide_code_generation_activity_count": item.get("code_generation_activity_count"),
            "github_copilot_ide_code_acceptance_activity_count": item.get("code_acceptance_activity_count"),
            "github_copilot_ide_user_initiated_interaction_count": item.get("user_initiated_interaction_count"),
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    for item in row.get("totals_by_language_feature") or []:
        labels = {
            **base,
            "language": normalize_scalar_label_value(item.get("language", "unknown")),
            "feature": normalize_scalar_label_value(item.get("feature", "unknown")),
        }
        for metric_name, value in {
            "github_copilot_language_feature_loc_added_sum": item.get("loc_added_sum"),
            "github_copilot_language_feature_code_generation_activity_count": item.get("code_generation_activity_count"),
            "github_copilot_language_feature_code_acceptance_activity_count": item.get("code_acceptance_activity_count"),
            "github_copilot_language_feature_prompt_count": item.get("user_initiated_interaction_count"),
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

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
    prompt_tokens = coerce_number(cli_token.get("prompt_tokens_sum"))
    output_tokens = coerce_number(cli_token.get("output_tokens_sum"))
    request_count = coerce_number(cli.get("request_count"))
    session_count = coerce_number(cli.get("session_count"))
    if prompt_tokens is not None or output_tokens is not None:
        cli_fields["github_copilot_cli_total_tokens_sum"] = (prompt_tokens or 0.0) + (output_tokens or 0.0)
    if request_count and request_count > 0 and prompt_tokens is not None:
        cli_fields["github_copilot_cli_avg_prompt_tokens_per_request"] = prompt_tokens / request_count
    if request_count and request_count > 0 and output_tokens is not None:
        cli_fields["github_copilot_cli_avg_output_tokens_per_request"] = output_tokens / request_count
    if session_count and session_count > 0 and (prompt_tokens is not None or output_tokens is not None):
        cli_fields["github_copilot_cli_avg_total_tokens_per_session"] = ((prompt_tokens or 0.0) + (output_tokens or 0.0)) / session_count
    for metric_name, value in cli_fields.items():
        p = build_point(metric_name, base, value, ts_ms)
        if p:
            points.append(p)

    return points


def build_user_usage_points(row: Dict[str, Any], settings: Settings) -> List[MetricPoint]:
    """
    Per-user usage, cohorts, modality flags, IDE / feature / language / model breakdowns.
    """
    points: List[MetricPoint] = []
    day = row["day"]
    ts_ms = day_to_midday_ms(day)
    user_login = normalize_scalar_label_value(row.get("user_login", "unknown"))
    user_id = normalize_scalar_label_value(row.get("user_id", "unknown"))
    base = {
        "enterprise": settings.gh_enterprise,
        "user_login": user_login,
        "user_id": user_id,
    }

    root_fields = {
        "github_copilot_user_daily_record": 1,
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
        p = build_point(metric_name, base, value, ts_ms)
        if p:
            points.append(p)

    ai_adoption_phase = row.get("ai_adoption_phase")
    if ai_adoption_phase:
        labels = {**base, "ai_adoption_phase": normalize_scalar_label_value(ai_adoption_phase)}
        p = build_point("github_copilot_user_ai_adoption_phase", labels, 1, ts_ms)
        if p:
            points.append(p)

    for item in row.get("totals_by_ide") or []:
        ide = normalize_scalar_label_value(item.get("ide", "unknown"))
        ide_version = ""
        plugin_version = ""

        last_known_ide_version = item.get("last_known_ide_version") or {}
        if isinstance(last_known_ide_version, dict):
            ide_version = str(last_known_ide_version.get("ide_version", ""))

        last_known_plugin_version = item.get("last_known_plugin_version") or {}
        if isinstance(last_known_plugin_version, dict):
            plugin_version = str(last_known_plugin_version.get("plugin_version", ""))

        labels = {
            **base,
            "ide": ide,
            "ide_version": ide_version,
            "plugin_version": plugin_version,
        }
        for metric_name, value in {
            "github_copilot_user_ide_daily_record": 1,
            "github_copilot_user_ide_prompt_count": item.get("user_initiated_interaction_count"),
            "github_copilot_user_ide_code_generation_activity_count": item.get("code_generation_activity_count"),
            "github_copilot_user_ide_code_acceptance_activity_count": item.get("code_acceptance_activity_count"),
            "github_copilot_user_ide_loc_added_sum": item.get("loc_added_sum"),
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    totals_by_cli = row.get("totals_by_cli") or {}
    last_known_cli_version = totals_by_cli.get("last_known_cli_version") or {}
    if isinstance(last_known_cli_version, dict):
        cli_version = str(last_known_cli_version.get("cli_version", ""))
        if cli_version:
            labels = {**base, "cli_version": cli_version}
            p = build_point("github_copilot_user_cli_daily_record", labels, 1, ts_ms)
            if p:
                points.append(p)

    for item in row.get("totals_by_feature") or []:
        labels = {**base, "feature": normalize_scalar_label_value(item.get("feature", "unknown"))}
        for metric_name, value in {
            "github_copilot_user_feature_prompt_count": item.get("user_initiated_interaction_count"),
            "github_copilot_user_feature_code_generation_activity_count": item.get("code_generation_activity_count"),
            "github_copilot_user_feature_code_acceptance_activity_count": item.get("code_acceptance_activity_count"),
            "github_copilot_user_feature_loc_added_sum": item.get("loc_added_sum"),
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    for item in row.get("totals_by_language_feature") or []:
        labels = {
            **base,
            "language": normalize_scalar_label_value(item.get("language", "unknown")),
            "feature": normalize_scalar_label_value(item.get("feature", "unknown")),
        }
        for metric_name, value in {
            "github_copilot_user_language_feature_loc_added_sum": item.get("loc_added_sum"),
            "github_copilot_user_language_feature_prompt_count": item.get("user_initiated_interaction_count"),
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    for item in row.get("totals_by_model_feature") or []:
        labels = {
            **base,
            "model": normalize_scalar_label_value(item.get("model", "unknown")),
            "feature": normalize_scalar_label_value(item.get("feature", "unknown")),
        }
        for metric_name, value in {
            "github_copilot_user_model_feature_prompt_count": item.get("user_initiated_interaction_count"),
            "github_copilot_user_model_feature_loc_added_sum": item.get("loc_added_sum"),
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    return points



def build_user_team_points(row: Dict[str, Any], settings: Settings) -> List[MetricPoint]:
    """
    Emit raw team-membership rows from the enterprise user-teams report.

    GitHub documents enterprise user-teams rows with fields such as:
      - user_id
      - user_login
      - day
      - enterprise_id
      - team_id
      - slug

    Teams with fewer than 5 seated Copilot users are excluded from this report.
    """
    points: List[MetricPoint] = []
    day = row["day"]
    ts_ms = day_to_midday_ms(day)
    user_login = normalize_scalar_label_value(row.get("user_login", "unknown"))
    user_id = normalize_scalar_label_value(row.get("user_id", "unknown"))
    team_slug = normalize_scalar_label_value(row.get("slug") or row.get("team_slug") or row.get("team") or "unknown")
    team_id = normalize_scalar_label_value(row.get("team_id") or "unknown")
    enterprise_id = normalize_scalar_label_value(row.get("enterprise_id") or "")
    organization_id = normalize_scalar_label_value(row.get("organization_id") or "")
    base = {
        "enterprise": settings.gh_enterprise,
        "user_login": user_login,
        "user_id": user_id,
        "team_slug": team_slug,
        "team_id": team_id,
    }
    if enterprise_id:
        base["enterprise_id"] = enterprise_id
    if organization_id:
        base["organization_id"] = organization_id

    p = build_point("github_copilot_user_team_membership", base, 1, ts_ms)
    if p:
        points.append(p)

    member_count_labels = {
        "enterprise": settings.gh_enterprise,
        "team_slug": team_slug,
        "team_id": team_id,
    }
    if enterprise_id:
        member_count_labels["enterprise_id"] = enterprise_id
    if organization_id:
        member_count_labels["organization_id"] = organization_id

    p = build_point("github_copilot_team_member_count", member_count_labels, 1, ts_ms)
    if p:
        points.append(p)
    return points




def build_ai_adoption_phase_rollup_points(user_rows: Sequence[Dict[str, Any]], settings: Settings) -> List[MetricPoint]:
    """
    Fallback cohort aggregates built from per-user daily rows when the enterprise
    aggregate row does not include totals_by_ai_adoption_phase for a day.
    """
    by_day_phase: Dict[Tuple[str, str], Dict[str, float]] = {}
    distinct_users: Dict[Tuple[str, str], Set[str]] = {}

    for row in user_rows:
        day = row.get("day")
        phase = normalize_scalar_label_value(row.get("ai_adoption_phase") or "").strip()
        if not day or not phase:
            continue
        key = (day, phase)
        user_id = normalize_scalar_label_value(row.get("user_id", "unknown"))
        distinct_users.setdefault(key, set()).add(user_id)
        agg = by_day_phase.setdefault(key, {
            "prompt_count": 0.0,
            "code_generation_activity_count": 0.0,
            "code_acceptance_activity_count": 0.0,
            "loc_added_sum": 0.0,
        })
        agg["prompt_count"] += float(coerce_number(row.get("user_initiated_interaction_count")) or 0.0)
        agg["code_generation_activity_count"] += float(coerce_number(row.get("code_generation_activity_count")) or 0.0)
        agg["code_acceptance_activity_count"] += float(coerce_number(row.get("code_acceptance_activity_count")) or 0.0)
        agg["loc_added_sum"] += float(coerce_number(row.get("loc_added_sum")) or 0.0)

    points: List[MetricPoint] = []
    for (day, phase), agg in sorted(by_day_phase.items()):
        ts_ms = day_to_midday_ms(day)
        labels = {"enterprise": settings.gh_enterprise, "ai_adoption_phase": phase}
        for metric_name, value in {
            "github_copilot_ai_adoption_phase_user_count": len(distinct_users.get((day, phase), set())),
            "github_copilot_ai_adoption_phase_prompt_count": agg["prompt_count"],
            "github_copilot_ai_adoption_phase_code_generation_activity_count": agg["code_generation_activity_count"],
            "github_copilot_ai_adoption_phase_code_acceptance_activity_count": agg["code_acceptance_activity_count"],
            "github_copilot_ai_adoption_phase_loc_added_sum": agg["loc_added_sum"],
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)
    return points


def build_team_rollup_points(
    user_rows: Sequence[Dict[str, Any]],
    team_rows: Sequence[Dict[str, Any]],
    settings: Settings,
) -> Tuple[List[MetricPoint], Dict[str, int]]:
    """
    Build team-level daily metrics by joining daily user-teams rows to daily
    per-user usage rows, following GitHub's documented approach.

    Returns (points, unmatched_by_day): unmatched_by_day counts team members
    present in the user-teams report for a day but absent from the users
    report for that same day. A non-zero count usually means a partial
    backfill (one report succeeded, the other didn't) and should not be
    silently treated as "zero usage" for that member.
    """
    usage_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in user_rows:
        day = str(row.get("day") or "")
        user_id = str(row.get("user_id") or "")
        if day and user_id:
            usage_index[(day, user_id)] = row

    unmatched_by_day: Dict[str, int] = {}
    by_team_day: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for team_row in team_rows:
        day = str(team_row.get("day") or "")
        user_id = str(team_row.get("user_id") or "")
        team_id = normalize_scalar_label_value(team_row.get("team_id") or "unknown")
        team_slug = normalize_scalar_label_value(team_row.get("slug") or team_row.get("team_slug") or team_row.get("team") or "unknown")
        if not day or not user_id:
            continue

        key = (day, team_id, team_slug)
        agg = by_team_day.setdefault(key, {
            "users": set(),
            "members": set(),
            "prompt_count": 0.0,
            "code_generation_activity_count": 0.0,
            "code_acceptance_activity_count": 0.0,
            "loc_added_sum": 0.0,
        })
        agg["members"].add(user_id)

        user_row = usage_index.get((day, user_id))
        if not user_row:
            unmatched_by_day[day] = unmatched_by_day.get(day, 0) + 1
            continue

        agg["users"].add(user_id)
        agg["prompt_count"] += float(coerce_number(user_row.get("user_initiated_interaction_count")) or 0.0)
        agg["code_generation_activity_count"] += float(coerce_number(user_row.get("code_generation_activity_count")) or 0.0)
        agg["code_acceptance_activity_count"] += float(coerce_number(user_row.get("code_acceptance_activity_count")) or 0.0)
        agg["loc_added_sum"] += float(coerce_number(user_row.get("loc_added_sum")) or 0.0)

    points: List[MetricPoint] = []
    for (day, team_id, team_slug), agg in sorted(by_team_day.items()):
        ts_ms = day_to_midday_ms(day)
        labels = {"enterprise": settings.gh_enterprise, "team_id": team_id, "team_slug": team_slug}
        for metric_name, value in {
            "github_copilot_team_member_count": len(agg["members"]),
            "github_copilot_team_active_users": len(agg["users"]),
            "github_copilot_team_prompt_count": agg["prompt_count"],
            "github_copilot_team_code_generation_activity_count": agg["code_generation_activity_count"],
            "github_copilot_team_code_acceptance_activity_count": agg["code_acceptance_activity_count"],
            "github_copilot_team_loc_added_sum": agg["loc_added_sum"],
        }.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    return points, unmatched_by_day



def _parse_iso_to_epoch_seconds(value: Any) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return float(dt.timestamp())
    except Exception:
        return None


def build_enterprise_seat_points(
    seat_payload: Dict[str, Any],
    settings: Settings,
    target_date: date,
    collected_at: Optional[datetime] = None,
) -> List[MetricPoint]:
    """
    Seat snapshot captured for the current day.

    Seats are mutable state rather than immutable daily reports. A user's last
    activity, active-last-7d/28d/90d status, pending cancellation status, and
    coverage ratios can change when the exporter runs again later the same day.
    Use the real collection timestamp so each run records the latest observed
    state instead of colliding on a fixed midday timestamp.
    """
    points: List[MetricPoint] = []
    ts_ms = collection_time_ms(collected_at)
    base = {
        "enterprise": settings.gh_enterprise,
    }

    seats = seat_payload.get("seats", [])
    total_seats = seat_payload.get("total_seats", 0)
    points.extend(
        p
        for p in [
            build_point("github_copilot_enterprise_seat_total", base, total_seats, ts_ms),
            build_point("github_copilot_enterprise_seat_rows_returned", base, seat_payload.get("seat_rows_returned"), ts_ms),
        ]
        if p
    )

    active_last_7d = 0
    active_last_28d = 0
    active_last_90d = 0
    never_active = 0
    pending_cancel = 0
    by_plan: Dict[str, int] = {}
    by_assigning_team: Dict[str, int] = {}

    now = datetime.now(timezone.utc)

    for seat in seats:
        assignee = seat.get("assignee") or {}
        user_login = str(
            assignee.get("login")
            or assignee.get("username")
            or assignee.get("name")
            or assignee.get("id")
            or "unknown"
        )
        organization_login = str(seat.get("organization", seat.get("organization_login", "unknown")))
        plan_type = str(seat.get("plan_type", seat.get("plan", "unknown")))
        assigning_team = str(seat.get("assigning_team", seat.get("team", "unassigned")))

        by_plan[plan_type] = by_plan.get(plan_type, 0) + 1
        by_assigning_team[assigning_team] = by_assigning_team.get(assigning_team, 0) + 1

        last_activity_at = _parse_iso_to_epoch_seconds(seat.get("last_activity_at"))
        pending_cancellation_date = _parse_iso_to_epoch_seconds(seat.get("pending_cancellation_date"))
        last_authenticated_at = _parse_iso_to_epoch_seconds(seat.get("last_authenticated_at"))
        created_at = _parse_iso_to_epoch_seconds(seat.get("created_at"))
        updated_at = _parse_iso_to_epoch_seconds(seat.get("updated_at"))
        last_activity_editor = str(seat.get("last_activity_editor", "unknown"))

        if last_activity_at is None:
            never_active += 1
        else:
            delta_days = (now.timestamp() - last_activity_at) / 86400.0
            if delta_days <= 7:
                active_last_7d += 1
            if delta_days <= 28:
                active_last_28d += 1
            if delta_days <= 90:
                active_last_90d += 1

        if pending_cancellation_date is not None:
            pending_cancel += 1

        labels = {
            **base,
            "user_login": user_login,
            "organization_login": organization_login,
            "plan_type": plan_type,
            "assigning_team": assigning_team,
            "last_activity_editor": last_activity_editor,
        }
        seat_fields = {
            "github_copilot_enterprise_seat_assigned": 1,
            "github_copilot_enterprise_seat_pending_cancellation": 1 if pending_cancellation_date is not None else 0,
            "github_copilot_enterprise_seat_last_activity_timestamp_seconds": last_activity_at,
            "github_copilot_enterprise_seat_last_authenticated_timestamp_seconds": last_authenticated_at,
            "github_copilot_enterprise_seat_created_timestamp_seconds": created_at,
            "github_copilot_enterprise_seat_updated_timestamp_seconds": updated_at,
        }
        for metric_name, value in seat_fields.items():
            p = build_point(metric_name, labels, value, ts_ms)
            if p:
                points.append(p)

    summary_fields = {
        "github_copilot_enterprise_seat_active_last_7d_total": active_last_7d,
        "github_copilot_enterprise_seat_active_last_28d_total": active_last_28d,
        "github_copilot_enterprise_seat_active_last_90d_total": active_last_90d,
        "github_copilot_enterprise_seat_never_active_total": never_active,
        "github_copilot_enterprise_seat_pending_cancellation_total": pending_cancel,
        "github_copilot_enterprise_seat_coverage_ratio_7d": (active_last_7d / total_seats * 100.0) if total_seats else 0.0,
        "github_copilot_enterprise_seat_coverage_ratio_28d": (active_last_28d / total_seats * 100.0) if total_seats else 0.0,
        "github_copilot_enterprise_seat_coverage_ratio_90d": (active_last_90d / total_seats * 100.0) if total_seats else 0.0,
    }
    for metric_name, value in summary_fields.items():
        p = build_point(metric_name, base, value, ts_ms)
        if p:
            points.append(p)

    for plan_type, count in by_plan.items():
        p = build_point(
            "github_copilot_enterprise_seat_plan_total",
            {**base, "plan_type": plan_type},
            count,
            ts_ms,
        )
        if p:
            points.append(p)

    for assigning_team, count in by_assigning_team.items():
        p = build_point(
            "github_copilot_enterprise_seat_assigning_team_total",
            {**base, "assigning_team": assigning_team},
            count,
            ts_ms,
        )
        if p:
            points.append(p)

    return points


def camel_to_snake(name: str) -> str:
    out: List[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (not name[i - 1].isupper() or (i + 1 < len(name) and not name[i + 1].isupper())):
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def apply_billing_dimension_aliases(labels: Dict[str, str], org: str, item: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """
    Standardize organization-style billing labels so dashboards can group by a
    single predictable dimension even when GitHub returns multiple variants.
    """
    item = item or {}
    normalized = dict(labels)

    org_slug = str(item.get("organization") or org or "")
    org_name = str(item.get("organizationName") or "")
    repo_name = str(item.get("repositoryName") or "")

    if org_slug:
        normalized["org"] = org_slug
        normalized["organization"] = org_slug
    elif org and org != "all":
        normalized["org"] = str(org)
        normalized["organization"] = str(org)

    if org_name:
        normalized["organization_name"] = org_name
    if repo_name:
        normalized["repository_name"] = repo_name
    return normalized


def extract_time_period_day(payload: Dict[str, Any]) -> Optional[date]:
    time_period = payload.get("timePeriod") or {}
    try:
        year = int(time_period.get("year"))
        month = int(time_period.get("month"))
        day = int(time_period.get("day")) if time_period.get("day") is not None else None
    except (TypeError, ValueError):
        return None
    if day is None:
        return None
    return date(year, month, day)


STANDARD_BILLING_NUMERIC_FIELDS = {
    "grossQuantity": "gross_quantity",
    "grossAmount": "gross_amount",
    "discountQuantity": "discount_quantity",
    "discountAmount": "discount_amount",
    "netQuantity": "net_quantity",
    "netAmount": "net_amount",
}


def build_billing_points(
    org: str,
    payload: Dict[str, Any],
    kind: str,
    settings: Settings,
    source_scope: str = "organization",
    cost_center_id: Optional[str] = None,
) -> List[MetricPoint]:
    """
    Billing usage: premium requests and AI credits.

    Supports both enterprise-scoped billing endpoints and legacy organization-scoped
    endpoints while preserving existing metric names.
    """
    points: List[MetricPoint] = []
    target_day = extract_time_period_day(payload)
    if target_day is None:
        return []
    ts_ms = snapshot_day_ms(target_day)

    family = "premium_request" if kind == "premium_request" else "ai_credit"
    base = {
        "enterprise": settings.gh_enterprise,
        "org": org,
        "billing_family": family,
        "billing_scope": source_scope,
    }
    if cost_center_id:
        base["cost_center_id"] = cost_center_id

    day_marker = build_point("github_copilot_billing_day_marker", base, 1, ts_ms)
    if day_marker:
        points.append(day_marker)

    totals: Dict[str, float] = {}
    usage_items = payload.get("usageItems", [])
    for item in usage_items:
        labels = dict(base)
        labels.update(
            {
                "model": normalize_scalar_label_value(item.get("model", "unknown")),
                "unit_type": str(item.get("unitType", "unknown")),
                "sku": str(item.get("sku", "unknown")),
                "product": str(item.get("product", "unknown")),
            }
        )
        labels = apply_billing_dimension_aliases(labels, org, item)

        price_per_unit = item.get("pricePerUnit")
        if price_per_unit is not None:
            p = build_point(f"github_copilot_billing_{family}_price_per_unit", labels, price_per_unit, ts_ms)
            if p:
                points.append(p)

        seen_fields = set()
        for field_name, metric_suffix in STANDARD_BILLING_NUMERIC_FIELDS.items():
            seen_fields.add(field_name)
            value = item.get(field_name)
            if value is not None:
                totals[metric_suffix] = totals.get(metric_suffix, 0.0) + float(value)
            p = build_point(f"github_copilot_billing_{family}_{metric_suffix}", labels, value, ts_ms)
            if p:
                points.append(p)

        # Emit any additional numeric billing fields GitHub adds in the future.
        for field_name, value in item.items():
            if field_name in seen_fields or field_name in {"model", "unitType", "sku", "product", "pricePerUnit", "organization", "organizationName", "repositoryName"}:
                continue
            num = coerce_number(value)
            if num is None:
                continue
            metric_suffix = camel_to_snake(field_name)
            totals[metric_suffix] = totals.get(metric_suffix, 0.0) + float(num)
            p = build_point(f"github_copilot_billing_{family}_{metric_suffix}", labels, num, ts_ms)
            if p:
                points.append(p)

    for suffix, total in sorted(totals.items()):
        p = build_point(f"github_copilot_billing_{family}_total_{suffix}", base, total, ts_ms)
        if p:
            points.append(p)

    return points


def build_billing_usage_summary_points(
    payload: Dict[str, Any],
    settings: Settings,
    cost_center_id: Optional[str] = None,
) -> List[MetricPoint]:
    """
    Enterprise billing usage summary across all paid GitHub products.

    This complements Copilot-specific billing data with higher-level enterprise spend
    and cost-center metrics documented in GitHub's billing usage API.
    """
    points: List[MetricPoint] = []
    target_day = extract_time_period_day(payload)
    if target_day is None:
        return []
    ts_ms = snapshot_day_ms(target_day)
    base = {
        "enterprise": settings.gh_enterprise,
        "billing_scope": "enterprise_summary",
    }
    if cost_center_id:
        base["cost_center_id"] = cost_center_id

    marker = build_point("github_billing_usage_summary_day_marker", base, 1, ts_ms)
    if marker:
        points.append(marker)

    totals: Dict[str, float] = {}
    for item in payload.get("usageItems", []):
        labels = dict(base)
        for src_key, label_key in {
            "product": "product",
            "sku": "sku",
            "unitType": "unit_type",
        }.items():
            if item.get(src_key) is not None:
                labels[label_key] = str(item.get(src_key))
        labels = apply_billing_dimension_aliases(labels, "", item)

        if item.get("pricePerUnit") is not None:
            p = build_point("github_billing_usage_summary_price_per_unit", labels, item.get("pricePerUnit"), ts_ms)
            if p:
                points.append(p)

        for field_name in ["quantity", "grossQuantity", "grossAmount", "discountQuantity", "discountAmount", "netQuantity", "netAmount"]:
            value = item.get(field_name)
            num = coerce_number(value)
            if num is None:
                continue
            suffix = camel_to_snake(field_name)
            totals[suffix] = totals.get(suffix, 0.0) + float(num)
            p = build_point(f"github_billing_usage_summary_{suffix}", labels, num, ts_ms)
            if p:
                points.append(p)

    for suffix, total in sorted(totals.items()):
        p = build_point(f"github_billing_usage_summary_total_{suffix}", base, total, ts_ms)
        if p:
            points.append(p)

    return points


# -----------------------------------------------------------------------------
# Exact diff engine
# -----------------------------------------------------------------------------

def diff_points_against_vm(
    vm: VictoriaMetricsClient,
    points: Sequence[MetricPoint],
    day: str,
    family: str,
    settings: Settings,
) -> Tuple[List[MetricPoint], int, int]:
    """
    Returns:
      - missing_points_to_write
      - skipped_count
      - drift_count
    """
    if not points:
        LAST_EXPECTED_POINTS.labels(enterprise=settings.gh_enterprise, family=family).set(0)
        LAST_MISSING_POINTS.labels(enterprise=settings.gh_enterprise, family=family).set(0)
        return [], 0, 0

    LAST_EXPECTED_POINTS.labels(enterprise=settings.gh_enterprise, family=family).set(len(points))

    if not settings.exact_diff_enabled:
        LAST_MISSING_POINTS.labels(enterprise=settings.gh_enterprise, family=family).set(len(points))
        return list(points), 0, 0

    metric_names = sorted({p.metric for p in points})
    existing = vm.export_existing_points(
        metric_names=metric_names,
        day=day,
        extra_match_labels={"enterprise": settings.gh_enterprise},
    )

    missing: List[MetricPoint] = []
    skipped = 0
    drift = 0

    for point in points:
        key = point.series_key()
        if key not in existing:
            missing.append(point)
            continue

        existing_value = float(existing[key])
        if abs(existing_value - point.value) < 1e-12:
            skipped += 1
        else:
            drift += 1
            logging.warning(
                "Drift detected family=%s metric=%s labels=%s ts_ms=%s existing=%s new=%s policy=%s",
                family,
                point.metric,
                point.labels,
                point.timestamp_ms,
                existing_value,
                point.value,
                settings.drift_policy,
            )
            if settings.drift_policy == "rewrite":
                missing.append(point)

    LAST_MISSING_POINTS.labels(enterprise=settings.gh_enterprise, family=family).set(len(missing))
    return missing, skipped, drift


# -----------------------------------------------------------------------------
# Unified processing pipeline
# -----------------------------------------------------------------------------

class CopilotExporter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.github = GitHubClient(settings)
        self.vm = VictoriaMetricsClient(settings)

        self.last_daily_import_day: Optional[str] = None
        self.bootstrapped = False
        self.backfill_done = False

    def _import_family_points(self, family: str, day: str, points: Sequence[MetricPoint]) -> int:
        missing, skipped, drift = diff_points_against_vm(self.vm, points, day, family, self.settings)
        SKIPPED_POINTS.labels(enterprise=self.settings.gh_enterprise, family=family).inc(skipped)
        DRIFT_POINTS.labels(enterprise=self.settings.gh_enterprise, family=family).inc(drift)

        if self.settings.dry_run:
            logging.info(
                "DRY_RUN family=%s day=%s expected=%s missing=%s skipped=%s drift=%s",
                family, day, len(points), len(missing), skipped, drift,
            )
            return 0

        imported = self.vm.import_points(missing)
        IMPORTED_POINTS.labels(enterprise=self.settings.gh_enterprise, family=family).inc(imported)

        logging.info(
            "Imported family=%s day=%s expected=%s missing=%s skipped=%s drift=%s imported=%s",
            family, day, len(points), len(missing), skipped, drift, imported,
        )
        return imported

    def _process_daily_usage(self, day: str) -> int:
        total = 0

        enterprise_rows = extract_rows(self.github.fetch_enterprise_usage_day(day))
        enterprise_points: List[MetricPoint] = []
        for row in enterprise_rows:
            enterprise_points.extend(build_enterprise_usage_points(row, self.settings))
        total += self._import_family_points("enterprise_usage", day, enterprise_points)

        user_rows = extract_rows(self.github.fetch_users_usage_day(day))
        user_points: List[MetricPoint] = []
        for row in user_rows:
            user_points.extend(build_user_usage_points(row, self.settings))
        total += self._import_family_points("user_usage", day, user_points)

        team_rows = extract_rows(self.github.fetch_user_teams_day(day))
        logging.info("Fetched user_teams rows day=%s count=%s", day, len(team_rows))
        team_points: List[MetricPoint] = []
        for row in team_rows:
            team_points.extend(build_user_team_points(row, self.settings))
        total += self._import_family_points("user_teams", day, team_points)

        team_rollup_points, unmatched_by_day = build_team_rollup_points(user_rows, team_rows, self.settings)
        total += self._import_family_points("team_rollups", day, team_rollup_points)
        unmatched = unmatched_by_day.get(day, 0)
        TEAM_JOIN_UNMATCHED_USERS.labels(enterprise=self.settings.gh_enterprise, day=day).set(unmatched)
        if unmatched:
            logging.warning(
                "Team join unmatched users day=%s count=%s "
                "(team-membership row had no corresponding users-1-day row; "
                "likely partial backfill of the users report for this day)",
                day, unmatched,
            )

        cohort_rollup_points = build_ai_adoption_phase_rollup_points(user_rows, self.settings)
        logging.info("Built cohort_rollup points day=%s count=%s", day, len(cohort_rollup_points))
        total += self._import_family_points("cohort_rollups", day, cohort_rollup_points)

        return total

    def _process_daily_billing(self, day: str) -> int:
        if not self.settings.enable_billing_reports:
            return 0
        total = 0
        d = datetime.strptime(day, "%Y-%m-%d").date()
        org_targets = self.settings.gh_billing_orgs or ("all",)

        for kind in ("premium_request", "ai_credit"):
            if self.settings.billing_scope == "enterprise":
                # Enterprise aggregate view.
                aggregate = self.github.fetch_enterprise_billing_usage(kind, d.year, d.month, d.day)
                aggregate_points = build_billing_points("all", aggregate, kind, self.settings, source_scope="enterprise")
                total += self._import_family_points(f"billing_{kind}:all", day, aggregate_points)

                # Optional per-org enterprise-filtered slices for team/showback visibility.
                for org in self.settings.gh_billing_orgs:
                    payload = self.github.fetch_enterprise_billing_usage(kind, d.year, d.month, d.day, organization=org)
                    points = build_billing_points(org, payload, kind, self.settings, source_scope="enterprise", cost_center_id=None)
                    total += self._import_family_points(f"billing_{kind}:{org}", day, points)
            else:
                for org in org_targets:
                    if org == "all":
                        continue
                    payload = self.github.fetch_org_billing_usage(org, kind, d.year, d.month, d.day)
                    points = build_billing_points(org, payload, kind, self.settings, source_scope="organization")
                    total += self._import_family_points(f"billing_{kind}:{org}", day, points)

        if self.settings.enable_billing_usage_summary:
            summary_payload = self.github.fetch_enterprise_billing_usage_summary(d.year, d.month, d.day)
            summary_points = build_billing_usage_summary_points(summary_payload, self.settings)
            total += self._import_family_points("billing_usage_summary:all", day, summary_points)
            for cost_center_id in self.settings.gh_cost_center_ids:
                payload = self.github.fetch_enterprise_billing_usage_summary(d.year, d.month, d.day, cost_center_id=cost_center_id)
                points = build_billing_usage_summary_points(payload, self.settings, cost_center_id=cost_center_id)
                total += self._import_family_points(f"billing_usage_summary:cost_center:{cost_center_id}", day, points)

        return total

    def _process_daily_seats(self, day: str) -> int:
        """
        Capture the current enterprise seat state for today only.

        There is no historical daily seat report API, so backfills should not
        attempt to invent past seat snapshots. When we do collect today's seat
        state, use the real collection timestamp rather than a fixed midday
        timestamp so same-day reruns append a newer observation instead of
        producing drift on an immutable key.
        """
        target_date = datetime.strptime(day, "%Y-%m-%d").date()
        collected_at = datetime.now(timezone.utc)
        if target_date != collected_at.date():
            return 0
        payload = self.github.fetch_enterprise_seats()
        points = build_enterprise_seat_points(payload, self.settings, target_date, collected_at=collected_at)
        return self._import_family_points("seat_snapshot", day, points)

    def process_day(self, day: str) -> int:
        total = 0
        total += self._process_daily_usage(day)
        total += self._process_daily_billing(day)
        total += self._process_daily_seats(day)
        return total

    def bootstrap_28d_once(self) -> None:
        if not self.settings.bootstrap_28d or self.bootstrapped:
            return

        logging.info("Starting exact-diff 28-day bootstrap for enterprise=%s", self.settings.gh_enterprise)
        logging.warning(
            "28-day bootstrap covers enterprise_usage and user_usage only. "
            "team_rollups, user_teams, and cohort_rollups for this window will "
            "remain empty unless ENABLE_DATE_RANGE_BACKFILL is also run for the "
            "same 28-day window (see backfill runbook)."
        )

        enterprise_rows = extract_rows(self.github.fetch_enterprise_usage_28d())
        user_rows = extract_rows(self.github.fetch_users_usage_28d())

        # Normalize rows by day and run through the same exact-diff family pipeline.
        enterprise_by_day: Dict[str, List[Dict[str, Any]]] = {}
        for row in enterprise_rows:
            enterprise_by_day.setdefault(row["day"], []).append(row)

        user_by_day: Dict[str, List[Dict[str, Any]]] = {}
        for row in user_rows:
            user_by_day.setdefault(row["day"], []).append(row)

        all_days = sorted(set(enterprise_by_day) | set(user_by_day))
        for day in all_days:
            enterprise_points: List[MetricPoint] = []
            for row in enterprise_by_day.get(day, []):
                enterprise_points.extend(build_enterprise_usage_points(row, self.settings))
            self._import_family_points("enterprise_usage", day, enterprise_points)

            user_points: List[MetricPoint] = []
            for row in user_by_day.get(day, []):
                user_points.extend(build_user_usage_points(row, self.settings))
            self._import_family_points("user_usage", day, user_points)

        self.bootstrapped = True
        if all_days:
            self.last_daily_import_day = max(all_days)
        logging.info("Completed exact-diff 28-day bootstrap for enterprise=%s latest_day=%s", self.settings.gh_enterprise, self.last_daily_import_day)


    def backfill_date_range_once(self) -> None:
        if not self.settings.enable_date_range_backfill or self.backfill_done:
            return

        if not self.settings.backfill_start_day or not self.settings.backfill_end_day:
            raise RuntimeError("Backfill requested but BACKFILL_START_DAY/BACKFILL_END_DAY is missing")

        start_date = datetime.strptime(self.settings.backfill_start_day, "%Y-%m-%d").date()
        end_date = datetime.strptime(self.settings.backfill_end_day, "%Y-%m-%d").date()
        if end_date < start_date:
            raise RuntimeError("BACKFILL_END_DAY must be >= BACKFILL_START_DAY")

        logging.info(
            "Starting exact-diff date-range backfill enterprise=%s start=%s end=%s",
            self.settings.gh_enterprise,
            self.settings.backfill_start_day,
            self.settings.backfill_end_day,
        )

        failed_days: List[str] = []
        current = start_date
        while current <= end_date:
            day = current.isoformat()
            try:
                self.process_day(day)
            except Exception:
                FAILED_BACKFILL_DAYS.labels(enterprise=self.settings.gh_enterprise).inc()
                failed_days.append(day)
                logging.exception(
                    "Backfill day failed enterprise=%s day=%s; continuing to next day",
                    self.settings.gh_enterprise,
                    day,
                )
            current += timedelta(days=1)

        self.backfill_done = True
        logging.info(
            "Completed exact-diff date-range backfill enterprise=%s start=%s end=%s failed_days=%s",
            self.settings.gh_enterprise,
            self.settings.backfill_start_day,
            self.settings.backfill_end_day,
            ",".join(failed_days) if failed_days else "none",
        )

    def import_latest_stable_day_if_needed(self) -> None:
        target_day = (datetime.now(timezone.utc).date() - timedelta(days=self.settings.data_lag_days)).isoformat()
        if self.last_daily_import_day == target_day:
            logging.info("Stable day %s already processed in this pod lifecycle; skipping", target_day)
            return
        self.process_day(target_day)
        self.last_daily_import_day = target_day
        logging.info("Completed exact-diff stable day import enterprise=%s day=%s", self.settings.gh_enterprise, target_day)

    def import_today_seat_snapshot(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        self._process_daily_seats(today)

    def run_cycle(self) -> None:
        start = time.time()
        try:
            self.backfill_date_range_once()
            self.bootstrap_28d_once()
            self.import_latest_stable_day_if_needed()
            # Refresh today's seat snapshot separately.
            self.import_today_seat_snapshot()

            EXPORTER_UP.labels(enterprise=self.settings.gh_enterprise).set(1)
            LAST_SUCCESS.labels(enterprise=self.settings.gh_enterprise).set(time.time())
        except Exception:
            ERRORS.labels(enterprise=self.settings.gh_enterprise).inc()
            EXPORTER_UP.labels(enterprise=self.settings.gh_enterprise).set(0)
            logging.exception("Collector cycle failed for enterprise=%s", self.settings.gh_enterprise)
            raise
        finally:
            LAST_DURATION.labels(enterprise=self.settings.gh_enterprise).set(time.time() - start)


# -----------------------------------------------------------------------------
# CLI / main loop
# -----------------------------------------------------------------------------

def run_once_for_cli(start_day: str, end_day: str) -> int:
    settings = SETTINGS
    exporter = CopilotExporter(settings)
    start = datetime.strptime(start_day, "%Y-%m-%d").date()
    end = datetime.strptime(end_day, "%Y-%m-%d").date()
    if end < start:
        raise SystemExit("--end-day must be >= --start-day")

    current = start
    while current <= end:
        exporter.process_day(current.isoformat())
        current += timedelta(days=1)
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub Copilot exact-diff exporter")
    parser.add_argument("--start-day", help="Run an exact-diff backfill / import from this day (YYYY-MM-DD)")
    parser.add_argument("--end-day", help="Run an exact-diff backfill / import through this day (YYYY-MM-DD)")
    parser.add_argument("--once", action="store_true", help="Run a single collector cycle and exit")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.start_day and args.end_day:
        return run_once_for_cli(args.start_day, args.end_day)

    exporter = CopilotExporter(SETTINGS)

    logging.info(
        "Starting GitHub Copilot exact-diff exporter on :%s for enterprise=%s schema=%s",
        SETTINGS.export_port,
        SETTINGS.gh_enterprise,
        SCHEMA_VERSION,
    )
    start_http_server(SETTINGS.export_port)

    if args.once:
        exporter.run_cycle()
        return 0

    while True:
        try:
            exporter.run_cycle()
        except Exception:
            pass
        time.sleep(SETTINGS.poll_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())