import re
import requests
import math
from datetime import datetime
from dateutil.relativedelta import relativedelta
from ..config import PROMETHEUS_URL
import pytz

CEST = pytz.timezone("Europe/Amsterdam")

def parse_grafana_time(time_str: str) -> datetime:
    """
    Parse Grafana time expressions like:
      - now
      - now-6h
      - now-1M/M
      - now/M
    Always returns a datetime in Europe/Amsterdam timezone.
    """
    now = datetime.now(CEST).replace(microsecond=0)

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

    if time_str.endswith("/M"):
        dt = dt.replace(day=1, hour=0, minute=0, second=0)
    elif time_str.endswith("/d"):
        dt = dt.replace(hour=0, minute=0, second=0)
    elif time_str.endswith("/w"):
        dt = dt - relativedelta(days=dt.weekday())
        dt = dt.replace(hour=0, minute=0, second=0)

    return dt

def compute_range_from_env(time_from: str, time_to: str):
    """Return start and end datetime based on TIME_FROM and TIME_TO (CEST-aware)."""
    start = parse_grafana_time(time_from)
    end = parse_grafana_time(time_to)
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
    return max(int(min_step), int(dynamic_step))


def extract_uid_from_url(url: str) -> str:
    match = re.search(r"/d/([^/]+)/", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid dashboard URL format. Expected /d/<uid>/")


def resolve_grafana_vars(query: str, variables: dict, start: datetime, end: datetime) -> str:
    range_seconds = max(1, int((end - start).total_seconds()))
    interval_seconds = compute_query_step_seconds(start, end)

    macro_values = {
        "$__range": _seconds_to_prom_duration(range_seconds),
        "${__range}": _seconds_to_prom_duration(range_seconds),
        "$__range_s": str(range_seconds),
        "${__range_s}": str(range_seconds),
        "$__range_ms": str(range_seconds * 1000),
        "${__range_ms}": str(range_seconds * 1000),
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


def query_prometheus_range(expr: str, start: datetime, end: datetime, step: int = 3600):
    start_utc = start.astimezone(pytz.utc)
    end_utc = end.astimezone(pytz.utc)
    params = {
        "query": expr,
        "start": int(start_utc.timestamp()),
        "end": int(end_utc.timestamp()),
        "step": step
    }
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()
