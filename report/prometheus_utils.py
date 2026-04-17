import re
import os
import requests
import math
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from ..config import PROMETHEUS_URL
from zoneinfo import ZoneInfo

CEST = ZoneInfo("Europe/Amsterdam")
TIME_TO_ROUND_TO_PERIOD_END = os.getenv("TIME_TO_ROUND_TO_PERIOD_END", "true").lower() == "true"


def _normalize_cest(dt: datetime) -> datetime:
    """Rebuild datetime in Europe/Amsterdam so DST offset matches the local wall time."""
    return datetime(
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        tzinfo=CEST,
    )


def _round_grafana_time(dt: datetime, unit: str) -> datetime:
    """Round down datetime to the start of the requested unit."""
    if unit == "M":
        return _normalize_cest(dt.replace(day=1, hour=0, minute=0, second=0))
    if unit == "w":
        rounded = (dt - relativedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0)
        return _normalize_cest(rounded)
    if unit == "d":
        return _normalize_cest(dt.replace(hour=0, minute=0, second=0))
    if unit == "h":
        return _normalize_cest(dt.replace(minute=0, second=0))
    if unit == "m":
        return _normalize_cest(dt.replace(second=0))
    if unit == "s":
        return _normalize_cest(dt)
    return _normalize_cest(dt)


def _rounding_unit_from_expr(time_str: str):
    m = re.search(r"/([smhdwM])$", time_str)
    return m.group(1) if m else None


def _end_of_rounded_period(dt: datetime, rounding_unit: str) -> datetime:
    """Convert a rounded boundary timestamp to the end of that rounded period."""
    if rounding_unit == "M":
        return _normalize_cest(dt + relativedelta(months=1, seconds=-1))
    if rounding_unit == "w":
        return _normalize_cest(dt + relativedelta(weeks=1, seconds=-1))
    if rounding_unit == "d":
        return _normalize_cest(dt + relativedelta(days=1, seconds=-1))
    if rounding_unit == "h":
        return _normalize_cest(dt + relativedelta(hours=1, seconds=-1))
    if rounding_unit == "m":
        return _normalize_cest(dt + relativedelta(minutes=1, seconds=-1))
    return _normalize_cest(dt)

def parse_grafana_time(time_str: str) -> datetime:
    """
    Parse Grafana time expressions like:
      - now
      - now-6h
      - now-1M/M
      - now/M
    Always returns a datetime in Europe/Amsterdam timezone.
    """
    now = _normalize_cest(datetime.now(CEST).replace(microsecond=0))

    if time_str == "now":
        return now
    m = re.match(r"now-(\d+)([smhdwM])", time_str)
    if m:
        value, unit = m.groups()
        value = int(value)
        if unit == "s":
            dt = now - relativedelta(seconds=value)
        elif unit == "m":
            dt = now - relativedelta(minutes=value)
        elif unit == "h":
            dt = now - relativedelta(hours=value)
        elif unit == "d":
            dt = now - relativedelta(days=value)
        elif unit == "w":
            dt = now - relativedelta(weeks=value)
        elif unit == "M":
            dt = now - relativedelta(months=value)
        else:
            dt = now
    else:
        dt = now

    rounding_unit = _rounding_unit_from_expr(time_str)
    if rounding_unit:
        dt = _round_grafana_time(dt, rounding_unit)

    return _normalize_cest(dt)

def compute_range_from_env(time_from: str, time_to: str):
    """Return start and end datetime based on TIME_FROM and TIME_TO (CEST-aware)."""
    start = parse_grafana_time(time_from)
    end = parse_grafana_time(time_to)

    # Optional behavior: expand rounded upper bounds (e.g. now-1M/M) to end-of-period.
    if TIME_TO_ROUND_TO_PERIOD_END:
        rounding_unit = _rounding_unit_from_expr(time_to)
        if rounding_unit:
            end = _end_of_rounded_period(end, rounding_unit)

    return start, end


def compute_prometheus_duration(start, end) -> str:
    delta = end - start
    hours = int(delta.total_seconds() / 3600)
    return f"{hours}h"


def _seconds_to_prom_duration(seconds: int) -> str:
    """Convert seconds to a compact Prometheus duration string."""
    seconds = max(1, int(seconds))
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def compute_query_step_seconds(start: datetime, end: datetime, max_points: int = 1000, min_step: int = 60) -> int:
    """
    Compute a query_range step that keeps result cardinality bounded.
    Mirrors Grafana behavior by deriving interval from total range / max data points.
    """
    range_seconds = max(1, int((end - start).total_seconds()))
    dynamic_step = math.ceil(range_seconds / max(1, int(max_points)))
    raw_step = max(int(min_step), int(dynamic_step))

    # Grafana-like rounded intervals for $__interval.
    interval_buckets = [
        1, 2, 5, 10, 15, 20, 30,
        60, 120, 300, 600, 900, 1200, 1800,
        3600, 7200, 10800, 21600, 43200,
        86400, 604800, 2592000,
    ]
    for bucket in interval_buckets:
        if raw_step <= bucket:
            return bucket

    return raw_step


def parse_duration_to_seconds(value) -> int | None:
    """Parse Grafana/Prometheus duration strings into seconds."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # Grafana min interval values may be prefixed with '>'
    text = text.lstrip(">")

    # Ignore unresolved macros; caller should provide fallback.
    if text in ("$__interval", "${__interval}"):
        return None

    if text.endswith("ms") and text[:-2].isdigit():
        return max(1, math.ceil(int(text[:-2]) / 1000))

    m = re.match(r"^(\d+)([smhdwM])$", text)
    if not m:
        return None

    value_num = int(m.group(1))
    unit = m.group(2)
    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 7 * 86400,
        "M": 30 * 86400,
    }
    return value_num * multipliers[unit]


def extract_uid_from_url(url: str) -> str:
    match = re.search(r"/d/([^/]+)/", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid dashboard URL format. Expected /d/<uid>/")


def resolve_grafana_vars(query: str, variables: dict, start: datetime, end: datetime, interval_seconds: int | None = None) -> str:
    range_seconds = max(1, int((end - start).total_seconds()))
    if interval_seconds is None:
        interval_seconds = compute_query_step_seconds(start, end)
    aligned_range_seconds = range_seconds
    if interval_seconds and interval_seconds > 0 and range_seconds >= interval_seconds:
        aligned_range_seconds = (range_seconds // interval_seconds) * interval_seconds

    macro_values = {
        "$__range": _seconds_to_prom_duration(aligned_range_seconds),
        "${__range}": _seconds_to_prom_duration(aligned_range_seconds),
        "$__range_s": str(aligned_range_seconds),
        "${__range_s}": str(aligned_range_seconds),
        "$__range_ms": str(aligned_range_seconds * 1000),
        "${__range_ms}": str(aligned_range_seconds * 1000),
        "$__interval": _seconds_to_prom_duration(interval_seconds),
        "${__interval}": _seconds_to_prom_duration(interval_seconds),
        "$__interval_ms": str(interval_seconds * 1000),
        "${__interval_ms}": str(interval_seconds * 1000),
        "$__rate_interval": _seconds_to_prom_duration(max(60, interval_seconds * 4)),
        "${__rate_interval}": _seconds_to_prom_duration(max(60, interval_seconds * 4)),
    }

    for macro, replacement in macro_values.items():
        query = query.replace(macro, replacement)

    for var, value in variables.items():
        if not value or value in ("$__all", "['$__all']"):
            value = ".*"
        query = query.replace(f"${var}", value).replace(f"${{{var}}}", value)

    return query


def extract_metric(expr: str) -> str:
    matches = re.findall(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:[{(])', expr)
    if not matches:
        return "Unknown Metric"
    promql_functions = {
        "sum", "avg", "min", "max", "count", "stddev", "stdvar",
        "rate", "irate", "increase", "delta", "idelta",
        "sum_over_time", "avg_over_time", "min_over_time", "max_over_time",
        "quantile_over_time", "count_over_time", "last_over_time"
    }
    for token in matches:
        if token not in promql_functions:
            token = token.split(":")[0]
            token = re.sub(r'_per_[a-zA-Z0-9]+$', '', token)
            token = token.replace("_", " ").title()
            return token
    return "Unknown Metric"


def query_prometheus_range(expr: str, start: datetime, end: datetime, step: int = 3600, align_to_step: bool = False):
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    start_ts = int(start_utc.timestamp())
    end_ts = int(end_utc.timestamp())

    if align_to_step and step > 0:
        # Grafana commonly aligns range boundaries to step for stable reduction results.
        start_ts = (start_ts // step) * step
        end_ts = (end_ts // step) * step
        if end_ts < start_ts:
            end_ts = start_ts

    params = {
        "query": expr,
        "start": start_ts,
        "end": end_ts,
        "step": step
    }
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def query_prometheus_instant(expr: str, eval_time: datetime):
    """Query Prometheus at an exact evaluation timestamp."""
    eval_time_utc = eval_time.astimezone(timezone.utc)
    params = {
        "query": expr,
        "time": int(eval_time_utc.timestamp()),
    }
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()
