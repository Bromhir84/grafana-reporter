import os
import requests
import img2pdf
from datetime import datetime
from email.message import EmailMessage
import smtplib
from PIL import Image
import io

# ────────────────────────────────────────────────
# Load environment variables (set via ConfigMap or local .env)
# ────────────────────────────────────────────────
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_API_KEY = os.getenv("GRAFANA_API_KEY")
FOLDER_ID = int(os.getenv("GRAFANA_FOLDER_ID", "0"))
FOLDER_TITLE = os.getenv("GRAFANA_FOLDER_TITLE", "")
DASHBOARD_TAG_FILTER = os.getenv("DASHBOARD_TAG_FILTER", "")
REPORT_OUTPUT_PATH = os.getenv("REPORT_OUTPUT_PATH", "/tmp/grafana_report.pdf")

EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_FROM = os.getenv("EMAIL_FROM")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# New environment variables for time window
TIME_FROM = os.getenv("TIME_FROM", "now-6h")  # e.g. "now-24h"
TIME_TO = os.getenv("TIME_TO", "now")         # e.g. "now"

# Formatting values.
MAX_PAGE_WIDTH = 2480  # pixels (roughly A4 portait width at 300 DPI)

# A4 dimensions in pixels at 300 DPI
A4_WIDTH_PX = 2480   # 300 DPI
A4_HEIGHT_PX = 3508  # 300 DPI
A4_BG_COLOR = "white"

headers = {
    "Authorization": f"Bearer {GRAFANA_API_KEY}"
}

# ────────────────────────────────────────────────
# Fetch and filter dashboards from all folders
# ────────────────────────────────────────────────
def fetch_dashboards():
    print("🔍 Fetching dashboards from Grafana...")
    url = f"{GRAFANA_URL}/api/search?type=dash-db"
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    dashboards = response.json()

    if FOLDER_ID:
        dashboards = [d for d in dashboards if d.get("folderId") == FOLDER_ID]
    elif FOLDER_TITLE:
        dashboards = [d for d in dashboards if d.get("folderTitle") == FOLDER_TITLE]

    if DASHBOARD_TAG_FILTER:
        dashboards = [d for d in dashboards if DASHBOARD_TAG_FILTER in d.get("tags", [])]

    print(f"📦 Found {len(dashboards)} dashboards in folder.")
    return dashboards

# ────────────────────────────────────────────────
# Fetch all panels from a dashboard
# ────────────────────────────────────────────────
def get_dashboard_panels(dashboard_uid):
    url = f"{GRAFANA_URL}/api/dashboards/uid/{dashboard_uid}"
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

    return panels

# ────────────────────────────────────────────────
# Render all panels of a dashboard to images
# ────────────────────────────────────────────────
def combine_images_rowwise(images):
    rows = []
    current_row = []
    current_width = 0
    max_height_in_row = 0

    paginated_images = []

    for img_bytes in images:
        img = Image.open(io.BytesIO(img_bytes))
        width, height = img.size

        if current_width + width <= MAX_PAGE_WIDTH:
            current_row.append(img)
            current_width += width
            max_height_in_row = max(max_height_in_row, height)
        else:
            # Finish current row and start a new one
            if current_row:
                row_image = _compose_row_image(current_row, max_height_in_row)
                paginated_images.append(row_image)
            current_row = [img]
            current_width = width
            max_height_in_row = height

    # Append the last row
    if current_row:
        row_image = _compose_row_image(current_row, max_height_in_row)
        paginated_images.append(row_image)

    return [image_to_bytes(img) for img in paginated_images]

def paginate_to_a4(images):
    pages = []
    current_page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), A4_BG_COLOR)
    y_offset = 0

    for img_bytes in images:
        img = Image.open(io.BytesIO(img_bytes))

        # Scale down if panel is wider than page
        if img.width > A4_WIDTH_PX:
            ratio = A4_WIDTH_PX / img.width
            img = img.resize((A4_WIDTH_PX, int(img.height * ratio)), Image.LANCZOS)

        # If panel doesn't fit on current page → save and start new page
        if y_offset + img.height > A4_HEIGHT_PX:
            pages.append(current_page)
            current_page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), A4_BG_COLOR)
            y_offset = 0

        current_page.paste(img, (0, y_offset))
        y_offset += img.height

    # Add last page
    if y_offset > 0:
        pages.append(current_page)

    # Return as bytes for img2pdf
    return [image_to_bytes(page) for page in pages]

def _compose_row_image(row_imgs, row_height):
    total_width = sum(i.width for i in row_imgs)
    row_image = Image.new("RGB", (total_width, row_height), color="white")
    x_offset = 0
    for img in row_imgs:
        row_image.paste(img, (x_offset, 0))
        x_offset += img.width
    return row_image


def image_to_bytes(image):
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()

def render_dashboard_panels(dashboard_uid):
    panels = get_dashboard_panels(dashboard_uid)
    print(f"📊 Found {len(panels)} panels in dashboard {dashboard_uid}.")

    rendered_images = []

    # Default scaling
    GRID_SCALE = 100
    MAX_HEIGHT = 1000
    MAX_WIDTH = 1600

    # Panel-type-specific preferred sizes
    type_defaults = {
        "piechart":    (400, 400),
        "stat":        (600, 400),
        "gauge":       (600, 400),
        "table":       None, #(1600, 600),
        "logs":        (1600, 800),
        "timeseries":  None,  # Use grid size
        "graph":       None,
        "bargauge":    (800, 400),
        "heatmap":     (1600, 600),
        "text":        None #(800, 400)
    }

    for panel in panels:
        panel_id = panel["id"]
        title = panel["title"]
        w = panel.get("w", 24)
        h = panel.get("h", 10)
        panel_type = panel.get("type", "unknown")

        # Pick rendering size
        if panel_type == "timeseries":
            px_width = min(w * GRID_SCALE, MAX_WIDTH)
            px_height = min(h * GRID_SCALE // 2, MAX_HEIGHT // 2)
        elif panel_type == "table":
            px_width = min(w * GRID_SCALE, MAX_WIDTH)
            px_height = min(h * GRID_SCALE // 2, MAX_HEIGHT // 2)
        elif panel_type in type_defaults and type_defaults[panel_type]:
            px_width, px_height = type_defaults[panel_type]
        else:
            px_width = min(w * GRID_SCALE, MAX_WIDTH)
            px_height = min(h * GRID_SCALE, MAX_HEIGHT)

        print(f"🖼 Rendering panel {panel_id} - {title} ({panel_type}) at {px_width}x{px_height}...")

        url = (
            f"{GRAFANA_URL}/render/d-solo/{dashboard_uid}"
            f"?panelId={panel_id}&theme=light"
            f"&width={px_width}&height={px_height}"
            f"&tz=UTC&from={TIME_FROM}&to={TIME_TO}"
        )

        response = requests.get(url, headers=headers, stream=True, timeout=30)
        try:
            response.raise_for_status()
            rendered_images.append(response.content)
        except requests.exceptions.HTTPError as e:
            print(f"❌ Failed to render panel {panel_id}: {e}")

    return paginate_to_a4(rendered_images)
    #return combine_images_rowwise(rendered_images)

# ────────────────────────────────────────────────
# Combine images to single PDF
# ────────────────────────────────────────────────
def generate_pdf(images, output_path=REPORT_OUTPUT_PATH):
    print(f"📄 Generating paginated PDF at {output_path}...")
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(images))
    print(f"✅ PDF saved: {output_path}")

# ────────────────────────────────────────────────
# Send PDF via email
# ────────────────────────────────────────────────
def send_email(dashboard_title, pdf_path):
    print(f"📤 Sending report for dashboard '{dashboard_title}' via email...")
    msg = EmailMessage()
    msg["Subject"] = f"Grafana Report - {dashboard_title} - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"{dashboard_title}.pdf")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            #server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"📧 Email sent for dashboard '{dashboard_title}'.")
    except Exception as e:
        print(f"❌ Failed to send email for dashboard '{dashboard_title}': {e}")

# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────
def main():
    dashboards = fetch_dashboards()
    if not dashboards:
        print("⚠️ No dashboards to render.")
        return

    for dash in dashboards:
        print(f"Processing dashboard: {dash.get('title', 'Unknown')}")
        try:
            images = render_dashboard_panels(dash["uid"])
            if images:
                # Use a unique PDF path per dashboard to avoid overwriting
                pdf_path = f"/tmp/grafana_report_{dash['uid']}.pdf"
                generate_pdf(images, output_path=pdf_path)
                if EMAIL_ENABLED:
                    send_email(dash.get('title', 'Grafana Dashboard'), pdf_path)
            else:
                print(f"⚠️ No images rendered for dashboard '{dash.get('title')}'. Skipping email.")
        except Exception as e:
            print(f"❌ Failed processing dashboard '{dash.get('title')}': {e}")


if __name__ == "__main__":
    main()
