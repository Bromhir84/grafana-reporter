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

app = FastAPI(root_path="/report")

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

def clone_dashboard_without_panels(original_uid, excluded_titles):
    logger.info(f"Fetching dashboard UID: {original_uid}")
    url = f"{GRAFANA_URL}/api/dashboards/uid/{original_uid}"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    dashboard_data = r.json()

    dashboard = dashboard_data["dashboard"]
    logger.info(f"Original dashboard has {len(dashboard.get('panels', []))} panels")

    logger.info("Panels before filtering:")
    for p in dashboard.get("panels", []):
        logger.info(f"  - '{p.get('title')}'")

    # Remove panels by title
    excluded_titles_lower = [t.lower() for t in excluded_titles]
    dashboard["panels"] = filter_panels(dashboard.get("panels", []), excluded_titles_lower)
    logger.info(f"Dashboard after filtering: {len(dashboard['panels'])} panels")

    logger.info("Panels after filtering:")
    for p in dashboard.get("panels", []):
        logger.info(f"  - '{p.get('title')}'")

    # Give a new UID and modify title
    dashboard["uid"] = f"{original_uid}-temp-{int(datetime.now().timestamp())}"
    dashboard["title"] += " (Temp Render)"

    payload = {
        "dashboard": dashboard,
        "folderId": 0,
        "overwrite": False
    }

    save_url = f"{GRAFANA_URL}/api/dashboards/db"
    r = requests.post(save_url, headers=headers, json=payload)
    r.raise_for_status()
    logger.info(f"Temporary dashboard created: {dashboard['uid']}")
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

def send_email(dashboard_title, pdf_path, email_to):
    msg = EmailMessage()
    msg["Subject"] = f"Grafana Report - {dashboard_title} - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = email_to

    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"{dashboard_title}.pdf")

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
        dashboard_uid = extract_uid_from_url(dashboard_url)

        # Fetch original dashboard title
        url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        original_dashboard = r.json()["dashboard"]
        original_title = original_dashboard.get("title", "Grafana Dashboard")

        # Step 1: Clone dashboard without excluded panels
        temp_uid = clone_dashboard_without_panels(dashboard_uid, excluded_titles)

        # Step 2: Render full dashboard image in kiosk mode (tall image)
        render_url = (
            f"{GRAFANA_URL}/render/d/{temp_uid}"
            f"?kiosk&width={A4_WIDTH_PX}&height=10000"  # height large enough to fit entire dashboard
            f"&theme=light&tz=UTC&from={TIME_FROM}&to={TIME_TO}"
        )
        logger.info(f"Rendering dashboard at {render_url}")
        r = requests.get(render_url, headers=headers, stream=True, timeout=60)
        r.raise_for_status()

        img = Image.open(io.BytesIO(r.content))

        # Step 3: Paginate image to A4 pages
        pages = paginate_to_a4(img)
        logger.info(f"Dashboard paginated into {len(pages)} pages")

        # Step 4: Generate PDF from pages
        pdf_path = f"/tmp/grafana_report_{temp_uid}.pdf"
        generate_pdf_from_pages(pages, pdf_path)

        # Step 5: Send email if requested
        if email_to:
            send_email(original_title, pdf_path, email_to)

        logger.info(f"Report generation completed: {pdf_path}")

    except Exception as e:
        logger.error(f"Error during report generation: {e}")

    finally:
        if temp_uid:
            delete_dashboard(temp_uid)


@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=excluded_titles)
    return {"message": f"Report generation started for {req.email_to}"}
