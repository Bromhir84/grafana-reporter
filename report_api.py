# ─────────────────────────────────────────────────────────────
# FastAPI Grafana Report Script (PDF + CSV, temp dashboard)
# ─────────────────────────────────────────────────────────────
import os
import re
import io
import csv
import time
import math
import img2pdf
import requests
from datetime import datetime
from dateutil.relativedelta import relativedelta
from email.message import EmailMessage
import smtplib
import pandas as pd
from PIL import Image
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging
from playwright.sync_api import sync_playwright


app = FastAPI(root_path=os.getenv("ROOT_PATH", "/report"))

# Allow Grafana front-end to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Environment variables
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL","http://your-prometheus-server:9090")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_API_KEY = os.getenv("GRAFANA_API_KEY")
TIME_FROM = os.getenv("TIME_FROM", "now-6h")
TIME_TO = os.getenv("TIME_TO", "now")
TIME_TO_CSV = os.getenv("TIME_TO_CSV", "now")
EMAIL_FROM = os.getenv("EMAIL_FROM")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
A4_WIDTH_PX = 2480
A4_HEIGHT_PX = 3508
A4_BG_COLOR = "white"

excluded_titles = ["Report Button", "Another panel"]
excluded_titles_lower = [t.strip().lower() for t in excluded_titles]

headers = {"Authorization": f"Bearer {GRAFANA_API_KEY}"}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

class ReportRequest(BaseModel):
    dashboard_url: str
    email_report: bool = False
    email_to: str = None


def _round_grafana_time(dt: datetime, unit: str) -> datetime:
    """Round down datetime to the start of the requested unit."""
    if unit == "M":
        return dt.replace(day=1, hour=0, minute=0, second=0)
    if unit == "w":
        return (dt - relativedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0)
    if unit == "d":
        return dt.replace(hour=0, minute=0, second=0)
    if unit == "h":
        return dt.replace(minute=0, second=0)
    if unit == "m":
        return dt.replace(second=0)
    if unit == "s":
        return dt
    return dt


def _rounding_unit_from_expr(time_str: str):
    m = re.search(r"/([smhdwM])$", time_str)
    return m.group(1) if m else None


def _end_of_rounded_period(dt: datetime, rounding_unit: str) -> datetime:
    """Convert a rounded boundary timestamp to the end of that rounded period."""
    if rounding_unit == "M":
        return dt + relativedelta(months=1, seconds=-1)
    if rounding_unit == "w":
        return dt + relativedelta(weeks=1, seconds=-1)
    if rounding_unit == "d":
        return dt + relativedelta(days=1, seconds=-1)
    if rounding_unit == "h":
        return dt + relativedelta(hours=1, seconds=-1)
    if rounding_unit == "m":
        return dt + relativedelta(minutes=1, seconds=-1)
    return dt

def parse_grafana_time(time_str: str) -> datetime:
    """
    Parse Grafana time expressions like:
      - now
      - now-6h
      - now-1M/M
      - now/M
    Returns a UTC datetime.
    """
    now = datetime.utcnow().replace(microsecond=0)
    
    if time_str == "now":
        return now

    # Match relative offset: now-6h, now-30m, now-1d, now-1M
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

    # Snap to start of rounded period if /unit is provided.
    rounding_unit = _rounding_unit_from_expr(time_str)
    if rounding_unit:
        dt = _round_grafana_time(dt, rounding_unit)

    return dt

def compute_range_from_env(time_from: str, time_to: str):
    """Return start and end datetime based on TIME_FROM and TIME_TO."""
    start = parse_grafana_time(time_from)
    end = parse_grafana_time(time_to)

    # For rounded upper bounds (for example now-1M/M), use end-of-period.
    rounding_unit = _rounding_unit_from_expr(time_to)
    if rounding_unit:
        end = _end_of_rounded_period(end, rounding_unit)

    return start, end

def compute_prometheus_duration(start: datetime, end: datetime) -> str:
    """Return Prometheus duration string (e.g., '720h') for use in sum_over_time."""
    delta = end - start
    # Prometheus durations: seconds (s), minutes (m), hours (h), days (d)
    # We'll convert everything to hours for convenience
    hours = int(delta.total_seconds() / 3600)
    return f"{hours}h"


def _seconds_to_prom_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def compute_query_step_seconds(start: datetime, end: datetime, max_points: int = 1000, min_step: int = 60) -> int:
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
    for index, bucket in enumerate(interval_buckets):
        if raw_step <= bucket:
            if index == 0:
                return bucket
            previous_bucket = interval_buckets[index - 1]
            if abs(raw_step - previous_bucket) <= abs(bucket - raw_step):
                return previous_bucket
            return bucket

    return raw_step

def extract_uid_from_url(url: str) -> str:
    match = re.search(r"/d/([^/]+)/", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid dashboard URL format. Expected /d/<uid>/")

def filter_panels(panels, excluded_titles_lower):
    filtered = []

    for panel in panels:
        # Recursively filter nested panels
        new_panel = panel.copy()
        if "panels" in panel:
            new_panel["panels"] = filter_panels(panel["panels"], excluded_titles_lower)

        # Include panel only if its title is not excluded
        title = panel.get("title", "").strip().lower()
        if title not in excluded_titles_lower:
            filtered.append(new_panel)

    return filtered

def clone_dashboard_without_panels(dashboard_uid: str, excluded_titles=None):
    excluded_titles = excluded_titles or []
    excluded_titles_lower = [t.strip().lower() for t in excluded_titles]

    # Fetch original dashboard
    url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    dash = r.json()["dashboard"]

    # Extract dashboard template variables
    GRAFANA_VARS = extract_grafana_vars(dash)

    # --- Step 1: Filter out excluded panels recursively ---
    dash["panels"] = filter_panels(dash.get("panels", []), excluded_titles_lower)

    # --- Step 2: Extract table panels after filtering ---
    table_panels = []
    def walk_panels(panels):
        for panel in panels:
            if panel.get("type") == "table":
                exprs = [t["expr"] for t in panel.get("targets", []) if "expr" in t]
                table_panels.append({"title": panel.get("title"), "queries": exprs})
            if "panels" in panel:
                walk_panels(panel["panels"])
    walk_panels(dash.get("panels", []))

    # --- Step 3: Create a temporary dashboard ---
    temp_uid = f"{dashboard_uid}-temp-{int(time.time())}"
    dash["uid"] = temp_uid
    dash["title"] = f"{dash['title']} (Temp Copy)"

    payload = {"dashboard": dash, "overwrite": True}
    put_url = f"{GRAFANA_URL}/api/dashboards/db"
    r = requests.post(put_url, headers=headers, json=payload)
    r.raise_for_status()

    return temp_uid, table_panels, GRAFANA_VARS

def delete_dashboard(uid):
    url = f"{GRAFANA_URL}/api/dashboards/uid/{uid}"
    r = requests.delete(url, headers=headers)
    if r.status_code == 200:
        logger.info(f"Deleted temporary dashboard {uid}")
    else:
        logger.warning(f"Could not delete dashboard {uid}: {r.text}")

def paginate_to_a4(img: Image.Image):
    pages = []
    y_offset = 0
    while y_offset < img.height:
        page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), A4_BG_COLOR)
        crop = img.crop((0, y_offset, A4_WIDTH_PX, min(y_offset + A4_HEIGHT_PX, img.height)))
        page.paste(crop, (0, 0))
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=95)
        pages.append(buf.getvalue())
        y_offset += A4_HEIGHT_PX
    return pages

def generate_pdf_from_pages(pages, output_path):
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(pages))
    logger.info(f"PDF saved to {output_path}")

def extract_grafana_vars(dashboard_json):
    vars_dict = {}
    for v in dashboard_json.get("templating", {}).get("list", []):
        # Use the current value if set, otherwise match everything
        value = v.get("current", {}).get("value", ".*")
        vars_dict[v["name"]] = str(value)
    return vars_dict

def resolve_grafana_vars(query: str, variables: dict, start: datetime, end: datetime) -> str:
    """Replace Grafana template variables with Prometheus-compatible values."""
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
        # Convert Grafana's $__all into regex match-all
        if not value or value in ("$__all", "['$__all']"):
            value = ".*"
        query = query.replace(f"${var}", value)
        query = query.replace(f"${{{var}}}", value)

    return query

def extract_metric(expr: str) -> str:
    """
    Extract the first Prometheus metric name from a query string,
    skipping PromQL functions like sum(), rate(), avg(), etc.
    Cleans the name by:
      1. Removing anything after ':'
      2. Removing trailing '_per_*' suffix
      3. Replacing underscores with spaces and capitalizing words
    """
    import re

    # Find all candidate tokens that look like metric names
    matches = re.findall(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:[{(])', expr)

    if not matches:
        return "Unknown Metric"

    # List of common PromQL function names to skip
    promql_functions = {
        "sum", "avg", "min", "max", "count", "stddev", "stdvar",
        "rate", "irate", "increase", "delta", "idelta",
        "sum_over_time", "avg_over_time", "min_over_time", "max_over_time",
        "quantile_over_time", "count_over_time", "last_over_time"
    }

    # Return the first non-function token
    for token in matches:
        if token not in promql_functions:
            # Remove everything after ':'
            token = token.split(":")[0]
            # Remove trailing '_per_*'
            token = re.sub(r'_per_[a-zA-Z0-9]+$', '', token)
            # Replace underscores with spaces and capitalize
            token = token.replace("_", " ").title()
            return token

    return "Unknown Metric"

def query_prometheus_range(expr: str, start: datetime, end: datetime, step: int = 3600):
    """
    Query Prometheus over a fixed time range.
    step: seconds between data points (default 1h)
    """
    params = {
        "query": expr,
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        "step": step
    }
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query_range", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def query_prometheus_instant(expr: str, eval_time: datetime):
    """Query Prometheus at an exact evaluation timestamp."""
    params = {
        "query": expr,
        "time": int(eval_time.timestamp()),
    }
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()

def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None):
    """
    Generate a Grafana report:
      1. Clone dashboard (excluding some panels).
      2. Rebuild table panels as CSVs via Prometheus queries.
      3. Render dashboard as PDF.
      4. Email PDF and CSVs if email_to is provided.
    """
    excluded_titles = excluded_titles or []
    temp_uid, csv_files, pdf_path = None, [], None

    try:
        # --- Step 1: Clone dashboard and extract table panels ---
        dashboard_uid = extract_uid_from_url(dashboard_url)
        temp_uid, table_panels, GRAFANA_VARS = clone_dashboard_without_panels(dashboard_uid, excluded_titles)

        # Step 2: Build CSVs for table panels
        start_dt, end_dt = compute_range_from_env(TIME_FROM, TIME_TO_CSV)
        logger.info(f"Querying Prometheus from {start_dt} to {end_dt}")

        for panel in table_panels:
            logger.info(f"Rebuilding table panel: {panel['title']}")

            start_dt, end_dt = compute_range_from_env(TIME_FROM, TIME_TO_CSV)
            panel_df = None

            for expr in panel["queries"]:
                expr_resolved = resolve_grafana_vars(expr, GRAFANA_VARS, start_dt, end_dt)
                metric_name = extract_metric(expr_resolved)
                query_step = compute_query_step_seconds(start_dt, end_dt)
                logger.info(
                    f"Querying Prometheus (instant @ {end_dt}) for panel '{panel['title']}':\n{expr_resolved}\n"
                    f"Start: {start_dt}, End: {end_dt}"
                )

                try:
                    results = query_prometheus_instant(expr_resolved, eval_time=end_dt)
                except Exception as e:
                    logger.error(f"Prometheus query failed for {expr_resolved}: {e}")
                    continue

                rows = []
                for r in results.get("data", {}).get("result", []):
                    metric_labels = r.get("metric", {})
                    key = metric_labels.get("project") or metric_labels.get("department") or "unknown"

                    if r.get("value"):
                        _, value = r["value"]
                        rows.append({"key": key, metric_name: float(value)})

                if rows:
                    df = pd.DataFrame(rows)
                    panel_df = df if panel_df is None else pd.merge(panel_df, df, on="key", how="outer")

            if panel_df is not None and not panel_df.empty:
                panel_df = panel_df.fillna(0)
                panel_df.rename(columns={"key": "project_or_department"}, inplace=True)
                csv_path = f"/tmp/{panel['title'].replace(' ', '_')}.csv"
                panel_df.to_csv(csv_path, index=False)
                csv_files.append(csv_path)
                logger.info(f"CSV saved for panel '{panel['title']}': {csv_path}")
            else:
                logger.warning(f"No data for panel '{panel['title']}'")
                
        # --- Step 3: Render dashboard as PDF ---
        render_url = (
            f"{GRAFANA_URL}/render/d/{temp_uid}"
            f"?kiosk&width={A4_WIDTH_PX}&height=10000&theme=light&tz=UTC"
            f"&from={TIME_FROM}&to={TIME_TO}"
        )
        logger.info(f"Rendering dashboard at {render_url}")
        r = requests.get(render_url, headers=headers, stream=True, timeout=60)
        r.raise_for_status()

        img = Image.open(io.BytesIO(r.content))
        pages = paginate_to_a4(img)

        pdf_path = f"/tmp/grafana_report_{temp_uid}.pdf"
        generate_pdf_from_pages(pages, pdf_path)
        logger.info(f"PDF saved: {pdf_path}")

        # --- Step 4: Email results ---
        if email_to:
            msg = EmailMessage()
            msg["Subject"] = f"Grafana Report - {temp_uid} - {datetime.now().strftime('%Y-%m-%d')}"
            msg["From"] = EMAIL_FROM
            msg["To"] = email_to

            # Attach PDF
            if pdf_path and os.path.exists(pdf_path):
                with open(pdf_path, "rb") as f:
                    msg.add_attachment(
                        f.read(),
                        maintype="application",
                        subtype="pdf",
                        filename=f"{temp_uid}.pdf"
                    )

            # Attach CSVs
            for csv_file in csv_files:
                with open(csv_file, "rb") as f:
                    msg.add_attachment(
                        f.read(),
                        maintype="text",
                        subtype="csv",
                        filename=os.path.basename(csv_file)
                    )

            # Send email
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                if SMTP_USERNAME and SMTP_PASSWORD:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)

            logger.info(f"Email sent to {email_to}")

        logger.info(f"Report completed successfully: PDF + {len(csv_files)} CSVs")

    except Exception as e:
        logger.error(f"Error during report generation: {e}")

    finally:
        if temp_uid:
            delete_dashboard(temp_uid)

@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=excluded_titles)
    return {"message": f"Report generation started for {req.email_to}"}
