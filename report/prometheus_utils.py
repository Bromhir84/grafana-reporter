import re
import requests
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
    ...
    # keep same logic as before
    ...

def compute_range_from_env(time_from: str, time_to: str):
    """Return start and end datetime based on TIME_FROM and TIME_TO (CEST-aware)."""
    start = parse_grafana_time(time_from)
    end = parse_grafana_time(time_to)
    return start, end


def compute_prometheus_duration(start, end) -> str:
    delta = end - start
    hours = int(delta.total_seconds() / 3600)
    return f"{hours}h"


def extract_uid_from_url(url: str) -> str:
    match = re.search(r"/d/([^/]+)/", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid dashboard URL format. Expected /d/<uid>/")


def resolve_grafana_vars(query: str, variables: dict, start: datetime, end: datetime) -> str:
    for var, value in variables.items():
        if not value or value in ("$__all", "['$__all']"):
            value = ".*"
        query = query.replace(f"${var}", value).replace(f"${{{var}}}", value)
    query = query.replace("$__range", compute_prometheus_duration(start, end))
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
