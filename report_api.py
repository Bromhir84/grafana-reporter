import os
import re
import io
import img2pdf
import requests
from datetime import datetime
from email.message import EmailMessage
import smtplib
from PIL import Image
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import logging
import csv
import time

app = FastAPI(root_path=os.getenv("ROOT_PATH","/report"))

# Allow Grafana front-end to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or use ["http://your-grafana-domain"] for stricter security
    allow_methods=["*"],
    allow_headers=["*"],
)


# ────────────────────────────────────────────────
# Environment variables (set these before running the app)
# ────────────────────────────────────────────────
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_API_KEY = os.getenv("GRAFANA_API_KEY")

TIME_FROM = os.getenv("TIME_FROM", "now-6h")
TIME_TO = os.getenv("TIME_TO", "now")

EMAIL_FROM = os.getenv("EMAIL_FROM")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

MAX_PAGE_WIDTH = 2480
A4_WIDTH_PX = 2480
A4_HEIGHT_PX = 3508
A4_BG_COLOR = "white"

excluded_titles = ["Report Button", "Another panel"]
excluded_titles_lower = [t.strip().lower() for t in excluded_titles]

headers = {
    "Authorization": f"Bearer {GRAFANA_API_KEY}"
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
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
    else:
        raise ValueError("Invalid dashboard URL format. Expected something like /d/<uid>/")
    
def filter_panels(panels, excluded_titles_lower):
    filtered = []
    logger.info(f"Filtering panels, excluding titles: {excluded_titles_lower}")
    for panel in panels:
        title = panel.get("title", "").strip().lower()
        if "panels" in panel:
            panel["panels"] = filter_panels(panel["panels"], excluded_titles_lower)
        if title in excluded_titles_lower:
            logger.info(f"Excluding panel: '{title}'")
            continue
        else:
            logger.info(f"Keeping panel: '{title}'")
            filtered.append(panel)
    return filtered

def clone_dashboard_without_panels(original_uid, excluded_titles, folder_name="Temp Reports"):
    """
    Clone the original dashboard, remove excluded panels, and save in a dedicated folder.
    Returns the UID of the saved dashboard.
    """
    logger.info(f"Fetching dashboard UID: {original_uid}")
    url = f"{GRAFANA_URL}/api/dashboards/uid/{original_uid}"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    dashboard_data = r.json()
    dashboard = dashboard_data["dashboard"]

    # Remove panels by title
    excluded_titles_lower = [t.lower() for t in excluded_titles]
    dashboard["panels"] = filter_panels(dashboard.get("panels", []), excluded_titles_lower)

    # Remove fields that Grafana doesn't allow on creation
    dashboard.pop("id", None)
    dashboard.pop("version", None)

    # Make UID unique and URL-safe
    timestamp = int(datetime.now().timestamp())
    dashboard["uid"] = re.sub(r'[^a-zA-Z0-9_-]', '-', f"{original_uid}-temp-{timestamp}")
    dashboard["title"] += f" (Temp Render {timestamp})"

    # Ensure folder exists
    folder_id = 0  # default folder
    try:
        # List folders
        folders_url = f"{GRAFANA_URL}/api/folders"
        resp = requests.get(folders_url, headers=headers)
        resp.raise_for_status()
        folders = resp.json()
        folder = next((f for f in folders if f["title"] == folder_name), None)
        if folder:
            folder_id = folder["id"]
        else:
            # Create folder
            create_url = f"{GRAFANA_URL}/api/folders"
            r = requests.post(create_url, headers=headers, json={"title": folder_name})
            r.raise_for_status()
            folder_id = r.json()["id"]
            logger.info(f"Created folder '{folder_name}' with ID {folder_id}")
    except Exception as e:
        logger.warning(f"Could not create/find folder '{folder_name}': {e}, saving to root folder.")

    # Save cloned dashboard
    payload = {
        "dashboard": dashboard,
        "folderId": folder_id,
        "overwrite": False
    }

    save_url = f"{GRAFANA_URL}/api/dashboards/db"
    r = requests.post(save_url, headers=headers, json=payload)
    r.raise_for_status()
    logger.info(f"Temporary dashboard saved in '{folder_name}': {dashboard['uid']}")

    # Small delay to ensure Grafana registers the dashboard
    time.sleep(2)

    return dashboard["uid"]


def delete_dashboard(uid):
    url = f"{GRAFANA_URL}/api/dashboards/uid/{uid}"
    r = requests.delete(url, headers=headers)
    if r.status_code == 200:
        logger.info(f"Deleted temporary dashboard {uid}")
    else:
        logger.warning(f"Could not delete dashboard {uid}: {r.text}")


def get_dashboard_panels(dashboard_uid):
    url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
    logger.info(f"Fetching dashboard panels from {url}")
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    dashboard = response.json()

    panels = []
    for panel in dashboard["dashboard"].get("panels", []):
        if "id" in panel and "gridPos" in panel:
            size = panel["gridPos"]
            panels.append({
                "id": panel["id"],
                "title": panel.get("title", "Unnamed Panel"),
                "w": size.get("w", 24),
                "h": size.get("h", 10),
                "type": panel.get("type", "unknown")
            })
    logger.info(f"Found {len(panels)} panels")
    return panels, dashboard["dashboard"].get("title", f"Dashboard-{dashboard_uid}")

def render_full_dashboard(dashboard_uid):
    # Render the entire dashboard as one large image
    # You might need to adjust width for high‐res printing
    px_width = A4_WIDTH_PX
    px_height = 5000  # tall enough for most dashboards
    url = (
        f"{GRAFANA_URL}/render/d/{dashboard_uid}"
        f"?theme=light&width={px_width}&height={px_height}"
        f"&tz=UTC&from={TIME_FROM}&to={TIME_TO}&kiosk"
    )

    logger.info(f"Rendering full dashboard: {url}")
    response = requests.get(url, headers=headers, stream=True, timeout=60)
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content))

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

def generate_pdf(images, output_path):
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(images))

def generate_pdf_from_pages(pages, output_path):
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(pages))
    logger.info(f"PDF saved to {output_path}")

def resolve_datasource(datasource):
    """
    Given a panel.datasource (string name, string uid, or dict),
    fetch the correct uid and type from Grafana.
    """
    if isinstance(datasource, dict):
        return datasource.get("uid"), datasource.get("type")

    if isinstance(datasource, str):
        # Try by UID first
        resp = requests.get(f"{GRAFANA_URL}/api/datasources/uid/{datasource}", headers=headers, timeout=30)
        if resp.status_code == 200:
            ds_info = resp.json()
            return ds_info["uid"], ds_info["type"]

        # Try by NAME
        resp = requests.get(f"{GRAFANA_URL}/api/datasources/name/{datasource}", headers=headers, timeout=30)
        if resp.status_code == 200:
            ds_info = resp.json()
            return ds_info["uid"], ds_info["type"]

        logger.error(f"Datasource '{datasource}' not found in Grafana")
        return None, None

    return None, None
    
def fetch_table_panel_csv(panel, dashboard_uid, retries=3, delay=2):
    """
    Fetch CSV for a single table panel using Grafana's /api/ds/query endpoint.
    """
    panel_id = panel.get("id")
    panel_title = panel.get("title", f"Panel-{panel_id}")
    panel_type = panel.get("type")

    # Only process table panels
    if panel_type != "table":
        return None

    try:
        # Step 1: Fetch dashboard definition (to get datasource + queries)
        url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        dashboard = resp.json()["dashboard"]

        # Locate this panel's full config
        panel_def = next((p for p in dashboard.get("panels", []) if p.get("id") == panel_id), None)
        if not panel_def:
            logger.error(f"Panel {panel_id} not found in dashboard {dashboard_uid}")
            return None

        datasource = panel_def.get("datasource")
        queries = panel_def.get("targets", [])
        if not queries:
            logger.warning(f"No queries found for panel '{panel_title}'")
            return None

        # Step 2: Build payload for /api/ds/query
        # If datasource is string (uid), fetch full datasource details
        if isinstance(datasource, str):
            ds_resp = requests.get(f"{GRAFANA_URL}/api/datasources/uid/{datasource}", headers=headers, timeout=30)
            ds_resp.raise_for_status()
            ds_info = ds_resp.json()
            ds_uid = ds_info["uid"]
            ds_type = ds_info["type"]
        else:
            ds_uid = datasource.get("uid")
            ds_type = datasource.get("type", "prometheus")
        payload = {
            "from": TIME_FROM,
            "to": TIME_TO,
            "queries": [
                {
                    "refId": q.get("refId", "A"),
                    "datasource": {"uid": ds_uid, "type": ds_type},
                    "intervalMs": 60000,
                    "maxDataPoints": 500,
                    **q
                }
                for q in queries
            ]
        }

        query_url = f"{GRAFANA_URL}/api/ds/query"

        # Step 3: Retry loop
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(query_url, headers=headers, json=payload, timeout=60)
                r.raise_for_status()
                data = r.json()
                logger.info(f"Fetched data for panel '{panel_title}'")

                # Step 4: Convert JSON results to CSV
                results = data.get("results", {})
                rows = []
                header = ["Panel Title"]

                for _, result in results.items():
                    frames = result.get("frames", [])
                    for frame in frames:
                        schema = frame.get("schema", {})
                        fields = schema.get("fields", [])
                        names = [f["name"] for f in fields]
                        if len(header) == 1:  # only once
                            header.extend(names)
                        values = frame.get("data", {}).get("values", [])
                        # transpose columns -> rows
                        for row in zip(*values):
                            rows.append([panel_title] + list(row))

                # Convert to CSV string
                if not rows:
                    logger.warning(f"No rows found for panel '{panel_title}'")
                    return None

                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(header)
                writer.writerows(rows)
                return buf.getvalue()

            except requests.HTTPError as e:
                if r.status_code == 404 and attempt < retries:
                    logger.warning(f"Data not ready for panel '{panel_title}', retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    logger.error(f"Failed to fetch data for panel '{panel_title}': {e}")
                    return None

    except Exception as e:
        logger.error(f"Error fetching CSV for panel '{panel_title}': {e}")
        return None

def generate_csv_from_table_panels(panels, dashboard_uid, output_path):
    """Combine all table panel CSVs into a single CSV file."""
    all_rows = []
    header_written = False

    for panel in panels:
        csv_content = fetch_table_panel_csv(panel, dashboard_uid)
        if not csv_content:
            continue

        f = io.StringIO(csv_content)
        reader = csv.reader(f)
        rows = list(reader)
        if not rows:
            continue

        # Prepend panel title column
        header = ["Panel Title"] + rows[0]
        data_rows = [[panel.get("title", "Panel")] + row for row in rows[1:]]

        if not header_written:
            with open(output_path, "w", newline="") as out_f:
                writer = csv.writer(out_f)
                writer.writerow(header)
                writer.writerows(data_rows)
            header_written = True
        else:
            with open(output_path, "a", newline="") as out_f:
                writer = csv.writer(out_f)
                writer.writerows(data_rows)

        all_rows.extend(data_rows)

    if all_rows:
        logger.info(f"CSV saved to {output_path}")
        return True
    else:
        logger.warning("No table panel data available to write.")
        return False

def send_email(dashboard_title, pdf_path, email_to, csv_path=None):
    msg = EmailMessage()
    msg["Subject"] = f"Grafana Report - {dashboard_title} - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = email_to

    # Attach PDF
    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"{dashboard_title}.pdf")

    # Attach CSV if available
    if csv_path:
        with open(csv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=f"{dashboard_title}.csv")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            if SMTP_USERNAME and SMTP_PASSWORD:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info(f"Email sent to {email_to}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        raise

def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None):
    excluded_titles = excluded_titles or []
    temp_uid = None

    try:
        # Extract original dashboard UID
        dashboard_uid = extract_uid_from_url(dashboard_url)

        # Step 1: Clone dashboard into Temp Reports folder
        temp_uid = clone_dashboard_without_panels(dashboard_uid, excluded_titles, folder_name="Temp Reports")

        # Step 2: Fetch all panels
        panels, dashboard_title = get_dashboard_panels(temp_uid)

        # Step 3: Render PDF
        render_url = (
            f"{GRAFANA_URL}/render/d/{temp_uid}"
            f"?kiosk&width={A4_WIDTH_PX}&height=10000"
            f"&theme=light&tz=UTC&from={TIME_FROM}&to={TIME_TO}"
        )
        logger.info(f"Rendering dashboard at {render_url}")
        r = requests.get(render_url, headers=headers, stream=True, timeout=60)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        pages = paginate_to_a4(img)
        pdf_path = f"/tmp/grafana_report_{temp_uid}.pdf"
        generate_pdf_from_pages(pages, pdf_path)

        # Step 4: Generate CSV for table panels
        csv_path = f"/tmp/grafana_report_{temp_uid}.csv"
        csv_written = generate_csv_from_table_panels(panels, temp_uid, csv_path)
        if not csv_written:
            csv_path = None  # Skip CSV attachment if no table data

        # Step 5: Send email with PDF and CSV (if any)
        if email_to:
            send_email(dashboard_title, pdf_path, email_to, csv_path)

        logger.info(f"Report generation completed for '{dashboard_title}'")

    except Exception as e:
        logger.error(f"Error during report generation: {e}")

    finally:
        if temp_uid:
            delete_dashboard(temp_uid)

@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=excluded_titles)
    return {"message": f"Report generation started for {req.email_to}"}
