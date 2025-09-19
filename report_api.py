# ─────────────────────────────────────────────────────────────
# FastAPI Grafana Report Script (PDF + CSV, temp dashboard)
# ─────────────────────────────────────────────────────────────
import os
import re
import io
import csv
import time
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

    # Snap to start of period if /M, /d, /w
    if time_str.endswith("/M"):
        dt = dt.replace(day=1, hour=0, minute=0, second=0)
    elif time_str.endswith("/d"):
        dt = dt.replace(hour=0, minute=0, second=0)
    elif time_str.endswith("/w"):
        # Snap to previous Monday
        dt = dt - relativedelta(days=dt.weekday())
        dt = dt.replace(hour=0, minute=0, second=0)

    return dt

def compute_range_from_env(time_from: str, time_to: str):
    """Return start and end datetime based on TIME_FROM and TIME_TO."""
    start = parse_grafana_time(time_from)
    end = parse_grafana_time(time_to)
    return start, end

def compute_prometheus_duration(start: datetime, end: datetime) -> str:
    """Return Prometheus duration string (e.g., '720h') for use in sum_over_time."""
    delta = end - start
    # Prometheus durations: seconds (s), minutes (m), hours (h), days (d)
    # We'll convert everything to hours for convenience
    hours = int(delta.total_seconds() / 3600)
    return f"{hours}h"

def extract_uid_from_url(url: str) -> str:
    match = re.search(r"/d/([^/]+)/", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid dashboard URL format. Expected /d/<uid>/")

def filter_panels(panels, excluded_titles_lower):
    filtered = []
    for panel in panels:
        title = panel.get("title", "").strip().lower()
        if "panels" in panel:
            panel["panels"] = filter_panels(panel["panels"], excluded_titles_lower)
        if title not in excluded_titles_lower:
            filtered.append(panel)
    return filtered

def extract_table_panels(dashboard_data):
    """Return list of (title, queries) for all table panels in a dashboard."""
    tables = []
    def walk_panels(panels):
        for panel in panels:
            if panel.get("type") == "table":
                queries = [t["expr"] for t in panel.get("targets", []) if "expr" in t]
                tables.append({"title": panel.get("title", "Untitled Table"), "queries": queries})
            if "panels" in panel:  # nested rows
                walk_panels(panel["panels"])
    walk_panels(dashboard_data.get("panels", []))
    return tables

def clone_dashboard_without_panels(dashboard_uid: str, excluded_titles=None):
    excluded_titles = excluded_titles or []
    url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
    r = requests.get(url, headers=headers)
    r.raise_for_status()

    dash = r.json()["dashboard"]
    GRAFANA_VARS = extract_grafana_vars(dash)

    table_panels = []
    for panel in dash.get("panels", []):
        if panel.get("type") == "table":
            exprs = []
            for target in panel.get("targets", []):
                if "expr" in target:
                    exprs.append(target["expr"])
            table_panels.append({"title": panel.get("title"), "queries": exprs})

    # clone dashboard logic stays the same...
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

def list_dashboard_panels(dashboard_url, api_key=None):
    """
    Open the original dashboard in Playwright and print all panel titles.
    Returns a list of panel titles.
    """
    from playwright.sync_api import sync_playwright

    logger.info(f"Opening original dashboard: {dashboard_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(extra_http_headers={
            "Authorization": f"Bearer {api_key}"
        } if api_key else {})

        page = context.new_page()
        page.goto(dashboard_url)
        page.wait_for_timeout(8000)  # Wait for dashboard to fully render
        logger.info("Dashboard loaded")

        # Attempt to locate all panel titles robustly
        panel_titles = set()
        for panel_div in page.query_selector_all("div[role='region'], div[data-panelid]"):
            try:
                # Try h2 inside panel
                h2 = panel_div.query_selector("h2")
                if h2:
                    panel_titles.add(h2.inner_text().strip())
                else:
                    # Fallback: look for any child with text content
                    text = panel_div.inner_text().strip()
                    if text:
                        panel_titles.add(text.split("\n")[0])  # take first line
            except Exception:
                continue

        if panel_titles:
            logger.info("Panels found on original dashboard:")
            for t in panel_titles:
                logger.info(f" - {t}")
        else:
            logger.warning("No panels found on the original dashboard.")

        browser.close()
        return list(panel_titles)

def extract_grafana_vars(dashboard_json):
    vars_dict = {}
    for v in dashboard_json.get("templating", {}).get("list", []):
        # Use the current value if set, otherwise match everything
        value = v.get("current", {}).get("value", ".*")
        vars_dict[v["name"]] = str(value)
    return vars_dict

def resolve_grafana_vars(query: str, variables: dict, start: datetime, end: datetime) -> str:
    """Replace Grafana template variables with Prometheus-compatible values."""
    for var, value in variables.items():
        # Convert Grafana's $__all into regex match-all
        if not value or value in ("$__all", "['$__all']"):
            value = ".*"
        query = query.replace(f"${var}", value)
        query = query.replace(f"${{{var}}}", value)
    
    # Replace $__range with the correct duration
    query = query.replace("$__range", compute_prometheus_duration(start, end))
    
    return query

def query_prometheus(expr: str):
    resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": expr},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()

def extract_metric(expr: str) -> str:
    """Extract first Prometheus metric name from query string."""
    match = re.search(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*[{(]', expr)
    return match.group(1) if match else "unknown_metric"

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

def save_prometheus_results_to_csv(results: dict, csv_path: str):
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for item in results.get("data", {}).get("result", []):
            metric = ",".join([f"{k}={v}" for k, v in item["metric"].items()])
            value = item["value"][1] if "value" in item else None
            writer.writerow([metric, value])

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
            combined_df = None

            # Compute start/end from env
            start_dt, end_dt = compute_range_from_env(TIME_FROM, TIME_TO_CSV)

            for expr in panel["queries"]:
                expr_resolved = resolve_grafana_vars(expr, GRAFANA_VARS, start_dt, end_dt)
                metric_name = extract_metric(expr_resolved)
                range_seconds = int((end_dt - start_dt).total_seconds())
                logger.info(f"Querying Prometheus for panel '{panel['title']}':\n{expr_resolved}\nStart: {start_dt}, End: {end_dt}")
            

                try:
                    results = query_prometheus_range(expr_resolved, start=start_dt, end=end_dt, step=range_seconds)
                except Exception as e:
                    logger.error(f"Prometheus query failed for {expr_resolved}: {e}")
                    continue
            
                rows = []
                for r in results.get("data", {}).get("result", []):
                    metric = r.get("metric", {})
                    for timestamp, value in r.get("values", []):  # [timestamp, value]
                        rows.append({
                            **metric,
                            "timestamp": datetime.utcfromtimestamp(timestamp),
                            "value": float(value),
                            "metric_name": metric_name  # 👈 add metric name column
                        })

                if rows:
                    df = pd.DataFrame(rows)
                    combined_df = df if combined_df is None else pd.concat([combined_df, df], ignore_index=True)

            if combined_df is not None and not combined_df.empty:
                csv_path = f"/tmp/{panel['title'].replace(' ', '_')}.csv"
                combined_df.to_csv(csv_path, index=False)
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
