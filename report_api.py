# ─────────────────────────────────────────────────────────────
# FastAPI Grafana Report Script (PDF + CSV, temp dashboard)
# ─────────────────────────────────────────────────────────────
import os
import re
import io
import img2pdf
import requests
from datetime import datetime
from email.message import EmailMessage
import smtplib
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
PROMETHEUS_URL = os.getenv("http://your-prometheus-server:9090")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_API_KEY = os.getenv("GRAFANA_API_KEY")
TIME_FROM = os.getenv("TIME_FROM", "now-6h")
TIME_TO = os.getenv("TIME_TO", "now")
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

def clone_dashboard_without_panels(original_uid, excluded_titles):
    logger.info(f"Fetching dashboard UID: {original_uid}")
    url = f"{GRAFANA_URL}/api/dashboards/uid/{original_uid}"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    dashboard_data = r.json()["dashboard"]

    all_panels = dashboard_data.get("panels", [])

    # Log all panel titles before filtering
    if all_panels:
        logger.info("Panels found in original dashboard:")
        def log_panels(panels, prefix=""):
            for panel in panels:
                title = panel.get("title", "Unnamed Panel")
                logger.info(f"{prefix}- {title}")
                if "panels" in panel:
                    log_panels(panel["panels"], prefix + "  ")
        log_panels(all_panels)
    else:
        logger.info("No panels found in original dashboard")

    # Filter out excluded panels
    dashboard_data["panels"] = filter_panels(all_panels, [t.lower() for t in excluded_titles])

    # Assign new UID and modify title
    dashboard_data["uid"] = f"{original_uid}-temp-{int(datetime.now().timestamp())}"
    dashboard_data["title"] += " (Temp Render)"

    payload = {"dashboard": dashboard_data, "folderId": 0, "overwrite": False}
    save_url = f"{GRAFANA_URL}/api/dashboards/db"
    r = requests.post(save_url, headers=headers, json=payload)
    r.raise_for_status()
    logger.info(f"Temporary dashboard created: {dashboard_data['uid']}")

    # ✅ Use the ORIGINAL dashboard to extract table panels
    table_panels = extract_table_panels(r.json().get("dashboard", dashboard_data))

    return dashboard_data["uid"], table_panels

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


def query_prometheus(promql):
    """Query Prometheus and return a list of results."""
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": promql})
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "success":
        raise ValueError("Prometheus query failed")
    return data["data"]["result"]

def build_total_consumption_table(cluster=None, projects=None, departments=None, time_range="1h"):
    """Build a table similar to the Grafana 'Total consumption' panel."""
    
    # Define PromQL expressions for each metric
    queries = {
        "GPU allocation hours": f'sum(sum_over_time((runai_allocated_gpu_count_per_pod:hourly{{clusterId=~"{cluster}", project=~"{projects}", department=~"{departments}"}}[{time_range}]))) by (project) / 3600',
        "CPU allocation hours": f'sum(sum_over_time((runai_allocated_millicpus_per_pod:hourly{{clusterId=~"{cluster}", project=~"{projects}", department=~"{departments}"}}[{time_range}]))) by (department, project) / 3600 / 1000',
        "Memory (GB) allocation hours": f'sum(sum_over_time((runai_allocated_memory_per_pod:hourly{{clusterId=~"{cluster}", project=~"{projects}", department=~"{departments}"}}[{time_range}]))) by (project) / 3600 / 1e9',
        "CPU usage hours": f'sum(sum_over_time((runai_used_cpu_cores_per_pod:hourly{{clusterId=~"{cluster}", project=~"{projects}", department=~"{departments}"}}[{time_range}]))) by (project) / 3600',
        "Memory (GB) usage hours": f'sum(sum_over_time((runai_used_memory_bytes_per_pod:hourly{{clusterId=~"{cluster}", project=~"{projects}", department=~"{departments}"}}[{time_range}]))) by (project) / 3600 / 1e9',
        "GPU Idle allocated hours": f'sum(sum_over_time((runai_gpu_idle_hours_per_queue:hourly{{clusterId=~"{cluster}", project=~"{projects}", department=~"{departments}"}}[{time_range}]))) by (project) / 3600'
    }

    dfs = []
    for name, expr in queries.items():
        results = query_prometheus(expr)
        # Convert each Prometheus result to DataFrame
        rows = []
        for r in results:
            metric = r.get("metric", {})
            project = metric.get("project")
            department = metric.get("department", "")
            value = float(r["value"][1])
            rows.append({"Project": project, "Department": department, name: value})
        if rows:
            df = pd.DataFrame(rows)
            dfs.append(df)

    # Merge all metrics on Project + Department
    if not dfs:
        return pd.DataFrame()  # no data

    from functools import reduce
    table = reduce(lambda left, right: pd.merge(left, right, on=["Project","Department"], how="outer"), dfs)
    table = table.fillna(0)  # fill missing metrics with 0
    return table

def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None):
    excluded_titles = excluded_titles or []
    temp_uid = None
    csv_files = []

    try:
        dashboard_uid = extract_uid_from_url(dashboard_url)
        temp_uid, table_panels = clone_dashboard_without_panels(dashboard_uid, excluded_titles)

        # --- Rebuild tables from Prometheus queries ---
        for panel in table_panels:
            logger.info(f"Rebuilding table: {panel['title']}")
            combined_df = None
            for expr in panel["queries"]:
                results = query_prometheus(expr)
                rows = []
                for r in results:
                    metric = r.get("metric", {})
                    value = float(r["value"][1])
                    rows.append({**metric, "value": value})
                if rows:
                    df = pd.DataFrame(rows)
                    combined_df = df if combined_df is None else combined_df.merge(df, how="outer")
            if combined_df is not None:
                csv_path = f"/tmp/{panel['title'].replace(' ','_')}.csv"
                combined_df.to_csv(csv_path, index=False)
                csv_files.append(csv_path)
                logger.info(f"Table saved as CSV: {csv_path}")

        # --- Generate PDF as before ---
        render_url = f"{GRAFANA_URL}/render/d/{temp_uid}?kiosk&width={A4_WIDTH_PX}&height=10000&theme=light&tz=UTC&from={TIME_FROM}&to={TIME_TO}"
        r = requests.get(render_url, headers=headers, stream=True, timeout=60)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        pages = paginate_to_a4(img)
        pdf_path = f"/tmp/grafana_report_{temp_uid}.pdf"
        generate_pdf_from_pages(pages, pdf_path)

        # --- Send email with attachments ---
        if email_to:
            msg = EmailMessage()
            msg["Subject"] = f"Grafana Report - {temp_uid} - {datetime.now().strftime('%Y-%m-%d')}"
            msg["From"] = EMAIL_FROM
            msg["To"] = email_to

            with open(pdf_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"{temp_uid}.pdf")

            for csv_file in csv_files:
                with open(csv_file, "rb") as f:
                    msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_file))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                if SMTP_USERNAME and SMTP_PASSWORD:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
            logger.info(f"Email sent to {email_to}")

        logger.info(f"Report generation completed: PDF + {len(csv_files)} CSVs")

    finally:
        if temp_uid:
            delete_dashboard(temp_uid)

@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=excluded_titles)
    return {"message": f"Report generation started for {req.email_to}"}
