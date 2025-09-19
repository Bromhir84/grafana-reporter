import requests, re
from datetime import datetime

_METRIC_REGEX = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:[{(])')
_PER_SUFFIX_REGEX = re.compile(r'_per_[a-zA-Z0-9]+$')

from ..config import PROMETHEUS_URL

def extract_grafana_vars(dashboard_json):
    return {v["name"]: str(v.get("current", {}).get("value", ".*"))
            for v in dashboard_json.get("templating", {}).get("list", [])}

def resolve_grafana_vars(query: str, variables: dict, start: datetime, end: datetime, duration: str):
    for var, value in variables.items():
        if not value or value in ("$__all", "['$__all']"):
            value = ".*"
        query = query.replace(f"${var}", value).replace(f"${{{var}}}", value)
    return query.replace("$__range", duration)

def extract_metric(expr: str) -> str:
    matches = _METRIC_REGEX.findall(expr)
    promql_functions = {
        "sum", "avg", "min", "max", "count", "stddev", "stdvar",
        "rate", "irate", "increase", "delta", "idelta",
        "sum_over_time", "avg_over_time", "min_over_time", "max_over_time",
        "quantile_over_time", "count_over_time", "last_over_time"
    }
    for token in matches:
        if token not in promql_functions:
            token = token.split(":")[0]
            token = _PER_SUFFIX_REGEX.sub("", token)
            return token.replace("_", " ").title()
    return "Unknown Metric"

def query_prometheus_range(expr: str, start: datetime, end: datetime, step: int = 3600):
    params = {"query": expr, "start": int(start.timestamp()), "end": int(end.timestamp()), "step": step}
    r = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params, timeout=60)
    r.raise_for_status()
    return r.json()
