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

def clone_dashboard_without_panels(original_uid, excluded_titles):
    logger.info(f"Fetching dashboard UID: {original_uid}")
    url = f"{GRAFANA_URL}/api/dashboards/uid/{original_uid}"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    dashboard_data = r.json()["dashboard"]

    dashboard_data["panels"] = filter_panels(dashboard_data.get("panels", []), [t.lower() for t in excluded_titles])
    dashboard_data["uid"] = f"{original_uid}-temp-{int(datetime.now().timestamp())}"
    dashboard_data["title"] += " (Temp Render)"

    payload = {"dashboard": dashboard_data, "folderId": 0, "overwrite": False}
    save_url = f"{GRAFANA_URL}/api/dashboards/db"
    r = requests.post(save_url, headers=headers, json=payload)
    r.raise_for_status()
    logger.info(f"Temporary dashboard created: {dashboard_data['uid']}")
    return dashboard_data["uid"]

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

def download_table_csvs(dashboard_uid, output_dir="/tmp/grafana_csvs"):
    os.makedirs(output_dir, exist_ok=True)
    csv_files = []
    dashboard_url = f"{GRAFANA_URL}/d/{dashboard_uid}"

    logger.info(f"Opening temporary dashboard for CSV export: {dashboard_url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(extra_http_headers={"Authorization": f"Bearer {GRAFANA_API_KEY}"})
        page = context.new_page()
        page.goto(dashboard_url)
        page.wait_for_timeout(8000)

        panels = page.query_selector_all("div[data-testid='data-testid panel content']")
        logger.info(f"Found {len(panels)} panels")

        for idx, panel in enumerate(panels, start=1):
            try:
                panel_header = panel.evaluate_handle("el => el.closest('section').querySelector('h2')")
                panel_title = panel_header.inner_text().strip() if panel_header else f"panel_{idx}"
                safe_title = panel_title.replace(" ", "_").replace("/", "_").replace("$", "")
                logger.info(f"Processing panel '{panel_title}'")

                panel.hover()
                menu_btn = panel.query_selector('button[aria-label^="Menu for panel"]')
                if not menu_btn:
                    logger.warning(f"Panel '{panel_title}': No menu button found, skipping CSV")
                    continue
                menu_btn.click()
                page.locator("text=Inspect").click()
                page.locator("text=Data").click()

                page.wait_for_selector('button:has-text("Download CSV")', timeout=10000)
                with page.expect_download() as download_info:
                    page.locator('button:has-text("Download CSV")').click()
                download = download_info.value
                csv_path = os.path.join(output_dir, f"{safe_title}.csv")
                download.save_as(csv_path)
                csv_files.append(csv_path)
                logger.info(f"Downloaded CSV → {csv_path}")
                page.goto(dashboard_url)
                page.wait_for_timeout(2000)

            except Exception as e:
                logger.error(f"Error processing panel {idx}: {e}")

        browser.close()
    logger.info(f"CSV export finished, downloaded {len(csv_files)} files")
    return csv_files

def process_report(dashboard_url: str, email_to: str = None, excluded_titles=None):
    excluded_titles = excluded_titles or []
    temp_uid = None
    try:
        dashboard_uid = extract_uid_from_url(dashboard_url)
        temp_uid = clone_dashboard_without_panels(dashboard_uid, excluded_titles)

        render_url = f"{GRAFANA_URL}/render/d/{temp_uid}?kiosk&width={A4_WIDTH_PX}&height=10000&theme=light&tz=UTC&from={TIME_FROM}&to={TIME_TO}"
        logger.info(f"Rendering dashboard at {render_url}")
        r = requests.get(render_url, headers=headers, stream=True, timeout=60)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))

        pages = paginate_to_a4(img)
        logger.info(f"Dashboard paginated into {len(pages)} pages")

        pdf_path = f"/tmp/grafana_report_{temp_uid}.pdf"
        generate_pdf_from_pages(pages, pdf_path)

        csv_files = []
        if email_to:
            csv_files = download_table_csvs(temp_uid)

        if email_to:
            send_email_msg = EmailMessage()
            send_email_msg["Subject"] = f"Grafana Report - {temp_uid} - {datetime.now().strftime('%Y-%m-%d')}"
            send_email_msg["From"] = EMAIL_FROM
            send_email_msg["To"] = email_to
            with open(pdf_path, "rb") as f:
                send_email_msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"{temp_uid}.pdf")
            for csv_file in csv_files:
                with open(csv_file, "rb") as f:
                    send_email_msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_file))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                if SMTP_USERNAME and SMTP_PASSWORD:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(send_email_msg)
            logger.info(f"Email sent to {email_to}")

        logger.info(f"Report generation completed: PDF + {len(csv_files)} CSVs")
    except Exception as e:
        logger.error(f"Error during report generation: {e}")
    finally:
        if temp_uid:
            delete_dashboard(temp_uid)

@app.post("/generate_report/")
async def generate_report(req: ReportRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_report, req.dashboard_url, req.email_to, excluded_titles=excluded_titles)
    return {"message": f"Report generation started for {req.email_to}"}
